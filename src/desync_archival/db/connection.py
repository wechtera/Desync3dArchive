"""The one place that opens SQLite. A ruff rule bans sqlite3.connect everywhere else.

services/db.py adds the settings a working connection wants. This stays bare for migrations and
backups, which need the file exactly as it is, and for the logger, which loads before services.db.
"""

import sqlite3
from pathlib import Path

BUSY_SECONDS = 45.0


def connect(
    path: Path | str, readonly: bool = False, factory: type[sqlite3.Connection] = sqlite3.Connection,
    shared: bool = False,
) -> sqlite3.Connection:
    options = {"timeout": BUSY_SECONDS, "factory": factory, "check_same_thread": not shared}
    if readonly:
        return sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, **options)
    return sqlite3.connect(str(path), **options)
