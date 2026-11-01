"""Brings one loose STL or archive from anywhere into the catalog, such as a Google Drive download.

Each import is its own collection, under Imports/ in the vault. Unlike linked libraries these files
are hashed, because the file has no folder or MMF ID to tell a repeat by.
"""

import hashlib
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.services import catalog_levels
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.external_indexer import (
    ARCHIVE_EXTENSIONS,
    SUPPORTED_FILE_EXTENSIONS,
)
from desync_archival.services.logger import log_layer

IMPORT_FOLDER = "Imports"
CHUNK = 1024 * 1024


class ImportRefused(ValueError):
    """The file wasn't imported. The message is shown to the user."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _vault_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'download_root_path';").fetchone()
    root = Path(row[0]) if row and row[0] else None
    if root is None or not root.is_dir():
        raise ImportRefused("The vault isn't available. Check the storage path in Settings, and that the NAS is mounted.")
    return root


def import_file(conn: sqlite3.Connection, db_path: Path, source: Path, name: str = "", creator: str = "") -> dict[str, Any]:
    source = source.expanduser()
    if not source.is_absolute():
        raise ImportRefused("Use the file's full path.")
    if not app_config.is_allowed(source):
        raise ImportRefused(f"{source.parent} isn't an allowed folder. Add it to DESYNC_ALLOWED_ROOTS.")
    if not source.is_file():
        raise ImportRefused(f"{source} doesn't exist.")
    if source.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS:
        raise ImportRefused(f"{source.suffix or 'That file'} isn't a printable file or an archive.")

    root = _vault_root(conn)
    digest = _hash(source)
    twin = conn.execute(
        "SELECT d.model_id, c.name FROM downloaded_files d JOIN collections c ON c.id = d.model_id WHERE d.sha256 = ? LIMIT 1;",
        (digest,),
    ).fetchone()
    if twin:
        return {"success": False, "duplicate": True, "collection_id": twin[0], "error": f"Already in the catalog as {twin[1]}."}

    label = name.strip() or source.stem
    folder = root / IMPORT_FOLDER / digest[:16]
    target = folder / source.name
    folder.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.partial")
    shutil.copyfile(source, partial)
    partial.rename(target)

    size = target.stat().st_size
    rel_path = f"{digest[:16]}/{source.name}"
    is_archive = source.suffix.lower() in ARCHIVE_EXTENSIONS
    with conn:
        collection_id = int(conn.execute(
            """INSERT INTO collections (name, designer_name, designer_username, primary_category, release_month, vault_path,
               source, external_key, raw_backup_path, updated_at)
               VALUES (?, ?, ?, 'Imported', ?, ?, 'import', ?, '', CURRENT_TIMESTAMP);""",
            (label, creator.strip() or "Imported", creator.strip() or "Imported", datetime.now().strftime("%Y-%m"),
             str(folder), f"import/{digest}"),
        ).lastrowid or 0)
        file_id = int(conn.execute(
            """INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
               VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, 'import', ?);""",
            (collection_id, source.name, size, f"import/{rel_path}"),
        ).lastrowid or 0)
        conn.execute(
            """INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,
               expected_bytes, actual_bytes, file_extension, storage_tier, vault_path, sha256, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP);""",
            (file_id, collection_id, source.name, source.name, f"files/{source.name}", size, size,
             source.suffix.lower(), str(target), digest),
        )
        if not is_archive:
            stat = target.stat()
            catalog_levels.sync_loose_artifacts(
                conn, collection_id, label, root / IMPORT_FOLDER, [(target, stat.st_size, stat.st_mtime_ns)],
            )

    peek = ArchivePeeker(db_path).inspect_and_record(collection_id, file_id, target) if is_archive else None
    log_layer("storage", 20, f"Imported {source.name} as collection #{collection_id}")
    return {
        "success": True, "collection_id": collection_id, "name": label,
        "peeked": bool(peek and peek.get("success")) if is_archive else None,
    }
