"""
ui/routes/catalog.py - Catalog, creators, models, releases, metrics, and Blender rendering endpoints.
Filters out 404/tombstone records and models with missing metadata from UI presentation.
Preserves 100% of pre-existing catalog exploration and metrics functionality.
"""

import json
import shutil
import sqlite3
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from desync_archival import settings as app_config
from desync_archival.db.queries import catalog as queries
from desync_archival.services.analytics import compute_catalog_audit
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.logger import logger
from desync_archival.services.mesh_renderer import VALID_VIEWS, MeshRenderer

# Global SQL filters: Exclude tombstones and records with missing designer metadata
VALID_MODEL_KNOWN = """
    name NOT LIKE '[Unavailable Model %]'
    AND designer_name IS NOT NULL
    AND designer_name != ''
    AND designer_name != 'Unknown Designer'
    AND designer_name != 'Unknown'
"""

# Lists and counts leave out unlinked models. A stash item can still open one by its ID.
VALID_MODEL_RAW = "is_active = 1 AND " + VALID_MODEL_KNOWN

VALID_MODEL_M = """
    m.is_active = 1
    AND m.name NOT LIKE '[Unavailable Model %]'
    AND m.designer_name IS NOT NULL
    AND m.designer_name != ''
    AND m.designer_name != 'Unknown Designer'
    AND m.designer_name != 'Unknown'
"""

VALID_RELEASE_COND = """
    is_active = 1
    AND designer_name IS NOT NULL
    AND designer_name != ''
    AND designer_name != 'Unknown Designer'
    AND designer_name != 'Unknown'
"""

VALID_RELEASE_R = """
    r.is_active = 1
    AND r.designer_name IS NOT NULL
    AND r.designer_name != ''
    AND r.designer_name != 'Unknown Designer'
    AND r.designer_name != 'Unknown'
"""


# -------------------------------------------------------------------------
# 1. Core Metrics & Audit Endpoints (Unchanged)
# -------------------------------------------------------------------------

