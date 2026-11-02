"""Verified backups of library.db, and restores to a new path."""

import os
import re
import shutil
import sqlite3
import tarfile
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


def copy_database(source_path: Path, target_path: Path, journal_mode: str) -> None:
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
    """Removes the oldest backups beyond keep, per kind. Only files with the backup name are touched.

    Quick safety copies hold no metadata, and a busy week makes many of them, so they get their own
    count. Sharing one would let them push out every monthly backup.
    """
    backups = sorted(p for p in directory.iterdir() if p.is_file() and BACKUP_PATTERN.fullmatch(p.name))
    full = [p for p in backups if metadata_copy(p).exists()]
    quick = [p for p in backups if not metadata_copy(p).exists()]
    removed = (full[:-keep] + quick[:-keep]) if keep > 0 else []
    for path in removed:
        path.unlink()
        secrets_copy(path).unlink(missing_ok=True)
        metadata_copy(path).unlink(missing_ok=True)
    return removed


def secrets_copy(backup_path: Path) -> Path:
    return backup_path.with_suffix(".secrets.json")


def metadata_copy(backup_path: Path) -> Path:
    return backup_path.with_suffix(".metadata.tar.gz")


def _archive_metadata(target: Path) -> None:
    source = app_config.metadata_dir()
    if not source.is_dir():
        return
    with tarfile.open(metadata_copy(target), "w:gz") as archive:
        archive.add(source, arcname="metadata")
    metadata_copy(target).chmod(0o600)


def mirror_backup(target: Path, keep: int = DEFAULT_RETENTION) -> str:
    """Copies a backup to the second location. Never raises: the local backup already succeeded.

    Secrets stay behind on purpose. A NAS share is a wider audience than this account's home
    folder, and losing cookies only means reloading them.
    """
    mirror = app_config.backup_mirror_dir()
    if mirror is None:
        return "off"
    # Only the last folder gets created. A missing parent means the share isn't mounted, and
    # writing there anyway would quietly fill the boot disk.
    if not mirror.parent.is_dir():
        logger.warning(f"Backup mirror skipped, {mirror.parent} is not available")
        return "offline"
    try:
        mirror.mkdir(exist_ok=True)
        partial = mirror / f"{target.stem}.partial"
        shutil.copyfile(target, partial)
        verify(partial)
        partial.rename(mirror / target.name)
        if metadata_copy(target).exists():
            shutil.copyfile(metadata_copy(target), metadata_copy(mirror / target.name))
        removed = prune(mirror, keep)
    except (OSError, sqlite3.Error, BackupError) as e:
        logger.warning(f"Backup mirror failed for {target.name}: {e}")
        return "failed"
    logger.info(f"Backup mirrored to {mirror}, {len(removed)} old copies removed")
    return "copied"


def list_backups() -> dict[str, object]:
    directory = backup_dir()
    mirror = app_config.backup_mirror_dir()
    online = bool(mirror and mirror.is_dir())
    names = lambda folder: {p.name for p in folder.iterdir() if BACKUP_PATTERN.fullmatch(p.name)}
    mirrored = names(mirror) if online and mirror else set()
    local = sorted(names(directory) if directory.is_dir() else set(), reverse=True)
    return {
        "backup_dir": str(directory), "mirror_dir": str(mirror) if mirror else "", "mirror_online": online,
        "backups": [
            {
                "name": name, "size_bytes": (directory / name).stat().st_size, "mirrored": name in mirrored,
                "has_metadata": metadata_copy(directory / name).exists(),
            }
            for name in local
        ],
    }


def check_backup(name: str) -> dict[str, int]:
    """Checks one backup by name, so a caller can't point this at an arbitrary path."""
    if not BACKUP_PATTERN.fullmatch(name):
        raise BackupError("That isn't a backup file name.")
    path = backup_dir() / name
    if not path.is_file():
        raise BackupError(f"{name} is not in the backup folder.")
    counts = verify(path)
    return {"tables": len(counts), "rows": sum(counts.values())}


def copy_private(source: Path, target: Path) -> None:
    """Copies secrets to a new file that's 0600 from the start. A chmod after can do nothing on a NAS share."""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, source.read_bytes())
    finally:
        os.close(descriptor)


def _copy_secrets(target: Path) -> None:
    source = app_config.secrets_file()
    if source.is_file():
        copy_private(source, secrets_copy(target))


def create_backup(db_path: Path, keep: int = DEFAULT_RETENTION, full: bool = False) -> Path:
    """Writes a verified 0600 copy of the database to the backup directory.

    full adds metadata and the mirror. Migrations skip those, because they need a quick safety
    copy, not a monthly archive.
    """
    directory = backup_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = directory / f"library-{stamp}.db"
    temp = target.with_suffix(".partial")

    os.close(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        copy_database(db_path, temp, journal_mode="DELETE")
        # Only the copy is checked. Comparing it with the live database fails on any write in between,
        # such as a download's audit row, and SQLite's backup API already gives one consistent snapshot.
        counts = verify(temp)
        temp.rename(target)
        _copy_secrets(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        logger.error(f"Backup of {db_path.name} failed")
        raise

    # Metadata first, because that file is how prune tells a full backup from a quick copy.
    if full:
        _archive_metadata(target)
    removed = prune(directory, keep)
    if full:
        mirror_backup(target, keep)
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
        copy_database(backup_path, target, journal_mode="WAL")
        verify(target, expected=counts)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    logger.info(f"Restored {backup_path.name} to {target}: {sum(counts.values())} rows")
    return counts
