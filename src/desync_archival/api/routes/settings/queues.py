"""Archive review, the image queue and the inactive list."""

import logging
import sqlite3
from typing import Any

from desync_archival.services import (
    archive_review,
    image_worker,
    inactive_log,
    library_review,
)
from desync_archival.services.logger import (
    log_layer,
)


def handle_get_archive_queue(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    return {"collections": archive_review.queue(conn)}, 200


def handle_get_archive_review(
    conn: sqlite3.Connection,
    collection_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        return archive_review.view(conn, collection_id), 200
    except library_review.ReviewError as e:
        return {"success": False, "error": str(e)}, 409


def handle_post_archive_review(
    conn: sqlite3.Connection,
    collection_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    try:
        result = archive_review.save(conn, collection_id, data.get("cards") or [], bool(data.get("reviewed")))
    except library_review.ReviewError as e:
        log_layer("storage", logging.WARNING, f"Collection #{collection_id}: archive review refused: {e}")
        return {"success": False, "error": str(e)}, 409
    log_layer("storage", logging.INFO, f"Collection #{collection_id}: archive review saved: {result}")
    return result, 200


def handle_get_image_queue(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    return image_worker.status(conn), 200


def handle_post_image_queue(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    action = str((body or {}).get("action") or "")
    if action == "pause":
        image_worker.set_paused(conn, True)
    elif action == "resume":
        image_worker.set_paused(conn, False)
    elif action == "retry":
        retried = image_worker.retry_failed(conn)
        log_layer("storage", logging.INFO, f"Image queue: {retried} failed items set to retry")
    else:
        return {"error": "Action must be pause, resume or retry."}, 400
    return {"success": True, **image_worker.status(conn)}, 200


def handle_get_inactive(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """GET /api/inactive: what went inactive or failed, from every process."""
    def param(name: str) -> str:
        return ((params or {}).get(name) or [""])[0]

    try:
        limit = int(param("limit") or inactive_log.DEFAULT_LIMIT)
        offset = int(param("offset") or 0)
    except ValueError:
        return {"error": "limit and offset must be numbers."}, 400
    return inactive_log.query(
        conn, param("process"), param("reason"), param("include_resolved") == "1", max(1, limit), max(0, offset),
    ), 200
