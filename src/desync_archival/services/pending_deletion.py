"""Takes a file out of the catalog and holds it in the vault's .Pending-Deletion folder.

Nothing here erases a file straight away. A deleted file is tombstoned in deleted_files, moved
aside, and only a double confirmed purge can remove it, 14 days after it was moved. Until then
it can be restored to where it was, with the catalog rows the delete took with it.
"""

import json
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
# Parents first, because restoring goes through the foreign keys that deleting cascaded along.
SNAPSHOT_TABLES = ("model_files", "downloaded_files", "file_archive_submodels", "file_archive_members", "model_artifacts")
DAY_SECONDS = 86400
LINKED_SOURCES = ("external", "document")
LINKED_REASON = "Linked libraries are read-only, so this file can only be kept or ignored."
STASHED_REASON = "This file is in a stash. Remove it from the stash first."
OFFLINE_REASON = "The vault is offline, so nothing can be moved."
OUTSIDE_REASON = "This file isn't in the vault, so it can't be moved."
# An orphan folder has no catalog rows, so its tombstone points at no file or collection.
FOLDER_ID = 0
ORPHAN_FOLDER_LABEL = "Orphan folder"


class DeleteRefused(ValueError):
    """Nothing changed. The message is shown to the user, and the code is the response status."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


def _vault_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute(queries.SELECT_VAULT_ROOT).fetchone()
    root = Path(row[0]) if row and row[0] else None
    if root is None or not root.is_dir():
        raise DeleteRefused(OFFLINE_REASON, 503)
    return root.resolve()


def _vault_root_or_none(conn: sqlite3.Connection) -> Path | None:
    try:
        return _vault_root(conn)
    except DeleteRefused:
        return None


def mark_blocked(conn: sqlite3.Connection, pairs: list[dict[str, Any]], busy: str | None = None) -> list[dict[str, Any]]:
    """Adds why each file can't be deleted, so the page can say so before anyone clicks.

    delete_file still applies these itself, because a page can be out of date. The vault is only
    checked once, and paths are compared as text, so a long list doesn't walk the NAS.
    """
    stashed = {row[0] for row in conn.execute(queries.SELECT_STASHED_FILE_IDS)}
    root = _vault_root_or_none(conn)
    def reason(side: dict[str, Any]) -> str:
        if side["source"] in LINKED_SOURCES:
            return LINKED_REASON
        if side["file_id"] in stashed:
            return STASHED_REASON
        if busy:
            return busy
        if not side["path"]:
            return ""
        if root is None:
            return OFFLINE_REASON
        return "" if side["path"].startswith(f"{root}/") else OUTSIDE_REASON
    for pair in pairs:
        for side in (pair["first"], pair["second"]):
            side["delete_blocked"] = reason(side)
    return pairs


def _hold(root: Path, source: Path, tombstone: int) -> Path:
    target = root / PENDING_DELETION_FOLDER / str(tombstone) / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), target)
    # A move keeps the old modified date, which would make a two year old file purgeable today.
    # Business rule: the 14 day wait counts from this move.
    os.utime(target)
    return target


def _snapshot(conn: sqlite3.Connection, file_id: int, source: Path | None) -> str:
    rows: dict[str, Any] = {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE file_id = ?", (file_id,))]
        for table in SNAPSHOT_TABLES
    }
    rows["duplicate_decisions"] = [dict(row) for row in conn.execute(queries.SELECT_DECISIONS_OF_FILE, (file_id, file_id))]
    return json.dumps({"rows": rows, "modified": source.stat().st_mtime if source else None}, default=str)


def delete_file(conn: sqlite3.Connection, file_id: int) -> dict[str, Any]:
    """Tombstones a vault file, moves it aside and removes its rows, all or nothing.

    Raises:
        DeleteRefused: Nothing changed. Carries the message and response status.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    row = conn.execute(queries.SELECT_FILE, (file_id,)).fetchone()
    if not row:
        raise DeleteRefused("That file is no longer in the catalog.", 404)
    if row["source"] in LINKED_SOURCES:
        raise DeleteRefused(LINKED_REASON)
    if conn.execute(queries.COUNT_STASH_ITEMS, (file_id,)).fetchone()[0]:
        raise DeleteRefused(STASHED_REASON, 409)

    stored = row["vault_path"] or row["staging_path"]
    source = Path(stored) if stored else None
    root = _vault_root(conn) if source else None
    if source and root:
        if not source.resolve().is_relative_to(root):
            raise DeleteRefused(OUTSIDE_REASON)
        if not source.is_file():
            raise DeleteRefused("The file isn't at its recorded path. Run a reconciliation first.", 409)

    moved = None
    try:
        with conn:
            tombstone = int(conn.execute(queries.INSERT_TOMBSTONE, (
                file_id, row["collection_id"], row["collection_name"], row["filename"], stored or "",
                row["size_bytes"], row["sha256"], row["external_key"], _snapshot(conn, file_id, source),
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


def hold_folder(conn: sqlite3.Connection, root: Path, folder: Path) -> Path:
    """Holds an orphan folder like a deleted file, so a mistaken delete can be undone for 14 days."""
    size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
    moved = None
    try:
        with conn:
            tombstone = int(conn.execute(queries.INSERT_TOMBSTONE, (
                FOLDER_ID, FOLDER_ID, ORPHAN_FOLDER_LABEL, folder.name, str(folder), size, None, None, None,
            )).lastrowid or 0)
            moved = _hold(root, folder, tombstone)
            conn.execute(queries.UPDATE_PENDING_PATH, (str(moved), tombstone))
    except (OSError, sqlite3.Error):
        # The catalog rolled back, so the folder has to go back with it.
        if moved and moved.exists():
            shutil.move(str(moved), folder)
        raise
    log_layer("storage", logging.WARNING, f"Held orphan folder {folder.name} in pending deletion")
    return moved


def _insert_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    # Only columns that still exist, so a tombstone survives later migrations.
    current = {column[1] for column in conn.execute(f"PRAGMA table_info({table})")}
    columns = [name for name in row if name in current]
    marks = ",".join("?" * len(columns))
    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", [row[name] for name in columns])


def _put_rows_back(conn: sqlite3.Connection, rows: dict[str, list[dict[str, Any]]]) -> int:
    """Returns how many artifacts couldn't return, because the model they belonged to is gone."""
    lost = 0
    for table in SNAPSHOT_TABLES:
        for row in rows.get(table, []):
            if table == "model_artifacts" and not conn.execute(queries.SELECT_MODEL_EXISTS, (row["model_id"],)).fetchone():
                lost += 1
                continue
            _insert_row(conn, table, row)
    for row in rows.get("duplicate_decisions", []):
        if all(conn.execute(queries.SELECT_FILE_EXISTS, (row[key],)).fetchone() for key in ("first_file_id", "second_file_id")):
            _insert_row(conn, "duplicate_decisions", row)
    return lost


def _put_bare_rows_back(conn: sqlite3.Connection, tombstone: sqlite3.Row, source: str) -> None:
    # A tombstone from before snapshots only holds the file itself, so artifacts need a re-peek or rescan.
    name = tombstone["filename"]
    conn.execute(queries.INSERT_BARE_FILE, (
        tombstone["file_id"], tombstone["collection_id"], name, tombstone["size_bytes"], source, tombstone["external_key"],
    ))
    if tombstone["original_path"]:
        conn.execute(queries.INSERT_BARE_DOWNLOAD, (
            tombstone["file_id"], tombstone["collection_id"], name, name, f"files/{name}", tombstone["size_bytes"],
            tombstone["size_bytes"], Path(name).suffix.lower(), tombstone["original_path"], tombstone["sha256"],
        ))


def restore_file(conn: sqlite3.Connection, tombstone_id: int) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    tombstone = conn.execute(queries.SELECT_TOMBSTONE, (tombstone_id,)).fetchone()
    if not tombstone:
        raise DeleteRefused("That file can no longer be restored.", 404)
    is_folder = tombstone["file_id"] == FOLDER_ID
    collection = conn.execute(queries.SELECT_COLLECTION, (tombstone["collection_id"],)).fetchone()
    if not collection and not is_folder:
        raise DeleteRefused("Its collection is no longer in the catalog.", 409)

    held = Path(tombstone["pending_path"]) if tombstone["pending_path"] else None
    target = Path(tombstone["original_path"]) if tombstone["original_path"] else None
    if held and target:
        root = _vault_root(conn)
        if not target.resolve().is_relative_to(root):
            raise DeleteRefused(OUTSIDE_REASON)
        if not (held.is_dir() if is_folder else held.is_file()):
            raise DeleteRefused("The held file is missing from .Pending-Deletion.", 409)
        if target.exists():
            raise DeleteRefused("Something else is now at its old path, so nothing was restored.", 409)

    snapshot = json.loads(tombstone["snapshot_json"]) if tombstone["snapshot_json"] else None
    back = False
    try:
        with conn:
            lost = _put_rows_back(conn, snapshot["rows"]) if snapshot else 0
            if not snapshot and not is_folder:
                _put_bare_rows_back(conn, tombstone, collection["source"])
            conn.execute(queries.UPDATE_RESTORED, (tombstone_id,))
            if held and target:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(held), target)
                back = True
                # The hold reset this date, and a reconcile compares it against the catalog.
                if snapshot and snapshot.get("modified"):
                    os.utime(target, (snapshot["modified"], snapshot["modified"]))
    except (OSError, sqlite3.Error, ValueError) as e:
        if back and held and target and target.exists():
            shutil.move(str(target), held)
        raise DeleteRefused(f"It couldn't be restored: {e}", 500) from e

    if held and held.parent.is_dir() and not any(held.parent.iterdir()):
        held.parent.rmdir()
    result = {
        "success": True, "file_id": tombstone["file_id"], "filename": tombstone["filename"],
        "path": tombstone["original_path"], "artifacts_lost": lost, "partial": snapshot is None,
    }
    inactive_log.resolve(conn, inactive_log.FILE, tombstone["file_id"], inactive_log.DELETED_DUPLICATE)
    conn.commit()
    audit.record(conn, "duplicate_restore", False, result)
    log_layer("storage", logging.WARNING, f"Restored file #{tombstone['file_id']} ({tombstone['filename']})")
    return result


def listing(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything that can still be restored, and which of it could be purged today."""
    conn.row_factory = sqlite3.Row
    online = _vault_root_or_none(conn) is not None
    items = []
    for row in conn.execute(queries.SELECT_RESTORABLE).fetchall():
        path = row["pending_path"]
        age = _age_days(Path(path)) if path and online else None
        items.append({
            "id": row["id"], "filename": row["filename"], "collection": row["collection_name"],
            "original_path": row["original_path"], "size_bytes": row["size_bytes"], "deleted_at": row["deleted_at"],
            "held": bool(path), "age_days": int(age) if age is not None else None,
            "can_purge": bool(path) and online and (age is None or age >= WAIT_DAYS),
        })
    return {"success": True, "online": online, "wait_days": WAIT_DAYS, "items": items}


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts held files from the catalog, so Disk Usage needs no NAS."""
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
        # FIXME(S-08): any stat failure reads as missing and skips the wait. Use deleted_at from the catalog.
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
            if path.is_dir():
                shutil.rmtree(path)
            else:
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
