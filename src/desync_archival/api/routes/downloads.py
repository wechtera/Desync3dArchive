import sqlite3
from typing import Any, Optional

from desync_archival.db.queries import downloads as queries
from desync_archival.services.downloader import DownloadManager, queue_models


def handle_get_downloads_status(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    if not download_manager:
        return {"error": "DownloadManager uninitialized"}, 500
    return download_manager.get_status(), 200


def handle_get_downloads_history(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    status = params.get("status", [""])[0].strip()
    sort = params.get("sort", ["enqueued_desc"])[0].strip()
    limit = max(1, min(100, int(params.get("limit", [50])[0])))
    offset = max(0, int(params.get("offset", [0])[0]))

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
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    release_id = body.get("release_id")

    if release_id and not model_ids:
        parts = release_id.split("_", 1)
        if len(parts) == 2:
            designer, month = parts
            rows = conn.execute(
                queries.SELECT_COLLECTIONS_ID_BY_DESIGNER_NAME,
                (designer, month),
            ).fetchall()
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
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    clear_status = body.get("clear_status")
    purge_incomplete = body.get("purge_incomplete", False)

    with conn:
        if purge_incomplete:
            conn.execute(queries.DELETE_DOWNLOAD_JOBS_BY_STATUS)
        elif clear_status:
            conn.execute(queries.DELETE_DOWNLOAD_JOBS_BY_STATUS_V2, (clear_status,))
        elif model_ids:
            clean_ids = [int(m) for m in model_ids if str(m).isdigit()]
            if clean_ids:
                conn.execute(
                    f"DELETE FROM download_jobs WHERE model_id IN ({','.join(['?']*len(clean_ids))});",
                    clean_ids,
                )

    return {"success": True}, 200


def handle_post_downloads_control(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
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
        with conn:
            conn.execute(queries.DELETE_DOWNLOAD_JOBS_BY_STATUS_V3)
    else:
        return {"error": f"Unknown action: {action}"}, 400

    return {"success": True, "action": action}, 200
