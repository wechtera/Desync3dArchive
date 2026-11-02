"""Linked libraries, their review, and file imports."""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from desync_archival import mounts
from desync_archival import settings as app_config
from desync_archival.api.routes.settings.core import format_bytes
from desync_archival.db.queries import settings as queries
from desync_archival.services import (
    audit,
    catalog_review,
    file_import,
    jobs,
    library_review,
)
from desync_archival.services.external_indexer import (
    FULL,
    PREVIEW,
    SCAN_MODES,
    ExternalLibraryIndexer,
    deactivate_library,
)
from desync_archival.services.logger import (
    log_layer,
    logger,
)

# -------------------------------------------------------------------------
# In-Place External Libraries Endpoints
# -------------------------------------------------------------------------

def handle_get_external_libraries(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """
    GET /api/settings/libraries
    Returns all registered in-place external library directories with telemetry,
    formatted disk usage, model counts, and scan timestamps.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_BY_IS_ACTIVE).fetchall()

    libraries = []
    for r in rows:
        item = dict(r)
        d_bytes = item.get("disk_bytes") or 0
        t_files = item.get("total_files") or 0

        if d_bytes > 0:
            item["disk_formatted"] = format_bytes(d_bytes)
        elif t_files == 0 and not item.get("last_scanned_at"):
            item["disk_formatted"] = "Calculating..."
        else:
            item["disk_formatted"] = "0 B"

        item["exists"] = mounts.is_online(item["path"]) if item.get("path") else False
        libraries.append(item)

    return {"success": True, "libraries": libraries, "total": len(libraries)}, 200


def handle_post_external_library(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """
    POST /api/settings/libraries
    Registers a new top-level in-place library folder.
    Strictly READ-ONLY: Never moves, renames, modifies, or creates files in the target directory.
    Returns immediately, and measures the folder as a background job.
    """
    data = body or {}
    raw_path = str(data.get("path", "")).strip()
    if not raw_path:
        return {"error": "Missing required field 'path'."}, 400

    lib_path = Path(raw_path).expanduser().resolve()
    try:
        app_config.check_settable_path(lib_path)
    except app_config.SettingsError as e:
        return {"error": str(e)}, 400

    if not lib_path.exists() or not lib_path.is_dir():
        return {"error": f"Target directory does not exist or is not a directory: {raw_path}"}, 400

    try:
        # Validate readability without running a full tree crawl
        with os.scandir(str(lib_path)) as it:
            next(it, None)
    except (PermissionError, OSError) as e:
        logger.warning(f"Cannot read library directory {raw_path}: {e}")
        return {"error": f"Cannot read directory '{raw_path}'"}, 403

    name = str(data.get("name", "")).strip() or lib_path.name
    default_creator = str(data.get("default_creator", "")).strip() or lib_path.name

    with conn:
        if not conn.execute(queries.UPDATE_EXTERNAL_LIBRARIES_NAME_BY_PATH, (name, default_creator, str(lib_path))).rowcount:
            conn.execute(queries.POST_EXTERNAL_LIBRARY_1B, (name, str(lib_path), default_creator))
        row = conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_ID_BY_PATH, (str(lib_path),)).fetchone()
        lib_id = row[0] if row else None

    # A scan records the same totals, so a busy slot only delays the number.
    started = task_manager.start_task("library_measure", {"library_id": lib_id, "path": str(lib_path)}) if task_manager else {}
    measuring = bool(started.get("success"))

    log_layer("storage", logging.INFO, f"Registered in-place library #{lib_id}: '{name}' at '{lib_path}'")

    return {
        "success": True,
        "library": {
            "id": lib_id,
            "name": name,
            "path": str(lib_path),
            "default_creator": default_creator,
            "disk_bytes": 0,
            "disk_formatted": "Calculating...",
            "total_files": 0,
            "total_models": 0,
            "last_scanned_at": None,
        },
        "task_id": started.get("task_id"),
        "message": f"Registered in-place library '{name}'. " + (
            "Measuring its size as a background task, and a scan can start once that finishes."
            if measuring else "Its size will appear after its first scan."
        ),
    }, 201


def handle_delete_external_library(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """
    DELETE /api/settings/libraries/:id
    Unlinks an in-place library record from SQLite.
    INVARIANT: MUST NOT delete or touch any physical files or folders on the NAS.
    """
    conn.row_factory = sqlite3.Row
    if reason := jobs.busy_reason(conn, download_manager):
        return {"success": False, "error": reason}, 409
    row = conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_BY_ID, (library_id,)).fetchone()
    if not row:
        return {"error": f"External library #{library_id} not found."}, 404

    lib_name = row["name"]
    lib_path = row["path"]

    deactivate_library(conn, library_id)
    audit.record(conn, "library_unlink", False, {"library_id": library_id, "name": lib_name, "path": lib_path})

    log_layer("storage", logging.INFO, f"Unlinked in-place library #{library_id} ('{lib_name}'). Physical files on NAS left untouched.")

    return {
        "success": True,
        "deleted_id": library_id,
        "name": lib_name,
        "path": lib_path,
        "message": f"Library '{lib_name}' unlinked from database. No physical files on disk were modified or deleted.",
    }, 200


def handle_post_external_library_scan(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """
    POST /api/settings/libraries/:id/scan
    Dispatches task_type = 'external_library_reconcile' to scan and index the external library in-place.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_BY_ID_IS_ACTIVE, (library_id,)).fetchone()
    if not row:
        return {"error": f"External library #{library_id} not found."}, 404

    if task_manager is None:
        return {"error": "TaskManager background service is unavailable."}, 503

    mode = str((body or {}).get("mode") or FULL)
    if mode not in SCAN_MODES:
        return {"error": f"Scan mode must be one of {', '.join(SCAN_MODES)}."}, 400

    res = task_manager.start_task("external_library_reconcile", {
        "library_id": library_id,
        "mode": mode,
        "library_name": row["name"],
        "library_root": row["path"],
        "path": row["path"],
    })

    if not res.get("success"):
        return res, 409

    return {
        "success": True,
        "task_id": res["task_id"],
        "task_type": "external_library_reconcile",
        "library_id": library_id,
        "message": f"Background scan dispatched for in-place library '{row['name']}' (Task #{res['task_id']}).",
    }, 200


