"""Files a download couldn't finish cleanly, kept until the owner settles each one.

Two kinds. size_differs: both tries delivered the same bytes, but not the size MMF lists, so the
file was kept and needs a look. not_downloaded: the file failed twice and needs a manual
download. A file has at most one open issue. A clean download, Keep, or an upload resolves it.
"""

import sqlite3
from typing import Any

from desync_archival.db.queries import download_issues as queries
from desync_archival.services import audit

SIZE_DIFFERS = "size_differs"
NOT_DOWNLOADED = "not_downloaded"
KEPT = "kept"
UPLOADED = "uploaded"
DOWNLOADED = "downloaded"
RESOLVED_SHOWN = 100


class IssueRefused(ValueError):
    """Nothing changed. The message is shown to the user, and the code is the response status."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def record(
    conn: sqlite3.Connection, file_id: int, model_id: int, kind: str,
    listed: int | None, received: int | None, detail: str,
) -> None:
    """Opens an issue for a file, or brings its open one up to date. The caller commits."""
    args = (kind, listed, received, detail, file_id)
    if not conn.execute(queries.UPDATE_OPEN_ISSUE, args).rowcount:
        conn.execute(queries.INSERT_ISSUE, (file_id, model_id, kind, listed, received, detail))


def resolve(conn: sqlite3.Connection, file_id: int, resolution: str) -> None:
    conn.execute(queries.RESOLVE_FILE, (resolution, file_id))


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute(queries.COUNT_OPEN).fetchone()[0]


def _item(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["stored"] = row["storage_tier"] == "nas" and bool(row["vault_path"])
    return item


def for_model(conn: sqlite3.Connection, model_id: int) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [_item(row) for row in conn.execute(queries.SELECT_OPEN_FOR_MODEL, (model_id,))]


def listing(conn: sqlite3.Connection, resolved: bool = False) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    rows = (
        conn.execute(queries.SELECT_RESOLVED, (RESOLVED_SHOWN,)) if resolved else conn.execute(queries.SELECT_OPEN)
    )
    return {"items": [_item(row) for row in rows], "open": open_count(conn)}


def open_issue(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_ISSUE, (issue_id,)).fetchone()
    if not row:
        raise IssueRefused("That issue no longer exists.", 404)
    if row["resolved_at"]:
        raise IssueRefused("That issue is already settled.", 409)
    return row


def keep(conn: sqlite3.Connection, issue_id: int) -> dict[str, Any]:
    """The owner looked and the kept file is right, so its real size replaces MMF's listing."""
    row = open_issue(conn, issue_id)
    if row["kind"] != SIZE_DIFFERS:
        raise IssueRefused("Only a file that was kept can be marked as checked. This one needs a download.")
    if row["storage_tier"] != "nas" or not row["vault_path"]:
        raise IssueRefused("This file is still on its way to the NAS. Try again when its download finishes.", 409)
    with conn:
        conn.execute(queries.CONFIRM_SIZE, (row["stored_bytes"], row["file_id"]))
        conn.execute(queries.RESOLVE_ISSUE, (KEPT, issue_id))
    result = {"success": True, "id": issue_id, "file_id": row["file_id"], "size_bytes": row["stored_bytes"]}
    audit.record(conn, "download_issue_keep", False, result)
    return result
