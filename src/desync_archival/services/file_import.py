"""Brings loose files and folders from anywhere into the catalog, such as a Google Drive download.

They land in ad-hoc/ at the vault root, beside the creator folders and never inside one.
The location is fixed on purpose: it follows the vault root and has no setting of its own. A folder
becomes one collection and keeps its structure. A file picked on its own becomes its own collection.
Unlike linked libraries these files are hashed, because nothing else can tell a repeat apart.
"""

import hashlib
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import file_import as queries
from desync_archival.services import catalog_levels
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.external_indexer import (
    ARCHIVE_EXTENSIONS,
    SUPPORTED_FILE_EXTENSIONS,
)
from desync_archival.services.logger import log_layer
from desync_archival.services.paths import IMPORT_FOLDER

CHUNK = 1024 * 1024


class ImportRefused(ValueError):
    """Nothing was imported. The message is shown to the user."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _vault_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute(queries.SELECT_VAULT_ROOT).fetchone()
    root = Path(row[0]) if row and row[0] else None
    if root is None or not root.is_dir():
        raise ImportRefused("The vault isn't available. Check the storage path in Settings, and that the NAS is mounted.")
    return root


def import_root(conn: sqlite3.Connection) -> Path:
    return _vault_root(conn) / IMPORT_FOLDER


def _is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_FILE_EXTENSIONS and not path.name.startswith(".")


def check_paths(paths: list[str]) -> list[Path]:
    """Runs before the job starts, so a bad pick is refused at once and not found in a task log."""
    if not paths:
        raise ImportRefused("Choose at least one file or folder.")
    checked = []
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ImportRefused(f"{value} needs to be a full path.")
        if not app_config.is_allowed(path):
            raise ImportRefused(f"{path} isn't in an allowed folder. Add it to DESYNC_ALLOWED_ROOTS.")
        if not path.exists():
            raise ImportRefused(f"{path} doesn't exist.")
        if path.is_file() and not _is_supported(path):
            raise ImportRefused(f"{path.name} isn't a printable file or an archive.")
        checked.append(path)
    return checked


def _files_under(folder: Path) -> Iterator[Path]:
    for path in sorted(folder.rglob("*")):
        if path.is_file() and _is_supported(path) and not any(p.startswith(".") for p in path.relative_to(folder).parts):
            yield path


def _folder_name(label: str, marker: str) -> str:
    # The hash stays in the name so two different things called Dragon can't collide.
    return f"{re.sub(r'[^A-Za-z0-9._-]+', '_', label).strip('_')[:60] or 'import'}-{marker[:8]}"


def _import_group(
    conn: sqlite3.Connection, db_path: Path, root: Path, label: str, creator: str, files: list[tuple[Path, str]],
    log: Callable[[str], None],
) -> dict[str, Any]:
    fresh, repeats = [], []
    for source, rel_path in files:
        digest = _hash(source)
        twin = conn.execute(queries.SELECT_FILE_BY_HASH, (digest,)).fetchone()
        if twin or any(digest == d for _, _, d in fresh):
            repeats.append(source.name)
            continue
        fresh.append((source, rel_path, digest))
    if not fresh:
        return {"imported": 0, "repeats": len(repeats)}

    marker = hashlib.sha256("".join(d for _, _, d in fresh).encode()).hexdigest()
    folder = root / _folder_name(label, marker)
    copied = []
    for source, rel_path, digest in fresh:
        target = folder / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.name}.partial")
        shutil.copyfile(source, partial)
        partial.rename(target)
        copied.append((target, digest))

    creator = creator.strip() or "Imported"
    with conn:
        collection_id = int(conn.execute(queries.INSERT_COLLECTION, (
            label, creator, creator, datetime.now().strftime("%Y-%m"), str(folder), f"import/{marker}",
        )).lastrowid or 0)
        archives, loose = [], []
        for target, digest in copied:
            size = target.stat().st_size
            rel_path = str(target.relative_to(folder))
            file_id = int(conn.execute(queries.INSERT_MODEL_FILE, (
                collection_id, target.name, size, f"import/{folder.name}/{rel_path}",
            )).lastrowid or 0)
            conn.execute(queries.INSERT_DOWNLOADED_FILE, (
                file_id, collection_id, target.name, target.name, f"files/{target.name}", size, size,
                target.suffix.lower(), str(target), digest,
            ))
            if target.suffix.lower() in ARCHIVE_EXTENSIONS:
                archives.append((file_id, target))
            else:
                loose.append((file_id, target, rel_path, size))
        if loose:
            catalog_levels.build_loose_models(conn, collection_id, label, loose)

    peeker = ArchivePeeker(db_path)
    for file_id, target in archives:
        if not peeker.inspect_and_record(collection_id, file_id, target).get("success"):
            log(f"{target.name} was imported, but its contents couldn't be read.")
    log_layer("storage", 20, f"Imported {label} as collection #{collection_id}: {len(copied)} files")
    return {"imported": len(copied), "repeats": len(repeats), "collection_id": collection_id, "name": label}


def import_paths(
    conn: sqlite3.Connection, db_path: Path, paths: list[Path], creator: str = "",
    log: Callable[[str], None] = lambda _: None, progress: Callable[[float, str], None] = lambda _p, _l: None,
) -> dict[str, Any]:
    root = import_root(conn)
    totals = {"collections": 0, "imported": 0, "repeats": 0}
    for index, path in enumerate(paths, 1):
        progress((index - 1) / len(paths) * 100, f"[{index}/{len(paths)}] {path.name[:50]}")
        if path.is_dir():
            files = [(f, str(f.relative_to(path))) for f in _files_under(path)]
            if not files:
                log(f"{path.name} has no printable files or archives.")
                continue
        else:
            files = [(path, path.name)]
        result = _import_group(conn, db_path, root, path.name if path.is_dir() else path.stem, creator, files, log)
        totals["imported"] += result["imported"]
        totals["repeats"] += result["repeats"]
        totals["collections"] += 1 if result["imported"] else 0
        log(f"{path.name}: {result['imported']} added, {result['repeats']} already in the catalog.")
    return totals
