"""PDFs across every source, as a list that reads like models.

The list comes only from SQLite and the local preview folder, so it works with the NAS offline.
The NAS is read when a linked library is inventoried, when previews are made, and when a PDF is opened.
"""

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from desync_archival import external
from desync_archival import settings as app_config
from desync_archival.db.queries import documents as queries
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.external_indexer import key_for, release_month_of
from desync_archival.services.logger import log_layer
from desync_archival.services.stash_service import StashService

PAGE_SIZE = 48
PREVIEW_FOLDER = "documents"
CACHE_FOLDER = "document_cache"
ON_DISK = "on_disk"
IN_ARCHIVE = "in_archive"
NOT_DOWNLOADED = "not_downloaded"
UNREAD = "unread"
SOURCE_LABELS = {"mmf": "MyMiniFactory", "external": "Linked library", "document": "Linked library", "import": "Ad-hoc import"}
# Checked in order, so a licence inside an adventure folder is still a licence.
KINDS = (
    ("Licence", r"\beula\b|licen[cs]e|terms"),
    ("Painting guide", r"paint|colou?r|scheme"),
    ("Assembly guide", r"assembl|instruction|print(ing)?[ _-]?(guide|setting)|how[ _-]?to"),
    ("Adventure", r"adventure|module|campaign|one[ _-]?shot|quest|scenario|encounter"),
    ("Rules", r"rule|stat|5e|d&d|dnd|pathfinder|faction|unit|builder|lore|bestiary"),
    ("Magazine", r"magazine|[ _-]mag[ _.-]|\bbooks?\b|wanted"),
)
OTHER = "Other"


