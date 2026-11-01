"""Backs the Inactive tab. Rows are never deleted: a returning item gets resolved_at instead.

Nothing here commits, so a record lands in the same transaction as the change it describes.
"""

import sqlite3
from typing import Any

from desync_archival.db.queries import inactive_log as queries

MODEL = "model"
FILE = "file"

LIBRARY_SCAN = "library_scan"
LIBRARY_UNLINK = "library_unlink"
DUPLICATE_CHECK = "duplicate_check"
METADATA_INGEST = "metadata_ingest"
IMAGE_WORKER = "image_worker"

FOLDER_MISSING = "folder_missing"
FILE_MISSING = "file_missing"
LIBRARY_UNLINKED = "library_unlinked"
DELETED_DUPLICATE = "deleted_duplicate"
UNAVAILABLE = "unavailable"
RENDER_FAILED = "render_failed"
NO_SOURCE = "no_source"
PEEK_FAILED = "peek_failed"

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def record(
    conn: sqlite3.Connection, item_type: str, item_id: int, item_name: str | None,
    process: str, reason: str, detail: str | None = None,
) -> None:
    # Safe to call on every rescan: a trigger drops repeats of an open row.
    conn.execute(
        queries.INSERT_INACTIVE_LOG,
        (item_type, item_id, item_name, process, reason, detail),
    )


def resolve(conn: sqlite3.Connection, item_type: str, item_id: int, reason: str | None = None) -> int:
    sql = "UPDATE inactive_log SET resolved_at = CURRENT_TIMESTAMP WHERE item_type = ? AND item_id = ? AND resolved_at IS NULL"
    args: list[Any] = [item_type, item_id]
    if reason:
        sql += " AND reason = ?"
        args.append(reason)
    return conn.execute(sql, args).rowcount


def query(
    conn: sqlite3.Connection, process: str = "", reason: str = "", include_resolved: bool = False,
    limit: int = DEFAULT_LIMIT, offset: int = 0,
) -> dict[str, Any]:
    conditions, args = [], []
    if not include_resolved:
        conditions.append("resolved_at IS NULL")
    if process:
        conditions.append("process = ?")
        args.append(process)
    if reason:
        conditions.append("reason = ?")
        args.append(reason)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    columns = ("id", "item_type", "item_id", "item_name", "process", "reason", "detail", "created_at", "resolved_at")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM inactive_log {where} ORDER BY id DESC LIMIT ? OFFSET ?;",
        [*args, min(limit, MAX_LIMIT), offset],
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM inactive_log {where};", args).fetchone()[0]
    counts = conn.execute(
        queries.SELECT_INACTIVE_LOG_BY_RESOLVED_AT
    ).fetchall()
    return {
        "items": [dict(zip(columns, tuple(row))) for row in rows],
        "total": total,
        "open_counts": [{"process": r[0], "reason": r[1], "count": r[2]} for r in counts],
    }
