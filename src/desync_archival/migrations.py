"""Numbered .sql migrations tracked with PRAGMA user_version.

The app never migrates a database on its own. If the schema is behind, it refuses to start, and
applying migrations is an explicit command. On production that command also needs a confirmation.
"""

import os
import re
import shutil
import sqlite3
from pathlib import Path

from desync_archival import backup
from desync_archival import settings as app_config
from desync_archival.services.logger import logger

MIGRATIONS_DIR = app_config.REPO_ROOT / "migrations"
FILE_PATTERN = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
NO_TRANSACTION = "-- no-transaction"


class MigrationError(RuntimeError):
    """A database can't be brought to the current schema safely."""


def load_migrations() -> list[tuple[int, Path]]:
    """Lists migrations in order. Numbers must run from 1 with no gaps.

    Raises:
        MigrationError: The database can't be brought to this schema safely.
    """
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = FILE_PATTERN.fullmatch(path.name)
        if not match:
            raise MigrationError(f"Unexpected migration file name: {path.name}")
        found.append((int(match.group(1)), path))
    versions = [version for version, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"Migration numbers must run from 1 with no gaps: {versions}")
    return found


def latest_version() -> int:
    return len(load_migrations())


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _columns_by_table(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name: {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')} for (name, *_) in tables}


def _baseline_columns() -> dict[str, set[str]]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(load_migrations()[0][1].read_text(encoding="utf-8"))
        return _columns_by_table(conn)
    finally:
        conn.close()


def adopt_baseline(conn: sqlite3.Connection) -> None:
    """Marks an existing database as version 1 after checking it, with no structural change."""
    existing = _columns_by_table(conn)
    missing = sorted(
        f"{table}.{column}"
        for table, columns in _baseline_columns().items()
        for column in columns - existing.get(table, set())
    )
    if missing:
        raise MigrationError(f"Database does not match the baseline schema. Missing: {missing[:12]}")
    conn.execute("PRAGMA user_version = 1")
    logger.info("Adopted existing database as schema version 1")


def _apply_file(conn: sqlite3.Connection, version: int, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    if sql.lstrip().startswith(NO_TRANSACTION):
        try:
            conn.executescript(sql)
        except sqlite3.Error:
            conn.rollback()
            raise
        conn.execute(f"PRAGMA user_version = {version}")
    else:
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
    logger.info(f"Applied migration {path.name}")


def apply_pending(conn: sqlite3.Connection) -> int:
    """Brings a connection to the latest schema with no backup, so it's for new or throwaway databases."""
    if current_version(conn) == 0 and _columns_by_table(conn):
        adopt_baseline(conn)
    for version, path in load_migrations():
        if version > current_version(conn):
            _apply_file(conn, version, path)
    return current_version(conn)


# The row count check compares by table name, so a rename has to be declared here.
TABLE_RENAMES = {
    14: {
        "models": "collections",
        "model_tags": "collection_tags",
        "catalog_models": "models",
        "catalog_model_tags": "model_tags",
        "catalog_model_collections": "model_collections",
    },
}


def require_current(conn: sqlite3.Connection) -> None:
    """Refuses to start on any other schema version, including a newer one.

    Raises:
        MigrationError: The database can't be brought to this schema safely.
    """
    current, latest = current_version(conn), latest_version()
    if current != latest:
        raise MigrationError(
            f"Database schema is version {current}, and this app needs {latest}. "
            "Run: uv run python -m desync_archival.dbtool migrate"
        )


def _beside(db_path: Path, suffix: str) -> Path:
    return db_path.with_name(db_path.name + suffix)


def _remove_copy(work: Path) -> None:
    for path in (work, _beside(work, "-wal"), _beside(work, "-shm")):
        path.unlink(missing_ok=True)


def _swap_in(work: Path, db_path: Path) -> None:
    shutil.copymode(db_path, work)
    # Read-only connections leave empty ones behind. A stale -wal beside the new file would be
    # replayed into it, so they go first.
    for suffix in ("-wal", "-shm"):
        _beside(work, suffix).unlink(missing_ok=True)
        _beside(db_path, suffix).unlink(missing_ok=True)
    os.replace(work, db_path)


def _check_row_counts(work: Path, before_counts: dict[str, int], before_version: int, backup_path: Path | None) -> None:
    after_counts = backup.verify(work)
    renames: dict[str, str] = {
        old: new for version, table_map in TABLE_RENAMES.items() if before_version < version for old, new in table_map.items()
    }
    changed = sorted(table for table, count in before_counts.items() if after_counts.get(renames.get(table, table)) != count)
    if changed:
        raise MigrationError(f"Row counts changed during migration: {changed}. Backup: {backup_path}")


def migrate_database(db_path: Path, confirm_production: bool = False) -> dict[str, object]:
    """Backs up, migrates a copy, checks it, and only then swaps it in.

    Steps commit as they go, so checking in place would be too late: a failure would leave the new
    schema behind, a rerun would report nothing to do, and the app would start on it.

    The swap isn't safe beside a running app, whose connections would keep the old file. dbtool's
    port check is the real guard against that, and the -wal check below only catches some cases.

    Raises:
        MigrationError: The checks failed, or the database looks open elsewhere. Either way the
            original is untouched.
    """
    if app_config.is_production() and not confirm_production:
        raise MigrationError("Refusing to migrate production without --confirm-production")
    # Resolved so the swap replaces the real file, and not a link to it.
    db_path = Path(db_path).resolve()
    app_config.check_path(db_path)

    conn = sqlite3.connect(db_path)
    try:
        before_version = current_version(conn)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if before_version > latest_version():
        raise MigrationError(f"Database version {before_version} is newer than this app")
    if before_version == latest_version():
        return {"from": before_version, "to": before_version, "backup": None}

    # The last connection to close removes the -wal file, and ours just closed. One still here means
    # another process is using the database. Its absence proves nothing, because connections close
    # after each block now, so an idle app leaves no -wal.
    if _beside(db_path, "-wal").exists():
        raise MigrationError(f"{db_path.name} is still open in another process. Stop it first.")

    before_counts = backup.row_counts(db_path)
    backup_path = backup.create_backup(db_path) if before_counts else None
    work = _beside(db_path, ".migrating")
    _remove_copy(work)
    try:
        backup.copy_database(db_path, work, journal_mode)
        conn = sqlite3.connect(work)
        try:
            after_version = apply_pending(conn)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
        if broken:
            raise MigrationError(f"{len(broken)} broken references after migration. Backup: {backup_path}")
        _check_row_counts(work, before_counts, before_version, backup_path)
        _swap_in(work, db_path)
    finally:
        _remove_copy(work)
    logger.info(f"Migrated {db_path.name} from version {before_version} to {after_version}")
    return {"from": before_version, "to": after_version, "backup": backup_path}
