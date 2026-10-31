"""Gives the test suite its own data directory before any app module is imported.

APP_ENV is forced to test, but DESYNC_PROTECTED_PATHS from .env still applies, so the guard stays
active for tests. The checks below stop the run before any test can touch production.
"""

import os
import tempfile
from pathlib import Path

_DATA_DIR = tempfile.TemporaryDirectory(prefix="desync_test_data_")
os.environ["APP_ENV"] = "test"
os.environ["DESYNC_DATA_DIR"] = str(Path(_DATA_DIR.name).resolve())
# Tests use temporary folders. tests/test_allowed_roots.py narrows this to test the rule.
os.environ["DESYNC_ALLOWED_ROOTS"] = str(Path(tempfile.gettempdir()).resolve())
os.environ["DESYNC_SECRETS_FILE"] = str(Path(_DATA_DIR.name).resolve() / "secrets.json")
os.environ["DESYNC_BACKUP_DIR"] = str(Path(tempfile.gettempdir()).resolve() / f"desync_test_backups_{os.getpid()}")
# A LAN setup in .env must not change what tests run against. An empty value blocks .env's value.
os.environ["DESYNC_HOST"] = "127.0.0.1"
os.environ["DESYNC_REQUIRE_LOGIN"] = ""
os.environ["DESYNC_TLS_CERT"] = ""
os.environ["DESYNC_TLS_KEY"] = ""

from desync_archival import settings

settings.data_dir()
