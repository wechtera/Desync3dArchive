import sqlite3
from typing import Any

from desync_archival.api.routing import Body, Conn, Downloads, Params, paging
from desync_archival.db.queries import downloads as queries
from desync_archival.db.queries.downloader import LIVE_JOB_STATUSES
from desync_archival.services import audit
from desync_archival.services.downloader import (
    queue_models,
    remove_waiting_jobs,
)

# Nothing in these can hold the only copy of a file.
CLEARABLE_STATUSES = ("pending", "failed", "completed")


def handle_get_downloads_status(
    download_manager: Downloads,
) -> tuple[dict[str, Any], int]:
    if not download_manager:
        return {"error": "DownloadManager uninitialized"}, 500
    return download_manager.get_status(), 200


def handle_get_downloads_history(
    conn: Conn,
    params: Params,
) -> tuple[dict[str, Any], int]:
    status = params.get("status", [""])[0].strip()
    sort = params.get("sort", ["enqueued_desc"])[0].strip()
    limit, offset = paging(params, 50, 100)

    where_clause = " WHERE 1=1"
    args = []

    if status == "completed":
        where_clause += " AND dj.status = 'completed'"
    elif status == "failed":
        where_clause += " AND dj.status = 'failed'"
    elif status:
        where_clause += " AND dj.status = ?"
        args.append(status)

    count_query = queries.COUNT_DOWNLOAD_JOBS.format(where_clause=where_clause)
    total_count = conn.execute(count_query, args).fetchone()[0]

    order_by = " ORDER BY dj.enqueued_at DESC"
    if sort == "enqueued_asc":
        order_by = " ORDER BY dj.enqueued_at ASC"
    elif sort == "completed_desc":
        order_by = " ORDER BY dj.completed_at DESC NULLS LAST, dj.id DESC"
    elif sort == "completed_asc":
        order_by = " ORDER BY dj.completed_at ASC NULLS LAST, dj.id ASC"

    query = queries.SELECT_DOWNLOAD_JOBS.format(order_by=order_by, where_clause=where_clause)
    rows = conn.execute(query, args + [limit, offset]).fetchall()

    history = []
    for r in rows:
        item = dict(r)
        if not item.get("creator_name"):
            item["creator_name"] = item.get("designer_name") or "Unknown"
        history.append(item)

    return {
        "total": total_count,
        "history": history,
        "limit": limit,
        "offset": offset,
    }, 200


def handle_post_downloads_queue(
    conn: Conn,
    download_manager: Downloads,
    body: Body,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    release_id = body.get("release_id")

    if release_id and not model_ids:
        rows = conn.execute(queries.SELECT_QUEUEABLE_RELEASE_MODELS, (release_id,)).fetchall()
        model_ids = [r[0] for r in rows]

    if not model_ids:
        return {"error": "No model IDs provided"}, 400

    clean_ids = []
    for mid in model_ids:
        try:
            clean_ids.append(int(mid))
        except (ValueError, TypeError):
            continue
    queued_count = queue_models(conn, download_manager, clean_ids)

    return {"success": True, "queued_count": queued_count}, 200


def handle_delete_downloads_queue(
    conn: Conn,
    body: Body,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    clear_status = body.get("clear_status")
    purge_incomplete = body.get("purge_incomplete", False)
    release_id = body.get("release_id")

    if clear_status in LIVE_JOB_STATUSES:
        return {"error": f"Jobs in {clear_status} are in progress, so they can't be cleared."}, 409
    if clear_status and clear_status not in CLEARABLE_STATUSES:
        return {"error": f"Only {', '.join(CLEARABLE_STATUSES)} jobs can be cleared."}, 400

    # A bulk clear lists first, like every other destructive route. Taking a chosen model or a whole
    # release off the queue is a single deliberate click, so those act at once and are audited.
    if purge_incomplete or clear_status:
        return _clear_jobs(conn, clear_status, bool(body.get("confirm")))

    removed = kept = 0
    with conn:
        if release_id:
            conn.execute(queries.DELETE_PENDING_RELEASE_JOBS, (release_id,))
        elif model_ids:
            clean_ids = [int(m) for m in model_ids if str(m).isdigit()]
            removed = remove_waiting_jobs(conn, clean_ids)
            kept = len(clean_ids) - removed
    if release_id or model_ids:
        audit.record(conn, "queue_remove", False, {"release_id": release_id, "model_ids": model_ids, "removed": removed, "kept": kept})

    return {"success": True, "removed": removed, "kept": kept}, 200


def _clear_jobs(conn: sqlite3.Connection, clear_status: str | None, confirm: bool) -> tuple[dict[str, Any], int]:
    """Clears a status, or every incomplete job when none is given. Lists them without confirm."""
    if clear_status:
        count, args = queries.COUNT_JOBS_BY_STATUS, (clear_status,)
    else:
        count, args = queries.COUNT_INCOMPLETE_JOBS, ()
    found = conn.execute(count, args).fetchone()[0]
    audit.record(conn, "queue_clear", not confirm, {"status": clear_status or "incomplete", "jobs": found})
    if not confirm:
        return {"success": True, "dry_run": True, "would_remove": found}, 200
    with conn:
        if clear_status:
            conn.execute(queries.DELETE_DOWNLOAD_JOBS_BY_STATUS_V2, (clear_status,))
        else:
            conn.execute(queries.DELETE_DOWNLOAD_JOBS_BY_STATUS)
    return {"success": True, "dry_run": False, "removed": found, "kept": 0}, 200


def handle_post_downloads_control(
    conn: Conn,
    download_manager: Downloads,
    body: Body,
) -> tuple[dict[str, Any], int]:
    if not download_manager:
        return {"error": "DownloadManager uninitialized"}, 500

    action = body.get("action", "").strip()

    if action == "pause":
        download_manager.pause()
    elif action == "resume":
        download_manager.resume()
    elif action == "reset_circuit_breaker":
        download_manager.reset_circuit_breaker()
        download_manager.resume()
    elif action == "retry_failed":
        with conn:
            conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS)
        download_manager.resume()
    elif action == "purge_incomplete":
        result, status = _clear_jobs(conn, None, bool(body.get("confirm")))
        return {**result, "action": action}, status
    else:
        return {"error": f"Unknown action: {action}"}, 400

    return {"success": True, "action": action}, 200
