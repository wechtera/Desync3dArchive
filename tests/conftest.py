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

import ipaddress
import socket

import pytest

from desync_archival import settings

settings.data_dir()


class NetworkBlocked(Exception):
    """Not an OSError on purpose, so httpx can't turn it into an ordinary connection failure."""


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _local_only(address) -> None:
    # A unix socket's address is a path. Anything else must be this machine.
    if isinstance(address, (str, bytes)):
        return
    try:
        if ipaddress.ip_address(address[0]).is_loopback:
            return
    except ValueError:
        pass
    raise NetworkBlocked(f"A test tried to reach {address[0]}. Tests must fake the network.")


def _guarded_connect(sock, address):
    _local_only(address)
    return _REAL_CONNECT(sock, address)


def _guarded_connect_ex(sock, address):
    _local_only(address)
    return _REAL_CONNECT_EX(sock, address)


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """A test that reached a real server once passed only because example.com answered 404 that day."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)


@pytest.fixture(autouse=True)
def fresh_secrets_file():
    """Secrets take priority over the database, so one test's file must not reach the next."""
    settings.secrets_file().unlink(missing_ok=True)
    yield
