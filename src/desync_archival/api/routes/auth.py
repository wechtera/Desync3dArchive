import logging
import secrets
import sqlite3
import time
import urllib.parse
from typing import Any

from desync_archival.services import auth_service
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.logger import log_layer


def handle_get_auth_status(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    status = auth_service.get_auth_status(conn)
    return status, 200


def handle_post_auth_config(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", body.get("api_key", ""))).strip()
    redirect_uri = str(body.get("redirect_uri", "")).strip()

    if not client_id and not client_secret and not redirect_uri:
        return {"error": "Either client_id or api_key must be provided"}, 400

    updates = {}
    if "client_id" in body:
        updates["mmf_client_id"] = client_id
    # A blank secret means "keep the current one", because the form never receives it.
    if client_secret:
        updates["mmf_client_secret"] = client_secret
    if redirect_uri:
        parts = urllib.parse.urlsplit(redirect_uri)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return {"error": "redirect_uri must be an http or https URL"}, 400
        updates["mmf_redirect_uri"] = redirect_uri

    auth_service.set_settings(conn, updates)
    return {"success": True, "configured": True}, 200


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

    # The authorization code grant needs the client secret. Without one, use the implicit grant.
    response_type = "code" if auth_service.get_setting(conn, "mmf_client_secret") else "token"
    query_params = {
        "client_id": client_id,
        "redirect_uri": auth_service.get_redirect_uri(conn),
        "response_type": response_type,
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
    Handles the MMF redirect. The authorization code grant is finished here on the server.
    In an Implicit flow, MMF sends the token in the URL hash fragment (#access_token=...),
    which browsers do not send to servers via HTTP. This script extracts the hash and POSTs it to /api/auth/token_callback.
    """
    code = params.get("code", [""])[0]
    if code:
        if not state_matches(conn, params.get("state", [""])[0]):
            return {"error": "Sign-in state did not match. Start the connection again."}, 400
        try:
            payload = auth_service.exchange_code(conn, code)
        except auth_service.TokenError as e:
            log_layer("http", logging.WARNING, f"Authorization code exchange failed: {e}")
            return {"error": "MyMiniFactory did not accept the sign-in. Try connecting again."}, 502

        username, user_id = auth_service.fetch_user_profile(str(payload["access_token"]))
        auth_service.set_settings(conn, {
            "mmf_user_id": str(user_id or payload.get("user_id") or ""),
            "mmf_username": str(username or ""),
        })
        return "", 302, {"Location": "/#settings"}

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

  <script>
    (async function() {
      const hash = window.location.hash.substring(1);
      const search = window.location.search.substring(1);
      const params = new URLSearchParams(hash || search);

      const accessToken = params.get('access_token');
      const expiresIn = params.get('expires_in');
      const state = params.get('state');
      const error = params.get('error') || params.get('error_description');

      const card = document.getElementById('status-card');
      const title = document.getElementById('status-title');
      const desc = document.getElementById('status-desc');
      const spinner = document.getElementById('spinner');

      if (error) {
        spinner.style.display = 'none';
        title.className = 'error';
        title.textContent = 'Authorization Denied';
        desc.textContent = error;
        return;
      }

      if (!accessToken) {
        spinner.style.display = 'none';
        title.className = 'error';
        title.textContent = 'Token Missing';
        desc.textContent = 'No access token was returned by MyMiniFactory.';
        return;
      }

      try {
        const res = await fetch('/api/auth/token_callback', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            access_token: accessToken,
            expires_in: expiresIn || 21600,
            state: state
          })
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to save token');

        spinner.style.display = 'none';
        title.textContent = 'Connected Successfully!';
        desc.innerHTML = 'Connected as <strong>@' + (data.username || 'User') + '</strong>. Redirecting...';

        setTimeout(() => {
          window.location.href = '/#settings';
        }, 1200);
      } catch (err) {
        spinner.style.display = 'none';
        title.className = 'error';
        title.textContent = 'Connection Error';
        desc.textContent = err.message;
      }
    })();
  </script>
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
        "mmf_refresh_token": "",
        "mmf_token_expires_at": "0",
        "mmf_user_id": "",
        "mmf_username": "",
        "mmf_oauth_state": "",
    })
    return {"success": True, "connected": False}, 200
