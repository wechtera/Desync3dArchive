"""Numbered .sql migrations tracked with PRAGMA user_version.

The app never migrates a database on its own. If the schema is behind, it refuses to start, and
applying migrations is an explicit command. On production that command also needs a confirmation.
"""

import re
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
    current, latest = current_version(conn), latest_version()
    if current != latest:
        raise MigrationError(
            f"Database schema is version {current}, and this app needs {latest}. "
            "Run: uv run python -m desync_archival.dbtool migrate"
        )


def migrate_database(db_path: Path, confirm_production: bool = False) -> dict[str, object]:
    """Backs up, applies pending migrations and checks that no table lost or gained rows."""
    if app_config.is_production() and not confirm_production:
        raise MigrationError("Refusing to migrate production without --confirm-production")
    app_config.check_path(db_path)

    conn = sqlite3.connect(db_path)
    try:
        before_version = current_version(conn)
        if before_version > latest_version():
            raise MigrationError(f"Database version {before_version} is newer than this app")
        if before_version == latest_version():
            return {"from": before_version, "to": before_version, "backup": None}

        before_counts = backup.row_counts(db_path)
        backup_path = backup.create_backup(db_path) if before_counts else None
        after_version = apply_pending(conn)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise MigrationError(f"{len(broken)} broken references after migration. Backup: {backup_path}")
    finally:
        conn.close()

    after_counts = backup.verify(db_path)
    renames = {old: new for version, table_map in TABLE_RENAMES.items() if before_version < version for old, new in table_map.items()}
    changed = sorted(t for t, count in before_counts.items() if after_counts.get(renames.get(t, t)) != count)
    if changed:
        raise MigrationError(f"Row counts changed during migration: {changed}. Backup: {backup_path}")
    logger.info(f"Migrated {db_path.name} from version {before_version} to {after_version}")
    return {"from": before_version, "to": after_version, "backup": backup_path}
