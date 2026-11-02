"""
services/db.py - Settings defaults and the SQLite connection factory.
"""

import sqlite3
from pathlib import Path
from typing import cast

from desync_archival import settings as app_config
from desync_archival.db.connection import connect
from desync_archival.db.queries import db as queries
from desync_archival.services import secret_store

# FULL fsyncs every commit. In WAL mode a power cut under NORMAL can lose the last commits but can't
# corrupt the database, and the owner accepted that on 2026-11-01 (P-02).
SYNCHRONOUS = "PRAGMA synchronous = NORMAL;"


class ScopedConnection(sqlite3.Connection):
    """Closes when its outermost `with` ends.

    sqlite3's own `with` only commits, and a dropped connection can't close itself, because it
    sits in a reference cycle with its statement cache. Each one kept its file handles until the
    garbage collector ran, and a burst of them hit the 256 file limit and stopped the server (E48).
    Nested `with conn:` blocks still just commit, so helpers that take a connection keep working.
    """

    _depth = 0

    def __enter__(self):
        self._depth += 1
        return super().__enter__()

    def __exit__(self, *exc_info):
        self._depth -= 1
        try:
            return super().__exit__(*exc_info)
        finally:
            if self._depth == 0:
                self.close()


def get_db_connection(db_path: Path | str, shared: bool = False) -> sqlite3.Connection:
    """A connection its owner closes. For one block of work, use `scoped_connection`.

    `shared` is for a request's connection, which a dependency opens in one worker thread and a
    handler uses in another. They take turns, so nothing touches it at the same time.
    """
    conn = connect(db_path, shared=shared)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 45000;")
    conn.execute(SYNCHRONOUS)
    return conn


def scoped_connection(db_path: Path | str, foreign_keys: bool = True) -> ScopedConnection:
    conn = cast(ScopedConnection, connect(db_path, factory=ScopedConnection))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 45000;")
    conn.execute(SYNCHRONOUS)
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def seed_default_settings(conn: sqlite3.Connection):
    """Adds any missing settings rows. The schema itself lives in migrations/."""
    conn.row_factory = sqlite3.Row
    with conn:
        initial_cookies = "{}"
        cookies_disk_file = app_config.data_dir() / "cookies.json"
        if cookies_disk_file.exists():
            try:
                content = cookies_disk_file.read_text(encoding="utf-8").strip()
                if content:
                    initial_cookies = content
            except Exception:
                initial_cookies = "{}"

        defaults = [
            ("download_root_path", ""),
            ("download_work_path", ""),
            ("metadata_backup_path", "metadata"),
            ("worker_concurrency", "2"),
            ("offloader_concurrency", "1"),
            ("batch_size", "5"),
            ("batch_cooldown_seconds", "3.0"),
            ("circuit_breaker_tripped", "0"),
            ("circuit_breaker_reason", ""),
            ("cookies_json", initial_cookies),
            ("mmf_client_id", ""),
            ("mmf_access_token", ""),
            ("mmf_token_expires_at", "0"),
            ("mmf_user_id", ""),
            ("mmf_username", ""),
            ("mmf_oauth_state", ""),
        ]
        conn.executemany(queries.INSERT_APP_SETTINGS, defaults)

        # A blanked row looks empty, so without this check every start refilled it from the old file.
        if secret_store.load().get("cookies_json"):
            return
        # Seed cookies_json if row exists but is empty
        row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY).fetchone()
        if row:
            val = row[0]
            if (not val or str(val).strip() in ("", "{}")) and initial_cookies != "{}":
                conn.execute(queries.UPDATE_APP_SETTINGS_VALUE_BY_KEY, (initial_cookies,))
