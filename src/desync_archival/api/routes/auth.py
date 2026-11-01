import secrets
import sqlite3
import time
import urllib.parse
from typing import Any

from desync_archival.services import auth_service
from desync_archival.services.downloader import DownloadManager


def handle_get_auth_status(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """GET /api/auth/status. Retries the profile lookup when a fresh token has no username yet."""
    status = auth_service.get_auth_status(conn)
    if status["connected"] and not status["username"]:
        # A token can fail the profile lookup in the moment after sign-in, so try again here.
        username, user_id = auth_service.fetch_user_profile(auth_service.get_setting(conn, "mmf_access_token"))
        if username:
            auth_service.set_settings(conn, {"mmf_username": username, "mmf_user_id": str(user_id or "")})
            status.update(username=username, user_id=user_id)
    return status, 200


def handle_post_auth_config(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    client_id = str(body.get("client_id", "")).strip()
    redirect_uri = str(body.get("redirect_uri", "")).strip()

    if not client_id and not redirect_uri:
        return {"error": "A client_id must be provided"}, 400

    updates = {}
    if "client_id" in body:
        updates["mmf_client_id"] = client_id
    if redirect_uri:
        parts = urllib.parse.urlsplit(redirect_uri)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return {"error": "redirect_uri must be an http or https URL"}, 400
        updates["mmf_redirect_uri"] = redirect_uri

    auth_service.set_settings(conn, updates)
    return {"success": True, "configured": True}, 200


def reload_workers(download_manager: DownloadManager | None) -> None:
    """Workers keep a copy of the token, so they must see every sign-in and disconnect."""
    if download_manager:
        download_manager._load_settings()


def state_matches(conn: sqlite3.Connection, supplied: Any) -> bool:
    """Checks the single-use OAuth state, then clears it so it can't be replayed."""
    stored = auth_service.get_setting(conn, "mmf_oauth_state")
    auth_service.set_settings(conn, {"mmf_oauth_state": ""})
    return bool(stored) and secrets.compare_digest(stored.encode(), str(supplied or "").encode())


def handle_get_auth_login(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[Any, ...]:
    client_id = auth_service.get_setting(conn, "mmf_client_id")
    if not client_id:
        return {"error": "MyMiniFactory client_id (application slug) is not configured"}, 400

    state = secrets.token_hex(16)
    auth_service.set_settings(conn, {"mmf_oauth_state": state})

    query_params = {
        "client_id": client_id,
        "redirect_uri": auth_service.get_redirect_uri(conn),
        "response_type": "token",
        "state": state,
    }
    auth_url = f"{auth_service.MMF_AUTHORIZE_URL}?{urllib.parse.urlencode(query_params)}"

    if params.get("redirect", ["1"])[0] == "0":
        return {"auth_url": auth_url, "state": state}, 200

    return "", 302, {"Location": auth_url}


def handle_get_auth_callback(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[Any, ...]:
    """
    Serves the callback page. In an Implicit flow, MMF sends the token in the URL hash fragment (#access_token=...),
    which browsers do not send to servers via HTTP. This script extracts the hash and POSTs it to /api/auth/token_callback.
    """
    html_page = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>DESYNC 3D Vault - Connecting...</title>
  <style>
    body { background: #0b0f19; color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #151d30; border: 1px solid #263554; border-radius: 12px; padding: 28px; max-width: 440px; text-align: center; }
    .spinner { border: 3px solid #263554; border-top: 3px solid #00c4a6; border-radius: 50%; width: 28px; height: 28px; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    .error { color: #fca5a5; }
  </style>
</head>
<body>
  <div class="card" id="status-card">
    <div class="spinner" id="spinner"></div>
    <h3 id="status-title" style="margin-bottom:8px; color:#00c4a6;">Authorizing Account...</h3>
    <p id="status-desc" style="color:#94a3b8; font-size:0.9rem;">Receiving token from MyMiniFactory...</p>
  </div>

  <script type="module" src="/static/js/auth_callback.js"></script>
</body>
</html>"""
    return html_page, 200


def handle_post_auth_token_callback(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Receives the access token from the browser hash fragment, validates profile, and persists."""
    access_token = str(body.get("access_token", "")).strip()
    expires_in = int(body.get("expires_in", 21600))

    if not access_token:
        return {"error": "Missing access_token"}, 400

    if not state_matches(conn, body.get("state")):
        return {"error": "Sign-in state did not match. Start the connection again."}, 403

    username, user_id = auth_service.fetch_user_profile(access_token)
    expires_at = int(time.time()) + expires_in

    auth_service.set_settings(conn, {
        "mmf_access_token": access_token,
        "mmf_token_expires_at": str(expires_at),
        "mmf_user_id": str(user_id or ""),
        "mmf_username": str(username or ""),
        "mmf_oauth_state": "",
    })
    reload_workers(download_manager)

    return {
        "success": True,
        "username": username,
        "user_id": user_id,
        "expires_at": expires_at,
    }, 200


def handle_post_auth_disconnect(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    auth_service.set_settings(conn, {
        "mmf_access_token": "",
        "mmf_token_expires_at": "0",
        "mmf_user_id": "",
        "mmf_username": "",
        "mmf_oauth_state": "",
    })
    reload_workers(download_manager)
    return {"success": True, "connected": False}, 200
