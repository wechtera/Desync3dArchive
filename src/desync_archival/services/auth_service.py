"""
services/auth_service.py - MyMiniFactory Native OAuth 2.0 account profile and token lifecycle.
Instrumented with dynamic topic-based subsystem logging ('http').
"""

import json
import logging
import sqlite3
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from desync_archival.db.queries import auth_service as queries
from desync_archival.services import secret_store
from desync_archival.services.logger import log_layer, logger

MMF_USER_API_URL = "https://www.myminifactory.com/api/v2/user"
MMF_COOKIE_DOMAIN = ".myminifactory.com"
MMF_AUTHORIZE_URL = "https://auth.myminifactory.com/web/authorize"
DEFAULT_REDIRECT_URI = "http://localhost:8080/api/auth/callback"

# Never returned by the API. Each one is reported only as configured or not.
SECRET_SETTING_KEYS = secret_store.KEYS
# Single-cookie settings, with the cookie name and domain each one is sent under.
COOKIE_FIELDS = {
    "cookie_cf_clearance": ("cf_clearance", ".myminifactory.com"),
    "cookie_phpsessid": ("PHPSESSID", ".myminifactory.com"),
    "cookie_rememberme": ("REMEMBERME", "www.myminifactory.com"),
    "cookie_cf_bm": ("__cf_bm", ".myminifactory.com"),
    "cookie_cfuvid": ("_cfuvid", ".myminifactory.com"),
    "cookie_cflb": ("__cflb", "www.myminifactory.com"),
}

# Set only by the sign-in flow, never through POST /api/settings.
AUTH_MANAGED_KEYS = ("mmf_access_token", "mmf_oauth_state", "mmf_token_expires_at")


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Reads one setting, preferring the secrets file for secret keys."""
    if key in SECRET_SETTING_KEYS:
        held = secret_store.load()
        if key in held:
            return held[key]
    row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY, (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_settings(conn: sqlite3.Connection, mapping: dict[str, str]):
    """Writes settings, sending secrets to the file and everything else to app_settings."""
    secret_store.save({k: v for k, v in mapping.items() if k in SECRET_SETTING_KEYS})
    with conn:
        for k, v in mapping.items():
            if k in SECRET_SETTING_KEYS:
                continue
            conn.execute(queries.INSERT_APP_SETTINGS, (k, str(v)))


def get_auth_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Reports sign-in state. Never includes the token."""
    client_id = get_setting(conn, "mmf_client_id")
    access_token = get_setting(conn, "mmf_access_token")
    raw_expires = get_setting(conn, "mmf_token_expires_at", "0")
    user_id = get_setting(conn, "mmf_user_id")
    username = get_setting(conn, "mmf_username")

    try:
        expires_at = int(raw_expires)
    except ValueError:
        expires_at = 0

    now = int(time.time())
    is_expired = now >= expires_at if expires_at > 0 else True
    connected = bool(access_token and not is_expired)

    log_layer("http", logging.DEBUG, f"OAuth status check: connected={connected}, is_expired={is_expired}, user='{username}'")

    return {
        "configured": bool(client_id),
        "connected": connected,
        "username": username,
        "user_id": user_id,
        "expires_at": expires_at,
        "is_expired": is_expired,
        "client_id": client_id,
        "redirect_uri": get_redirect_uri(conn),
    }


def cookie_expiry(conn: sqlite3.Connection) -> float | None:
    """When the first saved cookie runs out, or None if none of them say.

    Browser exports use expirationDate and Playwright uses expires. Session cookies carry neither, or
    -1, so they can stop working at any time and this can't warn about them.
    """
    try:
        cookies = json.loads(get_setting(conn, "cookies_json", "") or "[]")
    except ValueError:
        return None
    if not isinstance(cookies, list):
        return None
    dates = []
    for cookie in cookies:
        value = (cookie.get("expirationDate") or cookie.get("expires")) if isinstance(cookie, dict) else None
        if isinstance(value, (int, float)) and value > 0:
            dates.append(float(value))
    return min(dates) if dates else None


def keep_fresh_cookies(conn: sqlite3.Connection, refreshed: str) -> int:
    """Saves what a finished scrape renewed, and returns how many changed.

    Cookies the scrape didn't return are kept, because it only reports the ones the browser held.
    """
    try:
        fresh = json.loads(refreshed)
        stored = json.loads(get_setting(conn, "cookies_json", "") or "[]")
    except ValueError:
        return 0
    if not isinstance(fresh, list) or not isinstance(stored, list):
        return 0
    by_name = {str(c.get("name", "")).lower(): c for c in stored if isinstance(c, dict)}
    changed = 0
    for cookie in fresh:
        if not isinstance(cookie, dict) or not cookie.get("name") or not cookie.get("value"):
            continue
        known = by_name.get(str(cookie["name"]).lower())
        if known is None or known.get("value") != cookie["value"]:
            by_name[str(cookie["name"]).lower()] = cookie
            changed += 1
    if changed:
        with conn:
            set_settings(conn, {"cookies_json": json.dumps(list(by_name.values())), "cookies_saved_at": str(time.time())})
    return changed


def mmf_cookies(cookies: dict[str, str]) -> httpx.Cookies:
    """Binds cookies to MMF. A plain dict has no domain, so httpx sends it to every redirect host."""
    jar = httpx.Cookies()
    for name, value in cookies.items():
        jar.set(name, value, domain=MMF_COOKIE_DOMAIN, path="/")
    return jar


def is_mmf_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == MMF_COOKIE_DOMAIN[1:] or host.endswith(MMF_COOKIE_DOMAIN))


def get_redirect_uri(conn: sqlite3.Connection) -> str:
    return get_setting(conn, "mmf_redirect_uri") or DEFAULT_REDIRECT_URI


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
