"""Settings, audit history and the small helpers the other modules share."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.api.routing import Body, Conn, Downloads, Params
from desync_archival.db.queries import settings as queries
from desync_archival.services import (
    auth_service,
    secret_store,
)
from desync_archival.services.logger import (
    log_layer,
)

DEFAULT_STASH_EXPORT_PATH = str(Path.home() / "Downloads" / "DESYNC_Print_Queue")


def format_bytes(num_bytes: int) -> str:
    """Formats byte counts into human-readable binary units (B, KB, MB, GB, TB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024**3:
        return f"{num_bytes / (1024**2):.2f} MB"
    elif num_bytes < 1024**4:
        return f"{num_bytes / (1024**3):.2f} GB"
    else:
        return f"{num_bytes / (1024**4):.2f} TB"


def _extract_tokens_from_json(raw_json: str) -> tuple[str, str]:
    cf_val, php_val = "", ""
    if not raw_json:
        return cf_val, php_val
    try:
        parsed = json.loads(raw_json.strip())
        if isinstance(parsed, list):
            for c in parsed:
                if isinstance(c, dict):
                    name = str(c.get("name", "")).strip()
                    val = str(c.get("value", "")).strip()
                    if name == "cf_clearance" and not cf_val:
                        cf_val = val
                    if (name == "PHPSESSID" or name.lower() == "phpsessid") and not php_val:
                        php_val = val
        elif isinstance(parsed, dict):
            if "cf_clearance" in parsed:
                cf_val = str(parsed["cf_clearance"]).strip()
            if "PHPSESSID" in parsed:
                php_val = str(parsed["PHPSESSID"]).strip()
            elif "phpsessid" in parsed:
                php_val = str(parsed["phpsessid"]).strip()
    except Exception:
        pass
    return cf_val, php_val


# -------------------------------------------------------------------------
# Core Settings Endpoints
# -------------------------------------------------------------------------

def _merge_cookies(existing_json: str, updates: dict[str, str]) -> str:
    """Replaces cookies named by single-cookie settings and keeps every other cookie."""
    try:
        parsed = json.loads(existing_json) if existing_json else []
    except ValueError:
        parsed = []
    if isinstance(parsed, dict):
        parsed = [{"name": str(k), "value": str(v)} for k, v in parsed.items()]
    cookies = [c for c in parsed if isinstance(c, dict)]

    for key, value in updates.items():
        name, domain = auth_service.COOKIE_FIELDS[key]
        match = next((c for c in cookies if str(c.get("name", "")).lower() == name.lower()), None)
        if match:
            match["value"] = value
            # The old expiry belongs to the old value. Kept, it can make the scraper drop a fresh cookie.
            match.pop("expirationDate", None)
            match.pop("expires", None)
        else:
            cookies.append({
                "domain": domain, "name": name, "path": "/", "value": value,
                "secure": True, "httpOnly": True,
            })
    return json.dumps(cookies)


PUBLIC_SETTINGS = frozenset({
    "batch_cooldown_seconds", "batch_size", "circuit_breaker_reason", "circuit_breaker_tripped",
    "download_root_path", "download_work_path", "last_backup_at", "last_backup_result", "log_layers", "log_level",
    "metadata_backup_path", "mmf_client_id", "mmf_user_id", "mmf_username", "offload_chunk_size_mb",
    "offloader_concurrency", "scraper_headless", "stash_download_path", "stream_chunk_size_mb",
    "worker_concurrency",
})


def handle_get_settings(
    conn: Conn,
) -> tuple[dict[str, Any], int]:
    """Retrieves all key-value configuration pairs from app_settings."""
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(queries.SELECT_APP_SETTINGS).fetchall()
        settings = {r["key"]: r["value"] for r in rows} if rows else {}
    except Exception:
        settings = {}

    secret_store.overlay(settings)
    if not settings.get("stash_download_path"):
        settings["stash_download_path"] = DEFAULT_STASH_EXPORT_PATH

    # Only listed keys go out, so a secret added later stays on the server until someone chooses
    # otherwise. The UI only learns whether each known secret is set.
    configured = {key: bool(settings.get(key, "")) for key in auth_service.SECRET_SETTING_KEYS}
    public = {key: value for key, value in settings.items() if key in PUBLIC_SETTINGS}
    public["secrets_configured"] = configured

    return public, 200


def handle_post_settings(
    conn: Conn,
    download_manager: Downloads,
    body: Body,
) -> tuple[dict[str, Any], int]:
    """Persists settings key-value pairs into app_settings and reloads the download manager."""
    data = dict(body or {})

    for key in app_config.PATH_SETTING_KEYS:
        if data.get(key):
            try:
                app_config.check_settable_path(app_config.resolve_data_path(str(data[key])))
            except app_config.SettingsError as e:
                return {"error": str(e)}, 400

    for key in auth_service.AUTH_MANAGED_KEYS:
        data.pop(key, None)
    # Forms never receive secrets, so a blank field means "keep the current value".
    for key in auth_service.SECRET_SETTING_KEYS:
        if key in data and not str(data[key] or "").strip():
            del data[key]

    if data.get("cookies_json"):
        cf_extracted, php_extracted = _extract_tokens_from_json(data["cookies_json"])
        if cf_extracted and not data.get("cookie_cf_clearance"):
            data["cookie_cf_clearance"] = cf_extracted
        if php_extracted and not data.get("cookie_phpsessid"):
            data["cookie_phpsessid"] = php_extracted

    # Single fields apply on top of the list, including one just pasted as JSON.
    single_cookies = {key: str(data[key]) for key in auth_service.COOKIE_FIELDS if data.get(key)}
    if len(set(single_cookies.values())) < len(single_cookies):
        return {"error": "Two cookie fields hold the same value. Each cookie has its own value, so nothing was saved."}, 400
    if single_cookies:
        base = data.get("cookies_json") or auth_service.get_setting(conn, "cookies_json")
        data["cookies_json"] = _merge_cookies(base, single_cookies)

    secret_store.save({k: v for k, v in data.items() if k in secret_store.KEYS})
    with conn:
        for k, v in data.items():
            if k not in secret_store.KEYS:
                conn.execute(queries.INSERT_APP_SETTINGS, (str(k), str(v) if v is not None else ""))

    if download_manager and hasattr(download_manager, "_load_settings"):
        download_manager._load_settings()

    log_layer("sql", logging.INFO, f"Settings updated: {list(data.keys())}")
    return {"success": True, "message": "Settings updated successfully."}, 200


def handle_get_audit_history(
    conn: Conn,
    params: Params,
) -> tuple[dict[str, Any], int]:
    conn.row_factory = sqlite3.Row
    action = ((params or {}).get("action") or [""])[0]
    rows = conn.execute(
        "SELECT id, action, dry_run, outcome, detail, created_at FROM audit_log"
        " WHERE ?1 = '' OR action = ?1 ORDER BY id DESC LIMIT 200;", (action,),
    ).fetchall()
    actions = [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action;").fetchall()]
    return {"entries": [dict(r) for r in rows], "actions": actions}, 200
