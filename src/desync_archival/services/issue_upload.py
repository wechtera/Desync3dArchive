"""Puts a file the owner downloaded by hand in place of one a download couldn't settle.

The upload wins over MMF's listing: its size becomes the file's confirmed size. A copy it replaces
goes to .Pending-Deletion for the usual 14 days, so a wrong pick costs nothing.
"""

import contextlib
import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from desync_archival.db.queries import download_issues as queries
from desync_archival.db.queries.downloader import LIVE_JOB_STATUSES
from desync_archival.services import audit, download_issues, pending_deletion
from desync_archival.services.download_issues import IssueRefused
from desync_archival.services.downloader import (
    PAGE_SUFFIXES,
    FileProblem,
    check_zip,
    looks_like_page,
    model_folder,
    sha256_of,
    write_model_metadata_json,
)
from desync_archival.services.logger import log_layer
from desync_archival.services.paths import distinct_filename, stored_filename

COPY_CHUNK = 4 * 1024 * 1024
# A browser saves a second download of one name as "name (1).ext". That's still the same file.
BROWSER_COPY = re.compile(r" \(\d+\)$")
# Pending too: a queued download would overwrite the upload when it reaches the NAS.
BUSY_STATUSES = ("pending", *LIVE_JOB_STATUSES)


def same_name(expected: str, uploaded: str) -> bool:
    stem, suffix = os.path.splitext(uploaded)
    return (BROWSER_COPY.sub("", stem) + suffix).lower() == expected.lower()


def _vault_root(conn: sqlite3.Connection) -> Path:
    row = conn.execute(queries.SELECT_VAULT_ROOT).fetchone()
    root = Path(row[0]) if row and row[0] else None
    if root is None or not root.is_dir():
        raise IssueRefused("The vault is offline, so nothing can be placed. Try again when the NAS is back.", 503)
    return root.resolve()


def check(conn: sqlite3.Connection, issue_id: int, name: str, confirm_name: bool) -> sqlite3.Row:
    """Everything that can be refused before a byte arrives, so a refusal doesn't cost the transfer."""
    issue = download_issues.open_issue(conn, issue_id)
    wanted, given = Path(issue["filename"]).suffix.lower(), Path(name).suffix.lower()
    if given != wanted:
        raise IssueRefused(f"That's a {given or 'file with no extension'}, and this entry needs a {wanted} file.")
    if not confirm_name and not same_name(issue["filename"], name):
        raise IssueRefused(f"The file picked is {name}, but this entry is {issue['filename']}. Confirm to use it anyway.", 409)
    job = conn.execute(queries.SELECT_JOB_STATUS, (issue["model_id"],)).fetchone()
    if job and job[0] in BUSY_STATUSES:
        raise IssueRefused("This model is queued or downloading now. Try again when its download finishes.", 409)
    if issue["storage_tier"] and issue["storage_tier"] != "nas":
        raise IssueRefused("This file is still on its way to the NAS. Try again when its download finishes.", 409)
    _vault_root(conn)
    return issue


def _check_contents(issue: sqlite3.Row, upload: Path) -> int:
    size = upload.stat().st_size
    if not size:
        raise IssueRefused("The upload was empty.")
    if Path(issue["filename"]).suffix.lower() not in PAGE_SUFFIXES and looks_like_page(upload, ""):
        raise IssueRefused("That's a web page, not the file. A browser saves one when a download is refused.")
    try:
        check_zip(upload, issue["filename"])
    except FileProblem as e:
        raise IssueRefused(str(e)) from None
    return size


def _target(conn: sqlite3.Connection, root: Path, issue: sqlite3.Row) -> tuple[Path, Path]:
    """Where the file goes, and its model's folder. A stored file is replaced where it is."""
    collection = conn.execute(queries.SELECT_COLLECTION, (issue["model_id"],)).fetchone()
    if collection["vault_path"]:
        model_dir = Path(collection["vault_path"])
    else:
        model_dir = root / model_folder(
            collection["creator_name"], collection["release_month"], collection["external_key"],
            issue["model_id"], collection["name"],
        )
    if issue["vault_path"]:
        target = Path(issue["vault_path"])
    else:
        taken = {row[0] for row in conn.execute(queries.SELECT_STORED_NAMES, (issue["model_id"], issue["file_id"]))}
        target = model_dir / "files" / distinct_filename(stored_filename(issue["filename"]), issue["file_id"], taken)
        if target.exists():
            raise IssueRefused(f"Something the catalog doesn't know is already at {target}. Run a Vault Reconcile first.", 409)
    if not target.resolve().is_relative_to(root):
        raise IssueRefused("This file's recorded place isn't in the vault, so nothing can go there.")
    return target, model_dir


