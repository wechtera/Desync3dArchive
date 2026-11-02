"""The single app password, browser sessions and login rate limiting.

There is one user, so there is no users table. The password is a salted scrypt hash in a 0600 file
outside the repository and data directory. Sessions live in memory, so a restart signs everyone
out. Sessions and failed attempts both reset with the process, which suits a single-user app.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path

from desync_archival import settings as app_config
from desync_archival.services import secret_store
from desync_archival.services.logger import logger

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 8

COOKIE_NAME = "desync_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60

_sessions: dict[str, float] = {}
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


class WeakPasswordError(ValueError):
    pass


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM
    )


def hash_password(password: str) -> dict[str, str]:
    """Hashes with a fresh salt."""
    salt = secrets.token_bytes(16)
    return {
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_derive(password, salt)).decode(),
    }


def set_password(password: str) -> Path:
    """Saves the password hash to the 0600 secrets file, keeping anything else in it."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")

    hashed = hash_password(password)
    with secret_store.editing_file() as data:
        data["login"] = hashed
    clear_sessions()
    logger.warning("App password changed. All sessions were signed out.")
    return app_config.secrets_file()


def has_password() -> bool:
    return bool(secret_store.read_file().get("login"))


def verify_password(password: str) -> bool:
    """Checks a password in constant time."""
    stored = secret_store.read_file().get("login")
    if not stored:
        return False
    digest = _derive(password, base64.b64decode(stored["salt"]))
    # Safety: constant-time, so timing can't reveal how much of the hash matched.
    return hmac.compare_digest(digest, base64.b64decode(stored["hash"]))


def _key(token: str) -> str:
    # Only a hash is held, so a memory dump of this table can't be replayed as a cookie.
    return hashlib.sha256(token.encode()).hexdigest()


def create_session() -> str:
    """Creates a session and returns the cookie value. Only its hash is kept."""
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[_key(token)] = time.time() + SESSION_SECONDS
    return token


def session_valid(token: str | None) -> bool:
    """Checks a cookie value, dropping it once expired."""
    if not token:
        return False
    with _lock:
        expires = _sessions.get(_key(token))
        if expires is None:
            return False
        if time.time() >= expires:
            del _sessions[_key(token)]
            return False
    return True


def end_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(_key(token), None)


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()


def _recent_failures(client: str) -> list[float]:
    cutoff = time.time() - LOCKOUT_SECONDS
    recent = [moment for moment in _failures.get(client, []) if moment > cutoff]
    _failures[client] = recent
    return recent


def is_locked_out(client: str) -> bool:
    with _lock:
        return len(_recent_failures(client)) >= MAX_FAILURES


def begin_attempt(client: str) -> bool:
    """Counts a try before the slow hash, so parallel requests can't all pass one lockout check.

    Returns False if the client is locked out. A try that turns out not to be a wrong password
    goes back through forget_attempt, and a right one clears the count.
    """
    with _lock:
        recent = _recent_failures(client)
        if len(recent) >= MAX_FAILURES:
            return False
        recent.append(time.time())
    return True


def forget_attempt(client: str) -> None:
    with _lock:
        recent = _failures.get(client)
        if recent:
            recent.pop()


def clear_failures(client: str | None = None) -> None:
    with _lock:
        if client is None:
            _failures.clear()
        else:
            _failures.pop(client, None)
