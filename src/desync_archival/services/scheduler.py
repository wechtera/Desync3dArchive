"""Scheduled integrity checks, off until turned on in Settings.

The scheduled library sync lives on `feature/scheduled-sync`. It ran the library retrieval with no
one present, which only fits if an API ever lists a user's library.
"""

import logging
import sqlite3
import time
from typing import Any

from desync_archival.services import audit, auth_service, jobs
from desync_archival.services.downloader import StorageUnavailableError
from desync_archival.services.logger import log_layer

INTEGRITY_MODES = ("fast", "deep", "reverse")
DEFAULTS = {
    "integrity_enabled": "0",
    "integrity_interval_days": "30",
    "integrity_mode": "fast",
}
MIN_DAYS, MAX_DAYS = 1, 365


class ScheduleRefused(ValueError):
    """The message is shown to the user."""


def settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {key: auth_service.get_setting(conn, key, value) or value for key, value in DEFAULTS.items()}


def _days(value: Any, label: str) -> str:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 0
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise ScheduleRefused(f"{label} must be between {MIN_DAYS} and {MAX_DAYS} days.")
    return str(days)


def save(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, str]:
    mode = str(data.get("integrity_mode") or DEFAULTS["integrity_mode"])
    if mode not in INTEGRITY_MODES:
        raise ScheduleRefused("Choose a fast, deep or reverse check.")
    chosen = {
        "integrity_enabled": "1" if data.get("integrity_enabled") else "0",
        "integrity_interval_days": _days(data.get("integrity_interval_days"), "The check interval"),
        "integrity_mode": mode,
    }
    with conn:
        auth_service.set_settings(conn, chosen)
    audit.record(conn, "schedules_changed", False, chosen)
    log_layer("sql", logging.INFO, f"Schedules changed: {chosen}")
    return chosen


def _due(conn: sqlite3.Connection, name: str) -> bool:
    last = float(auth_service.get_setting(conn, f"last_{name}_epoch", "0") or 0)
    return time.time() - last >= int(settings(conn)[f"{name}_interval_days"]) * jobs.DAY


def _integrity(conn: sqlite3.Connection, tasks: Any, downloads: Any) -> None:
    chosen = settings(conn)
    if chosen["integrity_enabled"] != "1" or not _due(conn, "integrity"):
        return
    # A file mid-download reads as damage, and an offline NAS limits the check without making it due less.
    if jobs.busy_reason(conn, downloads):
        return
    try:
        downloads.storage.get_root_path()
    except StorageUnavailableError:
        with conn:
            auth_service.set_settings(conn, {"last_integrity_result": "Waiting: the vault is offline"})
        return
    started = tasks.start_task("vault_reconcile", {"scan_mode": chosen["integrity_mode"]})
    if started.get("success"):
        with conn:
            auth_service.set_settings(conn, {
                "last_integrity_epoch": str(time.time()), "last_integrity_task_id": str(started["task_id"]),
                "last_integrity_result": f"Started a {chosen['integrity_mode']} check",
            })


def tick(conn: sqlite3.Connection, tasks: Any, downloads: Any) -> None:
    """Starts whatever is due. Called every five minutes."""
    _integrity(conn, tasks, downloads)
