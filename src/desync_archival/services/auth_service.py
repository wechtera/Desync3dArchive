"""
services/auth_service.py - MyMiniFactory Native OAuth 2.0 account profile and token lifecycle.
Instrumented with dynamic topic-based subsystem logging ('http').
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from desync_archival.services.logger import log_layer, logger

MMF_USER_API_URL = "https://www.myminifactory.com/api/v2/user"
MMF_AUTHORIZE_URL = "https://auth.myminifactory.com/web/authorize"
MMF_TOKEN_URL = "https://auth.myminifactory.com/v1/oauth/tokens"
DEFAULT_REDIRECT_URI = "http://localhost:8080/api/auth/callback"
DEFAULT_EXPIRES_IN = 3600
FLOW_IMPLICIT = "implicit"
FLOW_CODE = "authorization_code"
REFRESH_MARGIN_SECONDS = 300

# Never returned by the API. Each one is reported only as configured or not.
SECRET_SETTING_KEYS = (
    "PHPSESSID",
    "cf_clearance",
    "cookie_cf_clearance",
    "cookie_phpsessid",
    "cookies_json",
    "mmf_access_token",
    "mmf_client_secret",
    "mmf_oauth_state",
    "mmf_refresh_token",
)
# Set only by the sign-in flow, never through POST /api/settings.
AUTH_MANAGED_KEYS = ("mmf_access_token", "mmf_oauth_state", "mmf_refresh_token", "mmf_token_expires_at")

# MMF may replace the refresh token on each use and revoke the old one, so only one refresh can
# run at a time and every other caller reuses its result.
_refresh_lock = threading.Lock()


class TokenError(Exception):
    """MMF refused a token request. The message never includes a token or secret."""


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_settings(conn: sqlite3.Connection, mapping: dict[str, str]):
    with conn:
        for k, v in mapping.items():
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """, (k, str(v)))


def get_auth_status(conn: sqlite3.Connection) -> dict[str, Any]:
    client_id = get_setting(conn, "mmf_client_id")
    client_secret = get_setting(conn, "mmf_client_secret")
    access_token = get_setting(conn, "mmf_access_token")
    refresh_token = get_setting(conn, "mmf_refresh_token")
    raw_expires = get_setting(conn, "mmf_token_expires_at", "0")
    user_id = get_setting(conn, "mmf_user_id")
    username = get_setting(conn, "mmf_username")

    try:
        expires_at = int(raw_expires)
    except ValueError:
        expires_at = 0

    now = int(time.time())
    is_expired = now >= expires_at if expires_at > 0 else True
    connected = bool(access_token and (not is_expired or refresh_token))

    log_layer("http", logging.DEBUG, f"OAuth status check: connected={connected}, is_expired={is_expired}, user='{username}'")

    return {
        "configured": bool(client_id),
        "connected": connected,
        "username": username,
        "user_id": user_id,
        "expires_at": expires_at,
        "is_expired": is_expired,
        "client_id": client_id,
        "secret_configured": bool(client_secret),
        "refresh_configured": bool(refresh_token),
        "flow": get_auth_flow(conn),
        "redirect_uri": get_redirect_uri(conn),
    }


def get_auth_flow(conn: sqlite3.Connection) -> str:
    """The implicit grant is the default. MMF applications may hold only an API key, which the
    code grant rejects, so a stored secret alone must not switch flows."""
    return FLOW_CODE if get_setting(conn, "mmf_auth_flow") == FLOW_CODE else FLOW_IMPLICIT


def get_redirect_uri(conn: sqlite3.Connection) -> str:
    return get_setting(conn, "mmf_redirect_uri") or DEFAULT_REDIRECT_URI


def describe_refusal(resp: httpx.Response) -> str:
    """Names why MMF refused, using only the standard OAuth error fields."""
    try:
        body = resp.json()
    except ValueError:
        return f", {resp.headers.get('content-type', 'unknown content')}, server {resp.headers.get('server', 'unknown')}"
    if not isinstance(body, dict):
        return ""
    detail = str(body.get("error_description") or body.get("error") or "")[:200]
    return f", {detail}" if detail else ""


