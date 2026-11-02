"""Remembers what a mount last said, so a view never waits on a stalled NAS.

A stalled SMB mount can block one stat for minutes, and each one holds a request thread until the
threadpool runs out. So reads run on their own daemon thread, at most one per key, and a view gets
the last answer. An offline mount limits what works here, and never changes what's stored.
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FRESH_SECONDS = 30.0
# A healthy mount answers well inside this, so only a stalled one ever gets the fallback.
FIRST_ANSWER_SECONDS = 0.5

_known: dict[str, tuple[float, Any]] = {}
_reading: dict[str, threading.Event] = {}
_lock = threading.Lock()


def _read(key: str, reader: Callable[[], Any], fallback: Any, done: threading.Event) -> None:
    value = fallback
    try:
        value = reader()
    except (OSError, ValueError):
        pass
    finally:
        with _lock:
            _known[key] = (time.monotonic(), value)
            _reading.pop(key, None)
        done.set()


def remembered(key: str, reader: Callable[[], Any], fallback: Any) -> Any:
    """Answers from memory, refreshing in the background once the answer is old."""
    with _lock:
        known = _known.get(key)
        fresh = known is not None and time.monotonic() - known[0] < FRESH_SECONDS
        done = _reading.get(key)
        start = not fresh and done is None
        if start:
            done = _reading[key] = threading.Event()
    if start:
        threading.Thread(target=_read, args=(key, reader, fallback, done), name="mount-read", daemon=True).start()
    if known is not None:
        return known[1]
    if done:
        done.wait(FIRST_ANSWER_SECONDS)
    with _lock:
        known = _known.get(key)
    return known[1] if known else fallback


def is_online(folder: Path | str) -> bool:
    return remembered(f"online:{folder}", Path(folder).is_dir, False)


def names_in(folder: Path | str) -> set[str]:
    return remembered(f"names:{folder}", lambda: {path.name for path in Path(folder).iterdir()}, set())


def forget(folder: Path | str) -> None:
    """Drops what's known about a folder, for code that has just changed it."""
    with _lock:
        _known.pop(f"online:{folder}", None)
        _known.pop(f"names:{folder}", None)