def handle_get_library_preview(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """GET /api/settings/libraries/:id/preview: the latest report, and what is waiting on a decision."""
    conn.row_factory = sqlite3.Row
    run = conn.execute(
        "SELECT id, status, report_json, finished_at FROM library_scan_runs"
        " WHERE library_id = ? AND mode = ? AND status = 'completed' ORDER BY id DESC LIMIT 1;",
        (library_id, PREVIEW),
    ).fetchone()
    statuses = dict(conn.execute(
        "SELECT status, COUNT(*) FROM library_candidates WHERE library_id = ? GROUP BY status;", (library_id,)
    ).fetchall())
    return {
        "report": library_review.with_reviews(conn, library_id, json.loads(run["report_json"])) if run and run["report_json"] else None,
        "finished_at": run["finished_at"] if run else None,
        "candidates": statuses,
    }, 200


def handle_post_library_approve(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """POST /api/settings/libraries/:id/preview/approve: only approved folders can become models."""
    skip = [str(name) for name in (body or {}).get("skip_releases") or []]
    conn.row_factory = sqlite3.Row
    if (body or {}).get("ready_only"):
        try:
            ready = library_review.approve_ready(conn, library_id, skip)
        except library_review.ReviewError as e:
            return {"success": False, "error": str(e)}, 409
        log_layer("storage", logging.INFO, f"Library #{library_id}: approved the reviewed and unflagged folders: {ready}")
        return {"success": True, **ready}, 200
    result = ExternalLibraryIndexer(app_config.db_path()).approve_candidates(conn, library_id, skip)
    log_layer("storage", logging.INFO, f"Library #{library_id}: approved {result['approved']} folders, skipped {result['skipped']}")
    return {"success": True, **result}, 200


def handle_get_library_release(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """GET /api/settings/libraries/:id/preview/release?name=: the saved tree and its cards."""
    try:
        return library_review.release_view(conn, library_id, ((params or {}).get("name") or [""])[0]), 200
    except library_review.ReviewError as e:
        return {"success": False, "error": str(e)}, 409


def handle_post_library_review(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """POST /api/settings/libraries/:id/preview/review: a human's cards beat the resolver's."""
    data = body or {}
    release = str(data.get("release") or "")
    try:
        if data.get("reset"):
            result = library_review.reset(conn, library_id, release)
        else:
            result = library_review.save(conn, library_id, release, data.get("cards") or [], bool(data.get("reviewed")))
    except library_review.ReviewError as e:
        log_layer("storage", logging.WARNING, f"Library #{library_id}: review of {release} refused: {e}")
        return {"success": False, "error": str(e)}, 409
    log_layer("storage", logging.INFO, f"Library #{library_id}: {release} review saved: {result}")
    return result, 200


def handle_get_catalog_review(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        return catalog_review.view(conn, library_id, ((params or {}).get("name") or [""])[0]), 200
    except library_review.ReviewError as e:
        return {"success": False, "error": str(e)}, 409


def handle_post_catalog_review(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    release = str(data.get("release") or "")
    try:
        result = catalog_review.save(conn, library_id, release, data.get("cards") or [])
    except library_review.ReviewError as e:
        return {"success": False, "error": str(e)}, 409
    log_layer("storage", logging.INFO, f"Library #{library_id}: added models in {release} changed: {result}")
    audit.record(conn, "added_models_edit", False, {"release": release, **result})
    return result, 200


def handle_post_import_file(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """POST /api/import-file. Checks the picked paths now, then starts the import job."""
    data = body or {}
    try:
        paths = file_import.check_paths([str(p) for p in data.get("paths") or []])
    except file_import.ImportRefused as e:
        return {"success": False, "error": str(e)}, 400
    result = task_manager.start_task("file_import", {"paths": [str(p) for p in paths], "creator": str(data.get("creator") or "")})
    return result, 200 if result.get("success") else 409
