"""Secrets in the 0600 file outside the repository and data folder, never in the database.

A value in the file wins. During the transition a key missing from the file falls back to
app_settings. Writes always go to the file, so a new sign-in can't be hidden by an older value.
"""

import json
import os
import sqlite3
import threading
from pathlib import Path

from desync_archival import settings as app_config
from desync_archival.db.queries import secret_store as queries
from desync_archival.services.logger import logger

KEYS = (
    "PHPSESSID",
    "cf_clearance",
    "cookie_cf_clearance",
    "cookie_cf_bm",
    "cookie_cfuvid",
    "cookie_cflb",
    "cookie_phpsessid",
    "cookie_rememberme",
    "cookies_json",
    "mmf_access_token",
    "mmf_client_secret",
    "mmf_oauth_state",
    "mmf_refresh_token",
)

_lock = threading.Lock()


class SecretsError(RuntimeError):
    """Secrets can't be moved safely."""


def read_file(path: Path | None = None) -> dict:
    """Reads the secrets file. Any failure reads as empty."""
    try:
        return json.loads((path or app_config.secrets_file()).read_text(encoding="utf-8"))
    # FIXME(R-09): a failed read looks empty, and save() then replaces the whole file, password hash included.
    except (OSError, ValueError):
        return {}


def write_file(data: dict) -> Path:
    """Replaces the secrets file through a 0600 temp file."""
    path = app_config.secrets_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    temp.replace(path)
    return path


def load() -> dict[str, str]:
    return read_file().get("settings", {})


def overlay(settings: dict) -> dict:
    """Puts file values over a dict read from app_settings."""
    settings.update(load())
    return settings


def save(mapping: dict[str, str]) -> None:
    if not mapping:
        return
    with _lock:
        data = read_file()
        data.setdefault("settings", {}).update({key: str(value) for key, value in mapping.items()})
        write_file(data)
    logger.info(f"Saved {len(mapping)} secrets to the secrets file")


def stored_in_database(conn: sqlite3.Connection) -> dict[str, str]:
    marks = ",".join("?" * len(KEYS))
    rows = conn.execute(f"SELECT key, value FROM app_settings WHERE key IN ({marks})", KEYS).fetchall()
    return {row[0]: row[1] for row in rows if row[1]}


def export_from_database(conn: sqlite3.Connection) -> list[str]:
    """Copies database secrets to the file. A key already in the file is newer, so it stays."""
    existing = load()
    pending = {key: value for key, value in stored_in_database(conn).items() if key not in existing}
    save(pending)
    return sorted(pending)


def blank_database(conn: sqlite3.Connection) -> list[str]:
    """Clears database copies, but only when the file holds every one of them."""
    held = load()
    stored = stored_in_database(conn)
    missing = sorted(key for key in stored if not held.get(key))
    if missing:
        raise SecretsError(f"Not in the secrets file, so nothing was blanked: {missing}")
    with conn:
        conn.executemany(queries.UPDATE_APP_SETTINGS_VALUE_BY_KEY, [(key,) for key in stored])
    logger.warning(f"Blanked {len(stored)} secrets in app_settings")
    return sorted(stored)
