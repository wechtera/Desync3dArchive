"""Verified backups of library.db, and restores to a new path."""

import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from desync_archival import settings as app_config
from desync_archival.services.logger import logger

BACKUP_PATTERN = re.compile(r"library-\d{8}-\d{6}-\d{6}\.db")
DEFAULT_RETENTION = 14


class BackupError(RuntimeError):
    """A backup or restore failed verification, so it must not be trusted."""


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def row_counts(path: Path) -> dict[str, int]:
    conn = _open_read_only(path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for (name,) in tables}
    finally:
        conn.close()


def verify(path: Path, expected: dict[str, int] | None = None) -> dict[str, int]:
    """Checks integrity and, when given, row counts. Returns the counts found."""
    conn = _open_read_only(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise BackupError(f"Integrity check failed for {path.name}: {result}")

    counts = row_counts(path)
    if expected is not None and counts != expected:
        changed = sorted(t for t in expected.keys() | counts.keys() if expected.get(t) != counts.get(t))
        raise BackupError(f"Row counts differ for {path.name}: {changed}")
    return counts


def _copy_database(source_path: Path, target_path: Path, journal_mode: str) -> None:
    """Copies with SQLite's backup API. Backups use DELETE mode so each one is a single file."""
    source = _open_read_only(source_path)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        target.execute(f"PRAGMA journal_mode = {journal_mode}")
    finally:
        target.close()
        source.close()


def backup_dir() -> Path:
    path = app_config.backup_dir()
    data = app_config.data_dir()
    if path == data or data in path.parents:
        raise BackupError("DESYNC_BACKUP_DIR must be outside the data directory")
    app_config.check_path(path)
    return path


def prune(directory: Path, keep: int) -> list[Path]:
    """Removes the oldest backups beyond keep. Only files with the backup name are touched."""
    backups = sorted(p for p in directory.iterdir() if p.is_file() and BACKUP_PATTERN.fullmatch(p.name))
    removed = backups[:-keep] if keep > 0 else []
    for path in removed:
        path.unlink()
    return removed


def create_backup(db_path: Path, keep: int = DEFAULT_RETENTION) -> Path:
    """Writes a verified 0600 copy of the database to the backup directory."""
    directory = backup_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = directory / f"library-{stamp}.db"
    temp = target.with_suffix(".partial")

    os.close(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        _copy_database(db_path, temp, journal_mode="DELETE")
        counts = verify(temp, expected=row_counts(db_path))
        temp.rename(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        logger.error(f"Backup of {db_path.name} failed")
        raise

    removed = prune(directory, keep)
    logger.info(
        f"Backup written: {target.name}, {len(counts)} tables, {sum(counts.values())} rows, "
        f"{len(removed)} old backups removed"
    )
    return target


def restore(backup_path: Path, target: Path) -> dict[str, int]:
    """Restores a backup to a new path. It never replaces an existing file."""
    app_config.check_path(target)
    if target.exists():
        raise BackupError(f"Refusing to overwrite {target}. Restore to a new path.")
    counts = verify(backup_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        _copy_database(backup_path, target, journal_mode="WAL")
        verify(target, expected=counts)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    logger.info(f"Restored {backup_path.name} to {target}: {sum(counts.values())} rows")
    return counts
