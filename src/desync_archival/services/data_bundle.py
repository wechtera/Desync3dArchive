"""Moves the app's data to another machine as one folder.

Files on the NAS and in linked libraries don't travel. They're reached by path, which is why an
import can rewrite stored path prefixes.
"""

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from desync_archival import backup
from desync_archival import settings as app_config
from desync_archival.db.queries import data_bundle as queries
from desync_archival.services.logger import logger

DATABASE = "library.db"
METADATA = "metadata.tar.gz"
PREVIEWS = "previews.tar"  # Already JPEGs, so compressing 4 GB would take time and save nothing.
SECRETS = "secrets.json"
MANIFEST = "MANIFEST.json"
IMPORT_PREFIX = ".import-"
PATH_SETTINGS = ("download_root_path", "download_work_path", "stash_download_path", "metadata_backup_path")
# Every column that can hold an absolute path outside the data folder. A test fails when a new
# path-like column is on neither this list nor the test's list of relative ones.
PATH_COLUMNS = (
    ("collections", "raw_backup_path"),
    ("collections", "vault_path"),
    ("deleted_files", "original_path"),
    ("deleted_files", "pending_path"),
    ("download_jobs", "destination_path"),
    ("downloaded_files", "staging_path"),
    ("downloaded_files", "vault_path"),
    ("external_libraries", "path"),
)


class BundleError(RuntimeError):
    """The bundle can't be written or used. Nothing was changed."""


def stored_roots(db_path: Path) -> dict[str, list[str]]:
    """Lists stored path prefixes, so the operator knows what --map has to cover."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(PATH_SETTINGS))
        return {
            "libraries": [r[0] for r in conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_PATH).fetchall()],
            "settings": [r[0] for r in conn.execute(
                f"SELECT DISTINCT value FROM app_settings WHERE key IN ({marks}) AND value != ''", PATH_SETTINGS,
            ).fetchall()],
        }
    finally:
        conn.close()


def export_bundle(db_path: Path, destination: Path, with_secrets: bool = False) -> dict[str, Any]:
    """Writes the catalog, metadata and previews to an empty folder, with secrets only on request.

    Raises:
        BundleError: The bundle can't be written or used.
    """
    if destination.exists() and any(destination.iterdir()):
        raise BundleError(f"{destination} isn't empty.")
    data = app_config.data_dir().resolve()
    if destination.resolve() == data or data in destination.resolve().parents:
        raise BundleError("The bundle has to go outside the data folder.")
    destination.mkdir(parents=True, exist_ok=True)

    database = destination / DATABASE
    backup.copy_database(db_path, database, journal_mode="DELETE")
    counts = backup.verify(database)
    with tarfile.open(destination / METADATA, "w:gz") as archive:
        if app_config.metadata_dir().is_dir():
            archive.add(app_config.metadata_dir(), arcname="metadata")
    with tarfile.open(destination / PREVIEWS, "w") as archive:
        if app_config.previews_dir().is_dir():
            archive.add(app_config.previews_dir(), arcname="previews")
    if with_secrets and app_config.secrets_file().is_file():
        # FIXME(S-07): world-readable until chmod below, and chmod may do nothing on a NAS share.
        shutil.copyfile(app_config.secrets_file(), destination / SECRETS)
        (destination / SECRETS).chmod(0o600)

    manifest = {
        "created_at": datetime.now(tz=UTC).isoformat(), "rows": sum(counts.values()),
        "has_secrets": with_secrets, "stored_roots": stored_roots(database),
    }
    (destination / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info(f"Exported a data bundle to {destination}")
    return {"success": True, "destination": str(destination), **manifest}


def _rewrite_prefix(conn: sqlite3.Connection, table: str, column: str, old: str, new: str, where: str = "") -> int:
    clause = f"{column} = ?1 OR substr({column}, 1, length(?1) + 1) = ?1 || '/'"
    return conn.execute(
        f"UPDATE {table} SET {column} = ?2 || substr({column}, length(?1) + 1) WHERE ({clause}){where};", (old, new),
    ).rowcount


def _moved(value: Any, old: str, new: str) -> Any:
    """Rewrites path strings anywhere inside decoded JSON."""
    if isinstance(value, str):
        return new + value[len(old):] if value == old or value.startswith(f"{old}/") else value
    if isinstance(value, dict):
        return {key: _moved(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_moved(item, old, new) for item in value]
    return value


def _rewrite_snapshots(conn: sqlite3.Connection, old: str, new: str) -> int:
    """A held file's snapshot is what a restore puts back, so its paths have to move with it."""
    changed = 0
    for row_id, text in conn.execute(queries.SELECT_DELETED_FILE_SNAPSHOTS).fetchall():
        snapshot = json.loads(text)
        moved = _moved(snapshot, old, new)
        if moved != snapshot:
            conn.execute(queries.UPDATE_DELETED_FILE_SNAPSHOT, (json.dumps(moved), row_id))
            changed += 1
    return changed