def request_tokens(client_id: str, client_secret: str, form: dict[str, str]) -> dict[str, Any]:
    """Posts to the MMF token endpoint with HTTP Basic client authentication."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(MMF_TOKEN_URL, auth=(client_id, client_secret), data=form)
    except httpx.HTTPError as e:
        raise TokenError(f"Could not reach the MMF token endpoint: {type(e).__name__}") from e

    grant = form.get("grant_type")
    log_layer("http", logging.INFO, f"MMF token request ({grant}): HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise TokenError(f"MMF refused the {grant} request (HTTP {resp.status_code}{describe_refusal(resp)})")
    try:
        payload = resp.json()
    except ValueError as e:
        raise TokenError("MMF returned an unreadable token response") from e
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise TokenError("MMF token response has no access token")
    return payload


def store_token_response(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    """Saves a token response. A response without a refresh token keeps the current one."""
    try:
        expires_in = int(payload.get("expires_in") or DEFAULT_EXPIRES_IN)
    except (TypeError, ValueError):
        expires_in = DEFAULT_EXPIRES_IN
    expires_at = int(time.time()) + expires_in

    updates = {
        "mmf_access_token": str(payload["access_token"]),
        "mmf_token_expires_at": str(expires_at),
    }
    if payload.get("refresh_token"):
        updates["mmf_refresh_token"] = str(payload["refresh_token"])
    set_settings(conn, updates)
    return expires_at


def exchange_code(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    """Trades an authorization code for tokens on the server side."""
    payload = request_tokens(
        get_setting(conn, "mmf_client_id"),
        get_setting(conn, "mmf_client_secret"),
        {"grant_type": "authorization_code", "code": code, "redirect_uri": get_redirect_uri(conn)},
    )
    store_token_response(conn, payload)
    return payload


def _token_connection(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=45.0)
    conn.row_factory = sqlite3.Row
    return conn


def refresh_tokens(db_path: Path | str, stale_token: str | None = None) -> str:
    """Returns a usable access token, refreshing it when needed.

    With stale_token, the caller saw that token rejected, so a refresh happens unless another
    thread already replaced it. Without it, a refresh happens only near expiry. If a refresh is
    not possible or fails, the stored token is returned and the caller keeps its own failure path.
    """
    with _refresh_lock:
        conn = _token_connection(db_path)
        try:
            token = get_setting(conn, "mmf_access_token")
            refresh_token = get_setting(conn, "mmf_refresh_token")
            if not refresh_token:
                return token

            if stale_token is not None:
                if token != stale_token:
                    return token
            else:
                try:
                    expires_at = int(get_setting(conn, "mmf_token_expires_at", "0"))
                except ValueError:
                    expires_at = 0
                if time.time() < expires_at - REFRESH_MARGIN_SECONDS:
                    return token

            try:
                payload = request_tokens(
                    get_setting(conn, "mmf_client_id"),
                    get_setting(conn, "mmf_client_secret"),
                    {"grant_type": "refresh_token", "refresh_token": refresh_token},
                )
            except TokenError as e:
                logger.warning(f"Token refresh failed: {e}")
                return token

            store_token_response(conn, payload)
            log_layer("http", logging.INFO, "MMF access token refreshed")
            return str(payload["access_token"])
        finally:
            conn.close()


def fetch_user_profile(access_token: str) -> tuple[str, str]:
    """Queries MMF v2 API with the bearer token to obtain username and user ID."""
    username = ""
    user_id = ""
    log_layer("http", logging.DEBUG, f"Querying MMF User Profile: GET {MMF_USER_API_URL}")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                MMF_USER_API_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            log_layer("http", logging.DEBUG, f"MMF User Profile response: HTTP {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("username") or data.get("name") or ""
                user_id = str(data.get("id") or "")
                log_layer("http", logging.INFO, f"OAuth connected to MyMiniFactory user: '{username}' (User ID: {user_id})")
            elif resp.status_code == 401:
                logger.warning("MMF User Profile query returned HTTP 401: Invalid or expired Bearer token.")
            else:
                logger.warning(f"MMF User Profile returned HTTP {resp.status_code}")
    except Exception as e:
        log_layer("http", logging.ERROR, f"OAuth Profile network error: {e}")

    return username, user_id
