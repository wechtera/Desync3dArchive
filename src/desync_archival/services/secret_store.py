"""Secrets in the 0600 file outside the repository and data folder, never in the database.

A value in the file wins. During the transition a key missing from the file falls back to
app_settings. Writes always go to the file, so a new sign-in can't be hidden by an older value.
"""

import fcntl
import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
    """Reads the secrets file. Only a missing file reads as empty.

    Raises:
        SecretsError: The file exists but can't be read or parsed. Treating that as empty
            would let the next write replace the password hash and every token.
    """
    path = path or app_config.secrets_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise SecretsError(f"Can't read the secrets file: {e.strerror or type(e).__name__}") from e
    # Never put the parse error's text here. It can quote the file.
    except ValueError:
        raise SecretsError("The secrets file isn't valid JSON") from None
    if not isinstance(data, dict):
        raise SecretsError("The secrets file doesn't hold a JSON object")
    return data


def write_file(data: dict) -> Path:
    """Replaces the secrets file through a unique 0600 temp file, synced before the rename."""
    path = app_config.secrets_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return path


@contextmanager
def _exclusive() -> Iterator[None]:
    """One writer across threads and processes, because `dbtool` runs beside the server."""
    # NOTE: a sidecar, since write_file replaces the secrets file and a lock on it would go too.
    # flock belongs to the open file rather than the thread, so `_lock` is still needed.
    # POSIX only, so E40's Windows work needs another lock here.
    path = app_config.secrets_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _lock:
        descriptor = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


@contextmanager
def editing_file() -> Iterator[dict]:
    """The file's contents, written back when the block ends. A failed read stops the write."""
    with _exclusive():
        data = read_file()
        yield data
        write_file(data)


def load() -> dict[str, str]:
    """Settings from the file. Reads run constantly, so a failed one logs and reads as empty."""
    try:
        return read_file().get("settings", {})
    except SecretsError as e:
        logger.error(f"{e}. Secrets read as empty until this is fixed, and saving is refused.")
        return {}


def overlay(settings: dict) -> dict:
    """Puts file values over a dict read from app_settings."""
    settings.update(load())
    return settings


def save(mapping: dict[str, str]) -> None:
    if not mapping:
        return
    with editing_file() as data:
        data.setdefault("settings", {}).update({key: str(value) for key, value in mapping.items()})
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