def handle_get_metrics(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    counts = conn.execute(queries.GET_METRICS_1.format(VALID_MODEL_M=VALID_MODEL_M, VALID_MODEL_RAW=VALID_MODEL_RAW, VALID_RELEASE_COND=VALID_RELEASE_COND)).fetchone()

    data = {
        "total_models": counts["total_models"],
        "total_bonus_models": counts["total_bonus_models"],
        "total_creators": counts["total_creators"],
        "total_files": counts["total_files"],
        "total_library_size_bytes": counts["total_library_size_bytes"],
        "total_size_bytes": counts["total_library_size_bytes"],
        "total_downloaded_bytes": counts["total_downloaded_bytes"],
        "total_downloaded_models": counts["total_downloaded_models"],
        "total_partial_models": counts["total_partial_models"],
        "supported_models": counts["supported_models"],
        "total_releases": counts["total_releases"],
        "likely_bundles": counts["likely_bundles"],
    }
    return data, 200


def handle_get_audit(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    designer = params.get("designer", [None])[0]
    data = compute_catalog_audit(conn, designer)
    return data, 200


def handle_get_creators(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[list[dict[str, Any]], int]:
    creators = conn.execute(queries.GET_CREATORS_1.format(VALID_MODEL_M=VALID_MODEL_M)).fetchall()
    return [dict(r) for r in creators], 200


def handle_get_releases(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    designer = params.get("designer", [None])[0]
    bundles_only = params.get("bundles_only", ["0"])[0] == "1"
    limit = max(1, min(100, int(params.get("limit", [24])[0])))
    offset = max(0, int(params.get("offset", [0])[0]))

    where_clause = f" WHERE {VALID_RELEASE_R}"
    args = []
    if designer:
        where_clause += " AND (r.designer_name = ? OR r.designer_name LIKE ?)"
        args.extend([designer, f"%{designer}%"])
    if bundles_only:
        where_clause += " AND r.is_likely_bundle = 1"

    count_query = queries.GET_RELEASES_1.format(where_clause=where_clause)
    total_count = conn.execute(count_query, args).fetchone()[0]

    query = queries.GET_RELEASES_2.format(VALID_MODEL_M=VALID_MODEL_M, where_clause=where_clause)
    rows = conn.execute(query, args + [limit, offset]).fetchall()

    enriched = []
    for r in rows:
        item = dict(r)
        m_count = item.get("model_count", 0)
        c_jobs = item.get("completed_jobs", 0)
        q_jobs = item.get("queued_jobs", 0)
        dl_models = item.get("downloaded_models", 0)

        if m_count > 0 and c_jobs >= m_count:
            status = "completed"
        elif q_jobs > 0:
            status = "queued"
        elif dl_models > 0:
            status = "partial"
        else:
            status = "not_downloaded"

        item["download_status"] = status
        enriched.append(item)

    return {
        "total": total_count,
        "releases": enriched,
        "limit": limit,
        "offset": offset,
    }, 200


def handle_get_models(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    designer = params.get("designer", [None])[0]
    month = params.get("month", [None])[0]
    search = params.get("search", [""])[0].strip()
    tag = params.get("tag", [""])[0].strip()
    only_bonus = params.get("bonus", ["0"])[0] == "1"
    hide_completed = params.get("hide_completed", ["0"])[0] in ("1", "true", "True")
    sort = params.get("sort", [""])[0].strip()
    limit = max(1, min(150, int(params.get("limit", [24])[0])))
    offset = max(0, int(params.get("offset", [0])[0]))

    query = queries.GET_MODELS_1.format(VALID_MODEL_M=VALID_MODEL_M)
    count_query = queries.GET_MODELS_2.format(VALID_MODEL_M=VALID_MODEL_M)
    args = []

    if only_bonus:
        query += " AND m.is_bonus = 1"
        count_query += " AND m.is_bonus = 1"
    if designer:
        query += " AND (m.designer_name = ? OR m.designer_name LIKE ?)"
        count_query += " AND (m.designer_name = ? OR m.designer_name LIKE ?)"
        args.extend([designer, f"%{designer}%"])
    if month:
        query += " AND m.release_month = ?"
        count_query += " AND m.release_month = ?"
        args.append(month)
    if tag:
        clean_tag = tag.lstrip("#").strip().lower()
        tag_clause = " AND m.id IN (SELECT model_id FROM model_tags WHERE tag = ? OR tag LIKE ?)"
        query += tag_clause
        count_query += tag_clause
        args.extend([clean_tag, f"%{clean_tag}%"])
    if search:
        # Tags are joined in the index, so a match there is re-checked one tag at a time.
        search_clause = """ AND m.id IN (
            SELECT rowid FROM catalog_search WHERE name LIKE ?
            UNION SELECT rowid FROM catalog_search WHERE designer_name LIKE ?
            UNION SELECT model_id FROM model_tags WHERE tag LIKE ?
                AND model_id IN (SELECT rowid FROM catalog_search WHERE tags LIKE ?)
        )"""
        query += search_clause
        count_query += search_clause
        args.extend([f"%{search}%"] * 4)

    if hide_completed:
        completed_filter = """
            AND (j.status IS NULL OR j.status != 'completed')
            AND (
                (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) < (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id)
                OR (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) = 0
            )
        """
        query += completed_filter
        count_query += completed_filter

    total_count = conn.execute(count_query, args).fetchone()[0]

    if sort == "date_desc":
        order_clause = " ORDER BY m.published_at DESC, m.id DESC"
    elif sort == "date_asc":
        order_clause = " ORDER BY m.published_at ASC, m.id ASC"
    elif sort == "name_asc":
        order_clause = " ORDER BY m.name COLLATE NOCASE ASC"
    elif sort == "name_desc":
        order_clause = " ORDER BY m.name COLLATE NOCASE DESC"
    else:
        order_clause = " ORDER BY m.id DESC"

    query += f"{order_clause} LIMIT ? OFFSET ?;"
    rows = conn.execute(query, args + [limit, offset]).fetchall()

    enriched_models = []
    for r in rows:
        item = dict(r)
        tags_raw = item.pop("tags_raw", None)
        item["tags"] = [t.strip() for t in tags_raw.split("||") if t.strip()] if tags_raw else []

        item["creator_name"] = item.get("designer_name") or item.get("designer_username") or "Unknown"
        item["creator"] = item["creator_name"]
        item["total_files"] = item.get("file_count") or 0
        item["files"] = item["total_files"]
        item["date_added"] = item.get("published_at") or ""

        enriched_models.append(item)

    return {
        "total": total_count,
        "models": enriched_models,
        "limit": limit,
        "offset": offset,
    }, 200


def handle_get_model_detail(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    raw_id = None
    if "id" in params:
        val = params["id"]
        raw_id = val[0] if isinstance(val, list) else val
    elif "model_id" in params:
        val = params["model_id"]
        raw_id = val[0] if isinstance(val, list) else val

    if not raw_id or not str(raw_id).isdigit():
        return {"error": "Valid numeric model id parameter is required"}, 400

    model_id = int(raw_id)
    cursor = conn.execute(
        queries.GET_MODEL_DETAIL_1.format(VALID_MODEL_RAW=VALID_MODEL_KNOWN), 
        (model_id,)
    )
    row = cursor.fetchone()
    if not row:
        return {"error": f"Model {model_id} not found or missing metadata"}, 404

    if isinstance(row, sqlite3.Row):
        model_data = dict(row)
    else:
        cols = [d[0] for d in cursor.description]
        model_data = dict(zip(cols, row))

    try:
        tag_rows = conn.execute(
            queries.GET_MODEL_DETAIL_2, (model_id,)
        ).fetchall()
        model_data["tags"] = [r[0] for r in tag_rows if r[0]]
    except Exception:
        model_data["tags"] = []

    files = []
    try:
        has_df = conn.execute(
            queries.GET_MODEL_DETAIL_3
        ).fetchone()

        if has_df:
            file_rows = conn.execute(queries.GET_MODEL_DETAIL_4, (model_id,)).fetchall()
        else:
            file_rows = conn.execute(queries.GET_MODEL_DETAIL_5, (model_id,)).fetchall()

        for fr in file_rows:
            if isinstance(fr, sqlite3.Row):
                files.append(dict(fr))
            else:
                f_cols = ["file_id", "filename", "size_bytes", "download_url", "created_at",
                          "storage_tier", "local_relative_path", "actual_bytes", "is_downloaded"]
                files.append(dict(zip(f_cols, fr)))
    except Exception:
        files = []

    model_data["files"] = files
    model_data["total_files"] = len(files)
    model_data["total_size_bytes"] = sum(f.get("size_bytes") or 0 for f in files)

    try:
        has_dj = conn.execute(
            queries.GET_MODEL_DETAIL_6
        ).fetchone()
        if has_dj:
            job_row = conn.execute(
                queries.GET_MODEL_DETAIL_7,
                (model_id,)
            ).fetchone()
            if job_row:
                if isinstance(job_row, sqlite3.Row):
                    model_data["download_job"] = dict(job_row)
                else:
                    j_cols = ["status", "downloaded_files", "downloaded_bytes", "total_files", "total_bytes", "enqueued_at", "completed_at", "error_message"]
                    model_data["download_job"] = dict(zip(j_cols, job_row))
            else:
                model_data["download_job"] = None
        else:
            model_data["download_job"] = None
    except Exception:
        model_data["download_job"] = None

    download_status = "not_downloaded"
    job_status = (model_data.get("download_job") or {}).get("status")
    total_f = model_data.get("total_files", 0)
    downloaded_f = sum(1 for f in files if f.get("is_downloaded"))

    if job_status == "completed" or (total_f > 0 and downloaded_f >= total_f):
        download_status = "completed"
    elif job_status in ("pending", "downloading"):
        download_status = "queued"
    elif job_status == "failed":
        download_status = "failed"
    elif downloaded_f > 0:
        download_status = "partial"

    model_data["download_status"] = download_status

    raw_json = None
    raw_path_str = model_data.get("raw_backup_path")
    if raw_path_str:
        p = app_config.resolve_data_path(raw_path_str)
        if p.exists() and p.is_file():
            try:
                raw_json = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                raw_json = None

    if not raw_json:
        fallback = app_config.metadata_dir() / f"model_{model_id}.json"
        if fallback.exists() and fallback.is_file():
            try:
                raw_json = json.loads(fallback.read_text(encoding="utf-8"))
            except Exception:
                raw_json = None

    model_data["raw_metadata"] = raw_json

    def _sanitize(obj: Any) -> Any:
        if obj is None:
            return None
        if isinstance(obj, (int, float, str, bool)):
            return obj
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {str(k): _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_sanitize(v) for v in obj]
        return str(obj)

    return _sanitize(model_data), 200


def handle_get_model_files(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    raw_model_id = params.get("model_id", [None])[0]
    if not raw_model_id or not str(raw_model_id).isdigit():
        return {"error": "Valid numeric model_id parameter is required"}, 400

    model_id = int(raw_model_id)
    model = conn.execute(
        queries.GET_MODEL_FILES_1.format(VALID_MODEL_RAW=VALID_MODEL_KNOWN),
        (model_id,),
    ).fetchone()

    if not model:
        return {"error": f"Model {model_id} not found or missing metadata"}, 404

    files = conn.execute(queries.GET_MODEL_FILES_2, (model_id,)).fetchall()

    total_size = sum(f["size_bytes"] or 0 for f in files)
    return {
        "id": model["id"],
        "name": model["name"],
        "designer_name": model["designer_name"],
        "url": model["url"],
        "primary_image_url": model["primary_image_url"],
        "total_files": len(files),
        "total_size_bytes": total_size,
        "files": [dict(f) for f in files],
    }, 200


# -------------------------------------------------------------------------
# 2. Blender Render Endpoints
# -------------------------------------------------------------------------

def handle_post_model_render_view(
    conn: sqlite3.Connection,
    model_id: int,
    download_manager: Optional[DownloadManager] = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """
    POST /api/models/:id/render-view
    Renders and caches on-demand secondary perspectives to previews/<model_id>/view_<view>.jpg.
    Scoped strictly to external/in-place models (id >= 2,000,000,000 or source != 'mmf').
    Guards MMF models since they already possess official photography and API covers.
    """
    payload = body or {}
    view = str(payload.get("view", (params or {}).get("view", ["front_left"])[0])).lower().strip()
    if view not in VALID_VIEWS:
        return {"error": f"Invalid view '{view}'. Must be one of: {sorted(VALID_VIEWS)}"}, 400

    force_rerender = bool(payload.get("force", False))

    source_expr = "COALESCE(source, 'mmf') AS source"

    model = conn.execute(queries.POST_MODEL_RENDER_VIEW_1.format(source_expr=source_expr), (model_id,)).fetchone()

    if not model:
        return {"error": f"Model #{model_id} not found."}, 404

    # MMF Guard: MMF models have official photography. Render individual components via /api/render/stl instead.
    if model["source"] == "mmf" and model_id < 2_000_000_000:
        return {
            "error": f"Model #{model_id} is an MMF catalog item with official cover imagery. "
                     "Root 3D thumbnail rendering is reserved for external libraries. "
                     "To render individual meshes from this model, use the component-level render button or /api/render/stl."
        }, 400

    previews_root = app_config.previews_dir().resolve()
    model_previews_dir = previews_root / str(model_id)
    model_previews_dir.mkdir(parents=True, exist_ok=True)

    cache_filename = "cover.jpg" if view == "front_left" else f"view_{view}.jpg"
    cache_path = model_previews_dir / cache_filename

    if not force_rerender and cache_path.exists() and cache_path.stat().st_size > 0:
        return {
            "success": True,
            "model_id": model_id,
            "view": view,
            "image_url": f"/previews/{model_id}/{cache_filename}",
            "cached": True,
        }, 200

    renderer = MeshRenderer()
    if not renderer.is_available():
        return {"error": "Blender is not installed or not found on host system PATH."}, 503

    candidate_stl: Optional[Path] = None
    scratch_dir: Optional[Path] = None

    try:
        # Check if model has loose files in its vault folder
        if model["vault_path"]:
            v_path = Path(model["vault_path"])
            if v_path.exists() and v_path.is_dir():
                candidate_stl = renderer.select_best_render_candidate(v_path)
            elif v_path.exists() and v_path.is_file() and v_path.suffix.lower() == ".stl":
                candidate_stl = v_path

        # Check loose downloaded STL files recorded in SQLite
        if not candidate_stl:
            file_rows = conn.execute(queries.POST_MODEL_RENDER_VIEW_2, (model_id,)).fetchall()

            for fr in file_rows:
                for p_str in (fr["vault_path"], fr["staging_path"]):
                    if p_str:
                        c = Path(p_str)
                        if c.exists() and c.is_file() and c.suffix.lower() == ".stl":
                            candidate_stl = c
                            break
                if candidate_stl:
                    break

        # Check inside downloaded archives (.zip, .rar, .7z)
        if not candidate_stl:
            scratch_dir = Path(tempfile.gettempdir()) / f".desync_scratch_{uuid.uuid4().hex[:12]}"
            scratch_dir.mkdir(parents=True, exist_ok=True)
            candidate_stl = renderer._resolve_model_stl_candidate(conn, model_id, scratch_dir)

        if not candidate_stl or not candidate_stl.exists():
            return {"error": f"No candidate STL mesh found on storage for Model #{model_id}."}, 404

        success = renderer.render_view(stl_path=candidate_stl, view=view, output_image_path=cache_path)
        if not success or not cache_path.exists() or cache_path.stat().st_size == 0:
            return {"error": f"Blender failed rendering view '{view}' for Model #{model_id}."}, 500

        if view == "front_left" and not model["primary_image_url"]:
            with conn:
                conn.execute(queries.POST_MODEL_RENDER_VIEW_3, (f"/previews/{model_id}/cover.jpg", model_id))

        return {
            "success": True,
            "model_id": model_id,
            "view": view,
            "image_url": f"/previews/{model_id}/{cache_filename}",
            "cached": False,
        }, 200

    finally:
        if scratch_dir and scratch_dir.exists():
            shutil.rmtree(scratch_dir, ignore_errors=True)


def handle_post_render_stl(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager] = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
    task_manager: Any = None,
) -> Union[Tuple[bytes, int, Dict[str, str]], Tuple[Dict[str, Any], int]]:
    """
    POST/GET /api/render/stl
    Universal Ephemeral Render API:
    Renders individual STL components, submodel parts, or archive members on the fly.
    Returns JPEG image stream directly (Content-Type: image/jpeg) with zero permanent disk footprint.
    """
    payload = body or {}
    q_params = params or {}

    source_type = str(payload.get("source_type", q_params.get("source_type", ["file"])[0])).lower().strip()
    if source_type not in ("file", "archive_member"):
        return {"error": "Invalid source_type. Must be 'file' or 'archive_member'."}, 400

    raw_id = payload.get("id", payload.get("file_id", payload.get("member_id", q_params.get("id", [""])[0])))
    if not raw_id or not str(raw_id).isdigit():
        return {"error": "Valid numeric 'id' is required."}, 400
    source_id = int(raw_id)

    view = str(payload.get("view", q_params.get("view", ["front_left"])[0])).lower().strip()
    if view not in VALID_VIEWS:
        view = "front_left"

    renderer = MeshRenderer()
    if not renderer.is_available():
        return {"error": "Blender is not installed or not found on host system PATH."}, 503

    try:
        jpeg_bytes = renderer.render_ephemeral(
            conn=conn,
            source_type=source_type,
            source_id=source_id,
            view=view,
        )
        return jpeg_bytes, 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}
    except FileNotFoundError as fnf:
        return {"error": str(fnf)}, 404
    except ValueError as ve:
        return {"error": str(ve)}, 400
    except Exception as e:
        logger.warning(f"Ephemeral rendering failed: {e}")
        return {"error": "Ephemeral rendering failed"}, 500
