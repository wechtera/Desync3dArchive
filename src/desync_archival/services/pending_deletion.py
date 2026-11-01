"""Takes a file out of the catalog and holds it in the vault's .Pending-Deletion folder.

Nothing here erases a file straight away. A deleted file is tombstoned in deleted_files, moved
aside, and only a double confirmed purge can remove it, 14 days after it was moved.
"""

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from desync_archival.db.queries import pending_deletion as queries
from desync_archival.services import audit, inactive_log
from desync_archival.services.logger import log_layer
from desync_archival.services.paths import PENDING_DELETION_FOLDER

WAIT_DAYS = 14
DAY_SECONDS = 86400
LINKED_SOURCES = ("external", "document")


class DeleteRefused(ValueError):
    """Nothing changed. The message is shown to the user, and the code is the response status."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


def _vault_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute(queries.SELECT_VAULT_ROOT).fetchone()
    root = Path(row[0]) if row and row[0] else None
    if root is None or not root.is_dir():
        raise DeleteRefused("The vault is offline, so nothing can be moved.", 503)
    return root.resolve()


def can_delete(source: str) -> bool:
    return source not in LINKED_SOURCES


def _hold(root: Path, source: Path, tombstone: int) -> Path:
    target = root / PENDING_DELETION_FOLDER / str(tombstone) / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), target)
    # A move keeps the old modified date, which would make a two year old file purgeable today.
    os.utime(target)
    return target


def delete_file(conn: sqlite3.Connection, file_id: int) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    row = conn.execute(queries.SELECT_FILE, (file_id,)).fetchone()
    if not row:
        raise DeleteRefused("That file is no longer in the catalog.", 404)
    if not can_delete(row["source"]):
        raise DeleteRefused("Linked libraries are read-only, so this file can only be kept or ignored.")
    if conn.execute(queries.COUNT_STASH_ITEMS, (file_id,)).fetchone()[0]:
        raise DeleteRefused("This file is in a stash. Remove it from the stash first.", 409)

    stored = row["vault_path"] or row["staging_path"]
    source = Path(stored) if stored else None
    root = _vault_root(conn) if source else None
    if source and root:
        if not source.resolve().is_relative_to(root):
            raise DeleteRefused("This file isn't in the vault, so it can't be moved.")
        if not source.is_file():
            raise DeleteRefused("The file isn't at its recorded path. Run a reconciliation first.", 409)

    moved = None
    try:
        with conn:
            tombstone = int(conn.execute(queries.INSERT_TOMBSTONE, (
                file_id, row["collection_id"], row["collection_name"], row["filename"], stored or "",
                row["size_bytes"], row["sha256"], row["external_key"],
            )).lastrowid or 0)
            if source and root:
                moved = _hold(root, source, tombstone)
                conn.execute(queries.UPDATE_PENDING_PATH, (str(moved), tombstone))
            conn.execute(queries.DELETE_MODEL_FILE, (file_id,))
    except (OSError, sqlite3.Error) as e:
        # The catalog rolled back, so the file has to go back with it.
        if moved and source and moved.exists():
            shutil.move(str(moved), source)
        raise DeleteRefused(f"The file couldn't be moved: {e}", 500) from e

    result = {"success": True, "file_id": file_id, "filename": row["filename"], "pending_path": str(moved or "")}
    inactive_log.record(
        conn, inactive_log.FILE, file_id, row["filename"], inactive_log.DUPLICATE_CHECK, inactive_log.DELETED_DUPLICATE,
        f"Moved from {stored or 'the catalog'}",
    )
    conn.commit()
    audit.record(conn, "duplicate_delete", False, result)
    log_layer("storage", logging.WARNING, f"Deleted file #{file_id} ({row['filename']}) to pending deletion")
    return result


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    count, size = conn.execute(queries.SUM_PENDING).fetchone()
    return {"count": count, "size_bytes": size, "wait_days": WAIT_DAYS}


def _age_days(path: Path) -> float | None:
    try:
        return (time.time() - path.stat().st_mtime) / DAY_SECONDS
    except OSError:
        return None


def purge(conn: sqlite3.Connection, confirm: bool = False, ids: list[int] | None = None) -> dict[str, Any]:
    """Lists what could go. With confirm, removes only the listed files that are old enough."""
    conn.row_factory = sqlite3.Row
    root = (_vault_root(conn) / PENDING_DELETION_FOLDER).resolve()
    chosen = set(ids or [])
    ready, waiting = [], 0
    for row in conn.execute(queries.SELECT_PENDING).fetchall():
        path = Path(row["pending_path"])
        age = _age_days(path)
        if age is not None and age < WAIT_DAYS:
            waiting += 1
            continue
        if not path.resolve().is_relative_to(root):
            continue
        ready.append({
            "id": row["id"], "filename": row["filename"], "collection": row["collection_name"],
            "original_path": row["original_path"], "pending_path": str(path), "size_bytes": row["size_bytes"],
            "age_days": int(age or 0), "missing": age is None,
        })
    result: dict[str, Any] = {
        "success": True, "dry_run": not confirm, "items": ready, "waiting": waiting,
        "reclaimable_bytes": sum(i["size_bytes"] for i in ready), "wait_days": WAIT_DAYS,
    }
    if confirm:
        purged = []
        for item in ready:
            if item["id"] not in chosen:
                continue
            path = Path(item["pending_path"])
            path.unlink(missing_ok=True)
            if path.parent != root and path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
            with conn:
                conn.execute(queries.UPDATE_PURGED, (item["id"],))
            purged.append(item["filename"])
        result["purged"] = len(purged)
        log_layer("storage", logging.WARNING, f"Purged {len(purged)} files from pending deletion")
    audit.record(conn, "pending_deletion_purge", not confirm, {k: v for k, v in result.items() if k != "items"})
    return result