class DocumentUnavailable(Exception):
    """The message is shown to the user, and the code becomes the response status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _preview_dir() -> Path:
    return app_config.previews_dir() / PREVIEW_FOLDER


def _kind(text: str) -> str:
    lowered = text.lower()
    return next((kind for kind, words in KINDS if re.search(words, lowered)), OTHER)


def _title(filename: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", Path(filename).stem)).strip() or filename


def _rows(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    for query in (queries.SELECT_PDF_FILES, queries.SELECT_PDF_MEMBERS, queries.SELECT_UNREAD_PDF_PRODUCTS):
        for row in conn.execute(query):
            member = row["member_id"]
            pdf = row["filename"].lower().endswith(".pdf")
            if member:
                state, name = IN_ARCHIVE if row["disk_path"] else NOT_DOWNLOADED, Path(row["internal_path"]).name
            elif pdf:
                state, name = ON_DISK if row["disk_path"] else NOT_DOWNLOADED, row["filename"]
            else:
                state, name = UNREAD, row["filename"]
            yield {
                "key": f"member-{member}" if member else f"file-{row['file_id']}",
                "title": _title(name) if pdf or member else row["collection"],
                "filename": name,
                "kind": _kind(f"{name} {row['collection']}"),
                "creator": row["creator"] or "",
                "collection": row["collection"],
                "collection_id": row["collection_id"],
                "source": SOURCE_LABELS.get(row["source"], row["source"]),
                "size_bytes": row["size_bytes"] or 0,
                "state": state,
                "cover": row["primary_image_url"] or "",
                "file_id": row["file_id"],
                "member_id": member,
                "submodel_id": row["submodel_id"] if member else None,
                "disk_path": row["disk_path"],
                "internal_path": row["internal_path"],
            }


def _made_previews() -> dict[str, int]:
    try:
        return {e.name.rsplit(".", 1)[0]: e.stat().st_size for e in os.scandir(_preview_dir())}
    except OSError:
        return {}


def listing(
    conn: sqlite3.Connection, search: str = "", kind: str = "", creator: str = "", source: str = "", state: str = "", page: int = 1,
) -> dict[str, Any]:
    everything = list(_rows(conn))
    words = search.lower().split()
    def wanted(row: dict[str, Any]) -> bool:
        text = f"{row['title']} {row['filename']} {row['collection']} {row['creator']}".lower()
        return (
            all(word in text for word in words) and kind in ("", row["kind"]) and creator in ("", row["creator"])
            and source in ("", row["source"]) and state in ("", row["state"])
        )
    found = sorted(filter(wanted, everything), key=lambda r: (r["title"].lower(), r["key"]))
    previews = _made_previews()
    shown = found[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    public = ("disk_path", "internal_path", "file_id", "member_id", "submodel_id")
    documents = [
        {
            **{k: v for k, v in row.items() if k not in public},
            "preview_url": f"/previews/{PREVIEW_FOLDER}/{row['key']}.jpg" if previews.get(row["key"]) else row["cover"],
        }
        for row in shown
    ]
    return {
        "documents": documents, "total": len(found), "page": page, "page_size": PAGE_SIZE,
        "kinds": dict(sorted(Counter(r["kind"] for r in everything).items())),
        "creators": sorted({r["creator"] for r in everything if r["creator"]}, key=str.lower),
        "sources": sorted({r["source"] for r in everything}),
        "previews_waiting": sum(1 for r in everything if r["state"] in (ON_DISK, IN_ARCHIVE) and r["key"] not in previews),
    }


def _find(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = next((r for r in _rows(conn) if r["key"] == key), None)
    if row is None:
        raise DocumentUnavailable("That PDF isn't in the catalog.", 404)
    return row


def _extract(row: dict[str, Any], archive: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="desync-pdf-") as scratch:
        found = ArchivePeeker(app_config.db_path()).extract_member(archive, row["internal_path"], Path(scratch))
        if not found:
            raise DocumentUnavailable("That PDF couldn't be read from its archive.", 500)
        shutil.move(str(found), target)


def locate(conn: sqlite3.Connection, key: str) -> Path:
    """Where the PDF is, in the local cache if it came out of an archive before."""
    row = _find(conn, key)
    if row["state"] in (NOT_DOWNLOADED, UNREAD):
        raise DocumentUnavailable("This hasn't been downloaded yet. Queue it for download first.", 409)
    cached = app_config.data_dir() / CACHE_FOLDER / f"{key}.pdf"
    if row["member_id"] and cached.is_file():
        return cached
    path = Path(row["disk_path"])
    if not path.is_file():
        raise DocumentUnavailable("The file isn't reachable. The NAS may be offline.", 503)
    if not row["member_id"]:
        return path
    _extract(row, path, cached)
    return cached


def add_to_stash(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = _find(conn, key)
    if row["state"] in (NOT_DOWNLOADED, UNREAD):
        raise DocumentUnavailable("This hasn't been downloaded yet, so it can't be stashed.", 409)
    service = StashService()
    stash = service.get_active_stash(conn=conn)
    if not stash:
        raise DocumentUnavailable("Pick an active stash first.", 400)
    try:
        service.add_to_stash(
            stash["id"], "archive_member" if row["member_id"] else "file", row["collection_id"], file_id=row["file_id"],
            submodel_id=row["submodel_id"], archive_member_id=row["member_id"], conn=conn,
        )
    except ValueError as e:
        raise DocumentUnavailable(str(e), 400) from e
    log_layer("sql", logging.INFO, f"Added PDF {key} to stash #{stash['id']}")
    return {"stash_id": stash["id"], "stash_name": stash.get("name", "")}


def _release_collection(conn: sqlite3.Connection, library: sqlite3.Row, root: Path, release: str) -> int:
    # Apart from the release's model collections on purpose. A rescan rebuilds those from the folder,
    # and would pull every STL in the release into a collection sitting at the top of it.
    key = f"doc/{key_for(library['id'], release)}"
    found = conn.execute(queries.SELECT_DOCUMENT_COLLECTION, (key,)).fetchone()
    if found:
        return int(found[0])
    creator = library["default_creator"] or library["name"]
    return int(conn.execute(queries.INSERT_DOCUMENT_COLLECTION, (
        library["id"], f"{release} PDFs", creator, creator, release_month_of(release), str(root / release), key,
    )).lastrowid or 0)


def _pdfs_under(root: Path, cancelled: Callable[[], bool]) -> Iterator[Path]:
    for folder, names, files in os.walk(root):
        if cancelled():
            return
        names[:] = sorted(n for n in names if not n.startswith("."))
        for name in sorted(files):
            if name.lower().endswith(".pdf") and not name.startswith("."):
                yield Path(folder) / name


def inventory_library(
    conn: sqlite3.Connection, library: sqlite3.Row, log: Callable[[str], None], cancelled: Callable[[], bool] = lambda: False,
) -> int:
    """Records PDFs a scan leaves out. Rows are only added, so an offline mount can't remove any."""
    root = Path(library["path"])
    if not root.is_dir():
        log(f"{library['name']} is offline, so it was left as it is.")
        return 0
    prefix = key_for(library["id"], "")
    known = {r[0] for r in conn.execute(queries.SELECT_LIBRARY_PDF_KEYS, (f"{prefix}%",))}
    added = 0
    for path in _pdfs_under(root, cancelled):
        rel_path = path.relative_to(root)
        key = key_for(library["id"], str(rel_path))
        if key in known or len(rel_path.parts) < 2:
            continue
        size = path.stat().st_size
        with conn:
            collection = _release_collection(conn, library, root, rel_path.parts[0])
            file_id = int(conn.execute(queries.INSERT_MODEL_FILE, (collection, path.name, size, key)).lastrowid or 0)
            conn.execute(queries.INSERT_DOWNLOADED_FILE, (
                file_id, collection, path.name, path.name, f"files/{path.name}", size, size, str(path),
            ))
        added += 1
    log(f"{library['name']}: {added} new PDFs recorded.")
    log_layer("storage", logging.INFO, f"PDF inventory of {library['name']}: {added} added")
    return added


