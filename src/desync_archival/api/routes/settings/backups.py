"""Backups and their folders."""

import logging
from pathlib import Path
from typing import Any

from desync_archival import backup
from desync_archival import settings as app_config
from desync_archival.api.routing import Body, Conn, Tasks
from desync_archival.services import (
    auth_service,
)
from desync_archival.services.logger import (
    log_layer,
)


def handle_get_backups(
    conn: Conn,
) -> tuple[dict[str, Any], int]:
    try:
        listing = backup.list_backups()
    except (OSError, app_config.SettingsError, backup.BackupError) as e:
        return {"success": False, "error": str(e)}, 409
    return {
        **listing,
        "last_backup_at": auth_service.get_setting(conn, "last_backup_at", ""),
        "last_backup_result": auth_service.get_setting(conn, "last_backup_result", ""),
    }, 200


def _save_backup_paths(backup_path: str, mirror_path: str) -> tuple[dict[str, Any], int]:
    data = app_config.data_dir().resolve()
    for label, value in (("Backup folder", backup_path), ("Second copy folder", mirror_path)):
        path = Path(value).expanduser()
        if not value and label == "Second copy folder":
            continue
        if not path.is_absolute():
            return {"success": False, "error": f"{label} must be a full path."}, 400
        if path.resolve() == data or data in path.resolve().parents:
            return {"success": False, "error": f"{label} must be outside the data folder."}, 400
        if not path.parent.is_dir():
            return {"success": False, "error": f"{path.parent} isn't available. Is the drive mounted?"}, 400
        # A full backup holds the secrets file, so this can't be anywhere the app happens to reach.
        try:
            app_config.check_settable_path(path)
        except app_config.SettingsError as e:
            return {"success": False, "error": str(e)}, 403
    app_config.save_backup_paths(backup_path, mirror_path)
    log_layer("storage", logging.INFO, "Backup folders changed in Settings")
    return {"success": True}, 200


def handle_post_backups(
    body: Body,
    task_manager: Tasks,
) -> tuple[dict[str, Any], int]:
    """Restoring is deliberately absent. It needs the app stopped, so it stays a command."""
    data = body or {}
    action = str(data.get("action") or "")
    if action == "run":
        result = task_manager.start_task("database_backup")
        return result, 200 if result.get("success") else 409
    if action == "verify":
        try:
            return {"success": True, **backup.check_backup(str(data.get("name") or ""))}, 200
        except backup.BackupError as e:
            return {"success": False, "error": str(e)}, 400
    if action == "paths":
        return _save_backup_paths(str(data.get("backup_dir") or "").strip(), str(data.get("mirror_dir") or "").strip())
    return {"success": False, "error": f"Unknown action '{action}'"}, 400
