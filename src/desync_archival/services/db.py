"""
services/db.py - Settings defaults and the SQLite connection factory.
"""

from pathlib import Path
import sqlite3
from typing import Union

from desync_archival import settings as app_config


def get_db_connection(db_path: Union[Path, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=45.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 45000;")
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
            ("mmf_client_secret", ""),
            ("mmf_access_token", ""),
            ("mmf_refresh_token", ""),
            ("mmf_token_expires_at", "0"),
            ("mmf_user_id", ""),
            ("mmf_username", ""),
            ("mmf_oauth_state", ""),
        ]
        conn.executemany("""
            INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)
        """, defaults)

        # Seed cookies_json if row exists but is empty
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'cookies_json';").fetchone()
        if row:
            val = row[0]
            if (not val or str(val).strip() in ("", "{}")) and initial_cookies != "{}":
                conn.execute("UPDATE app_settings SET value = ? WHERE key = 'cookies_json';", (initial_cookies,))