def inventory_one(
    conn: sqlite3.Connection, library_id: int, log: Callable[[str], None], cancelled: Callable[[], bool],
) -> int:
    conn.row_factory = sqlite3.Row
    library = next((r for r in conn.execute(queries.SELECT_ACTIVE_LIBRARIES).fetchall() if r["id"] == library_id), None)
    return inventory_library(conn, library, log, cancelled) if library else 0


def _render(source: Path, target: Path) -> bool:
    command = external.pdf_page_command(source, target)
    if not command:
        return False
    try:
        external.run(command, external.MEMBER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log_layer("storage", logging.WARNING, f"Preview failed for {source.name}: {e}")
        return False
    return target.is_file() and target.stat().st_size > 0


def make_previews(
    conn: sqlite3.Connection, log: Callable[[str], None], progress: Callable[[float, str], None], cancelled: Callable[[], bool],
) -> dict[str, int]:
    """First pages, stored locally so showing them never needs the NAS."""
    folder = _preview_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if not external.find_ghostscript():
        log("Ghostscript isn't installed, so PDFs keep their collection's picture.")
        return {"made": 0, "failed": 0}
    done = _made_previews()
    waiting = [r for r in _rows(conn) if r["state"] in (ON_DISK, IN_ARCHIVE) and r["key"] not in done]
    made = failed = 0
    for index, row in enumerate(waiting, 1):
        if cancelled():
            break
        progress(index / len(waiting) * 100, f"Preview {index}/{len(waiting)}: {row['filename'][:45]}")
        target = folder / f"{row['key']}.jpg"
        try:
            ok = _render(locate(conn, row["key"]), target)
        except DocumentUnavailable as e:
            # Unreachable isn't a bad PDF, so it stays waiting for the next run.
            log(f"{row['filename']}: {e}")
            failed += 1
            continue
        made += int(ok)
        if not ok:
            failed += 1
            # An empty file marks a PDF that can't be drawn, so it isn't retried every time.
            target.touch()
    return {"made": made, "failed": failed}


def update(
    conn: sqlite3.Connection, log: Callable[[str], None], progress: Callable[[float, str], None], cancelled: Callable[[], bool],
) -> dict[str, int]:
    conn.row_factory = sqlite3.Row
    added = sum(inventory_library(conn, lib, log, cancelled) for lib in conn.execute(queries.SELECT_ACTIVE_LIBRARIES).fetchall())
    return {"added": added, **make_previews(conn, log, progress, cancelled)}