def _copy_verified(source: Path, part: Path, size: int, sha256: str) -> None:
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as f_in, open(part, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, COPY_CHUNK)
            f_out.flush()
            os.fsync(f_out.fileno())
        # Read back, like the offload does: SMB can report a write that failed late.
        matches = part.stat().st_size == size and sha256_of(part, COPY_CHUNK) == sha256
    except OSError as e:
        raise IssueRefused(f"The NAS stopped answering during the copy, so nothing changed: {e}", 503) from e
    if not matches:
        raise IssueRefused("The copy on the NAS didn't match the upload, so nothing changed. Try again.", 500)


def place(
    conn: sqlite3.Connection, issue_id: int, upload: Path, *, name: str, sha256: str, confirm_name: bool = False,
) -> dict[str, Any]:
    """Replaces or adds the file, then settles its issue.

    Raises:
        IssueRefused: Nothing changed. Carries the message and response status.
    """
    conn.row_factory = sqlite3.Row
    issue = check(conn, issue_id, name, confirm_name)
    size = _check_contents(issue, upload)
    root = _vault_root(conn)
    target, model_dir = _target(conn, root, issue)
    part = target.with_name(f"{target.name}.part")
    new_folder = not (model_dir / "metadata.json").exists()
    held: Path | None = None
    placed = False

    try:
        _copy_verified(upload, part, size, sha256)
        try:
            with conn:
                if target.exists():
                    held = pending_deletion.hold_replaced_file(conn, root, dict(issue), target)
                os.replace(part, target)
                placed = True
                if issue["storage_tier"]:
                    conn.execute(queries.UPDATE_STORED_FILE, (size, size, sha256, issue["file_id"]))
                else:
                    conn.execute(queries.INSERT_STORED_FILE, (
                        issue["file_id"], issue["model_id"], issue["filename"], target.name,
                        str(target.resolve().relative_to(root)), size, size, target.suffix.lower(), str(target), sha256,
                    ))
                conn.execute(queries.CONFIRM_SIZE, (size, issue["file_id"]))
                conn.execute(queries.RESOLVE_ISSUE, (download_issues.UPLOADED, issue_id))
                conn.execute(queries.SET_VAULT_PATH_IF_NONE, (str(model_dir), issue["model_id"]))
                conn.execute(queries.INSERT_UPLOAD_ATTEMPT, (
                    issue["model_id"], issue["file_id"], f"Uploaded {name} ({size:,} bytes) as {target}",
                ))
        except (OSError, sqlite3.Error) as e:
            # The catalog rolled back, so the files go back with it.
            if placed and target.exists():
                os.replace(target, part)
            if held and held.exists():
                shutil.move(str(held), target)
                with contextlib.suppress(OSError):
                    held.parent.rmdir()
            raise IssueRefused(f"The upload couldn't be put in place, so nothing changed: {e}", 500) from e
    finally:
        part.unlink(missing_ok=True)

    if new_folder:
        try:
            write_model_metadata_json(conn, issue["model_id"], model_dir)
        except OSError as e:
            log_layer("storage", logging.WARNING, f"Uploaded file #{issue['file_id']}, but metadata.json wasn't written: {e}")

    result = {
        "success": True, "id": issue_id, "file_id": issue["file_id"], "filename": issue["filename"],
        "path": str(target), "size_bytes": size, "replaced_copy": str(held or ""),
    }
    audit.record(conn, "download_issue_upload", False, result)
    log_layer("storage", logging.WARNING, f"Placed an upload for file #{issue['file_id']} ({issue['filename']}) at {target}")
    return result
