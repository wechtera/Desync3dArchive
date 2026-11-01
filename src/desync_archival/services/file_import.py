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
import time
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

PDF = ".pdf"
IMPORTABLE = {*SUPPORTED_FILE_EXTENSIONS, PDF}
CHUNK = 8 * 1024 * 1024
# Peeking the archives takes the rest of the bar.
BYTES_SHARE = 95


class ImportRefused(ValueError):
    """Nothing was imported. The message is shown to the user."""


class ImportCancelled(Exception):
    """Stopped by the operator. Groups already finished stay imported."""


class _Progress:
    """Counts bytes, because one large archive is otherwise a bar that sits at zero for minutes."""

    def __init__(self, total: int, report: Callable[[float, str], None], cancelled: Callable[[], bool]) -> None:
        self.total = max(total, 1)
        self.done = 0
        self.report = report
        self.cancelled = cancelled
        self.last = 0.0

    def advance(self, count: int, label: str) -> None:
        if self.cancelled():
            raise ImportCancelled
        self.done += count
        now = time.monotonic()
        if now - self.last >= 1:
            self.last = now
            self.report(min(self.done / self.total, 1) * BYTES_SHARE, label)


def _hash(path: Path, progress: _Progress) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            progress.advance(len(chunk), f"Checking {path.name[:50]}")
    return digest.hexdigest()


def _copy(source: Path, target: Path, progress: _Progress) -> None:
    # The name only appears when the copy is whole, so a stopped import leaves no half file.
    partial = target.with_name(f"{target.name}.partial")
    try:
        with source.open("rb") as reader, partial.open("wb") as writer:
            while chunk := reader.read(CHUNK):
                writer.write(chunk)
                progress.advance(len(chunk), f"Copying {source.name[:50]}")
        partial.rename(target)
    finally:
        partial.unlink(missing_ok=True)


def _vault_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute(queries.SELECT_VAULT_ROOT).fetchone()
    root = Path(row[0]) if row and row[0] else None
    if root is None or not root.is_dir():
        raise ImportRefused("The vault isn't available. Check the storage path in Settings, and that the NAS is mounted.")
    return root


def import_root(conn: sqlite3.Connection) -> Path:
    return _vault_root(conn) / IMPORT_FOLDER


def _is_supported(path: Path) -> bool:
    return path.suffix.lower() in IMPORTABLE and not path.name.startswith(".")


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
    log: Callable[[str], None], progress: _Progress,
) -> dict[str, Any]:
    fresh, repeats = [], []
    for source, rel_path in files:
        digest = _hash(source, progress)
        twin = conn.execute(queries.SELECT_FILE_BY_HASH, (digest,)).fetchone()
        if twin or any(digest == d for _, _, d in fresh):
            repeats.append(source.name)
            progress.advance(source.stat().st_size, f"Skipping {source.name[:50]}")
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
        _copy(source, target, progress)
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
            elif target.suffix.lower() != PDF:
                loose.append((file_id, target, rel_path, size))
        if loose:
            catalog_levels.build_loose_models(conn, collection_id, "" if archives else label, loose)

    peeker = ArchivePeeker(db_path)
    read = 0
    for file_id, target in archives:
        progress.report(BYTES_SHARE, f"Reading the contents of {target.name[:50]}")
        result = peeker.inspect_and_record(collection_id, file_id, target)
        if result.get("success"):
            read += 1
        else:
            log(f"{target.name} was imported, but its contents couldn't be read: {result.get('error')}")
    log_layer("storage", 20, f"Imported {label} as collection #{collection_id}: {len(copied)} files, {read} archives read")
    return {"imported": len(copied), "repeats": len(repeats), "archives": read, "collection_id": collection_id, "name": label}


def import_paths(
    conn: sqlite3.Connection, db_path: Path, paths: list[Path], creator: str = "",
    log: Callable[[str], None] = lambda _: None, progress: Callable[[float, str], None] = lambda _p, _l: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    root = import_root(conn)
    groups = [(p, [(f, str(f.relative_to(p))) for f in _files_under(p)] if p.is_dir() else [(p, p.name)]) for p in paths]
    size = sum(f.stat().st_size for _, files in groups for f, _ in files)
    root.mkdir(exist_ok=True)
    if size > shutil.disk_usage(root).free:
        raise ImportRefused(f"This needs {size:,} bytes and the vault doesn't have that much free.")

    # Every byte is read twice, once to hash it and once to copy it.
    meter = _Progress(size * 2, progress, cancelled)
    totals = {"collections": 0, "imported": 0, "repeats": 0, "archives": 0}
    for path, files in groups:
        if not files:
            log(f"{path.name} has no printable files or archives.")
            continue
        result = _import_group(conn, db_path, root, path.name if path.is_dir() else path.stem, creator, files, log, meter)
        for key in ("imported", "repeats"):
            totals[key] += result[key]
        totals["archives"] += result.get("archives", 0)
        totals["collections"] += 1 if result["imported"] else 0
        log(f"{path.name}: {result['imported']} added, {result['repeats']} already in the catalog.")
    if totals["archives"]:
        log(f"{totals['archives']} archives are waiting in Archive Review.")
    return totals
