import sqlite3
from pathlib import Path
from typing import Any, Optional

from desync_archival import settings as app_config
from desync_archival.db.queries import storage as queries
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.logger import logger


def handle_get_storage_status(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Capacity, health and mount state for both NAS and staging storage."""
    if not download_manager:
        return {"error": "DownloadManager instance uninitialized"}, 500

    metrics = download_manager.storage.get_disk_metrics()
    return metrics, 200


def handle_post_storage_config(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Configures NAS root storage path and local high-speed staging path."""
    nas_path = str(body.get("download_root_path", body.get("path", ""))).strip()
    work_path = str(body.get("download_work_path", body.get("work_path", ""))).strip()

    for value in (nas_path, work_path):
        if value:
            try:
                app_config.check_settable_path(value)
            except app_config.SettingsError as e:
                return {"error": str(e)}, 400

    updates = []
    if nas_path:
        p_nas = Path(nas_path).expanduser().resolve()
        try:
            p_nas.mkdir(parents=True, exist_ok=True)
            updates.append(("download_root_path", str(p_nas)))
        except Exception as e:
            logger.warning(f"Failed to create NAS root directory {nas_path}: {e}")
            return {"error": f"Failed to initialize NAS root directory '{nas_path}'"}, 400
    elif "download_root_path" in body or "path" in body:
        updates.append(("download_root_path", ""))

    if work_path:
        p_work = Path(work_path).expanduser().resolve()
        try:
            p_work.mkdir(parents=True, exist_ok=True)
            updates.append(("download_work_path", str(p_work)))
        except Exception as e:
            logger.warning(f"Failed to create staging directory {work_path}: {e}")
            return {"error": f"Failed to initialize staging directory '{work_path}'"}, 400
    elif "download_work_path" in body or "work_path" in body:
        updates.append(("download_work_path", ""))

    with conn:
        for k, v in updates:
            conn.execute(queries.INSERT_APP_SETTINGS, (k, v))

    if download_manager:
        download_manager._load_settings()

    return {"success": True, "updated": [k for k, _ in updates]}, 200
