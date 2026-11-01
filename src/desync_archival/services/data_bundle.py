"""Moves the app's data to another machine as one folder.

Files on the NAS and in linked libraries don't travel. They're reached by path, which is why an
import can rewrite stored path prefixes.
"""

import json
import shutil
import sqlite3
import tarfile
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
PATH_SETTINGS = ("download_root_path", "download_work_path", "stash_download_path", "metadata_backup_path")


class BundleError(RuntimeError):
    """The bundle can't be written or used. Nothing was changed."""


def stored_roots(db_path: Path) -> dict[str, list[str]]:
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
    if destination.exists() and any(destination.iterdir()):
        raise BundleError(f"{destination} isn't empty.")
    data = app_config.data_dir().resolve()
    if destination.resolve() == data or data in destination.resolve().parents:
        raise BundleError("The bundle has to go outside the data folder.")
    destination.mkdir(parents=True, exist_ok=True)

    database = destination / DATABASE
    backup._copy_database(db_path, database, journal_mode="DELETE")
    counts = backup.verify(database)
    with tarfile.open(destination / METADATA, "w:gz") as archive:
        if app_config.metadata_dir().is_dir():
            archive.add(app_config.metadata_dir(), arcname="metadata")
    with tarfile.open(destination / PREVIEWS, "w") as archive:
        if app_config.previews_dir().is_dir():
            archive.add(app_config.previews_dir(), arcname="previews")
    if with_secrets and app_config.secrets_file().is_file():
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


def import_bundle(bundle: Path, data_dir: Path, path_map: dict[str, str]) -> dict[str, Any]:
    """Sets up an empty data folder from a bundle. It refuses to replace an existing catalog."""
    app_config.check_path(data_dir)
    if not (bundle / MANIFEST).is_file() or not (bundle / DATABASE).is_file():
        raise BundleError(f"{bundle} isn't a data bundle.")
    target = data_dir / DATABASE
    if target.exists():
        raise BundleError(f"{target} already exists. Import only goes into an empty data folder.")
    data_dir.mkdir(parents=True, exist_ok=True)
    backup.restore(bundle / DATABASE, target)
    for name in (METADATA, PREVIEWS):
        if (bundle / name).is_file():
            with tarfile.open(bundle / name) as archive:
                archive.extractall(data_dir, filter="data")

    changed = 0
    conn = sqlite3.connect(target)
    try:
        with conn:
            for old, new in path_map.items():
                old, new = old.rstrip("/"), new.rstrip("/")
                changed += _rewrite_prefix(conn, "external_libraries", "path", old, new)
                changed += _rewrite_prefix(conn, "collections", "vault_path", old, new)
                changed += _rewrite_prefix(conn, "downloaded_files", "vault_path", old, new)
                changed += conn.execute(
                    f"UPDATE app_settings SET value = ?2 || substr(value, length(?1) + 1)"
                    f" WHERE key IN ({','.join(repr(k) for k in PATH_SETTINGS)}) AND (value = ?1 OR value LIKE ?1 || '/%');",
                    (old, new),
                ).rowcount
    finally:
        conn.close()
    logger.info(f"Imported a data bundle into {data_dir}, {changed} paths rewritten")
    return {"success": True, "paths_rewritten": changed, "secrets_in_bundle": (bundle / SECRETS).is_file()}