def _rewrite_paths(conn: sqlite3.Connection, old: str, new: str) -> int:
    changed = sum(_rewrite_prefix(conn, table, column, old, new) for table, column in PATH_COLUMNS)
    changed += _rewrite_snapshots(conn, old, new)
    settings_only = f" AND key IN ({','.join(repr(key) for key in PATH_SETTINGS)})"
    return changed + _rewrite_prefix(conn, "app_settings", "value", old, new, settings_only)


def _unpack(bundle: Path, staging: Path, path_map: dict[str, str]) -> int:
    backup.restore(bundle / DATABASE, staging / DATABASE)
    for name in (METADATA, PREVIEWS):
        if (bundle / name).is_file():
            with tarfile.open(bundle / name) as archive:
                archive.extractall(staging, filter="data")
    conn = sqlite3.connect(staging / DATABASE)
    try:
        # Longest first, or /Volumes/STL would take the paths /Volumes/STL/Loot was meant to move.
        ordered = sorted(path_map.items(), key=lambda pair: len(pair[0].rstrip("/")), reverse=True)
        with conn:
            return sum(_rewrite_paths(conn, old.rstrip("/"), new.rstrip("/")) for old, new in ordered)
    finally:
        conn.close()


def _move_into_place(staging: Path, data_dir: Path) -> None:
    # The catalog goes last: it's what makes a rerun refuse, so it only appears once the rest is in.
    for path in sorted(staging.iterdir(), key=lambda p: p.name == DATABASE):
        target = data_dir / path.name
        if target.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
        elif path.name in (DATABASE, "metadata", "previews"):
            os.replace(path, target)


def parse_path_map(values: list[str]) -> dict[str, str]:
    """Reads `--map OLD=NEW` values.

    Raises:
        BundleError: A value isn't OLD=NEW.
    """
    path_map = {}
    for value in values:
        old, separator, new = value.partition("=")
        if not (separator and old and new):
            raise BundleError(f"--map needs OLD=NEW, and got {value!r}")
        path_map[old] = new
    return path_map


def import_bundle(bundle: Path, data_dir: Path, path_map: dict[str, str]) -> dict[str, Any]:
    """Sets up an empty data folder from a bundle. It refuses to replace an existing catalog.

    Everything is built in a temp folder inside the data folder, so a failure leaves no catalog
    behind and the same command can run again.

    Raises:
        BundleError: The bundle can't be written or used.
    """
    app_config.check_path(data_dir)
    if not (bundle / MANIFEST).is_file() or not (bundle / DATABASE).is_file():
        raise BundleError(f"{bundle} isn't a data bundle.")
    target = data_dir / DATABASE
    if target.exists():
        raise BundleError(f"{target} already exists. Import only goes into an empty data folder.")
    data_dir.mkdir(parents=True, exist_ok=True)
    # A killed import leaves one of these, with a full copy of the catalog inside.
    for leftover in data_dir.glob(f"{IMPORT_PREFIX}*"):
        shutil.rmtree(leftover, ignore_errors=True)
    staging = Path(tempfile.mkdtemp(dir=data_dir, prefix=IMPORT_PREFIX))
    try:
        changed = _unpack(bundle, staging, path_map)
        _move_into_place(staging, data_dir)
    except (OSError, EOFError, ValueError, sqlite3.Error, tarfile.TarError, backup.BackupError) as e:
        raise BundleError(f"Import failed, so no catalog was added: {e}") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    logger.info(f"Imported a data bundle into {data_dir}, {changed} paths rewritten")
    return {"success": True, "paths_rewritten": changed, "secrets_in_bundle": (bundle / SECRETS).is_file()}
