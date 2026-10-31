"""Starts the real server over TLS with login on, using a throwaway database and certificate."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from db_helper import create_database

with tempfile.TemporaryDirectory(prefix="desync_lan_") as temp:
    data = Path(temp).resolve()
    create_database(data / "library.db")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-days", "1",
         "-keyout", str(data / "key.pem"), "-out", str(data / "cert.pem")],
        check=True, capture_output=True,
    )
    env = {
        **os.environ, "APP_ENV": "test", "DESYNC_DATA_DIR": str(data), "DESYNC_PORT": sys.argv[1],
        "DESYNC_ALLOWED_ROOTS": str(data), "DESYNC_REQUIRE_LOGIN": "1",
        "DESYNC_SECRETS_FILE": str(data / "secrets.json"),
        "DESYNC_TLS_CERT": str(data / "cert.pem"), "DESYNC_TLS_KEY": str(data / "key.pem"),
    }
    python = str(ROOT / ".venv/bin/python")
    subprocess.run([python, "-m", "desync_archival.dbtool", "set-password"], input=sys.argv[2] + "\n", text=True,
                   env=env, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([python, "-m", "desync_archival.api.server"], env=env, check=False)
