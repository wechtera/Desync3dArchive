"""Builds test databases from the real migrations."""

import sqlite3
from pathlib import Path

from desync_archival.migrations import apply_pending
from desync_archival.services.db import get_db_connection, seed_default_settings


def init_persistent_tables(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    apply_pending(conn)
    seed_default_settings(conn)


def create_database(db_path: Path | str) -> None:
    conn = get_db_connection(db_path)
    init_persistent_tables(conn)
    conn.close()
