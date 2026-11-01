"""Scheduled library sync and integrity checks. Both are off until turned on in Settings.

A sync runs over several jobs, so its stage is stored and each tick moves it along. That way a
restart between the scrape and the ingest carries on instead of losing the scrape.
"""

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from desync_archival.db.queries import scheduler as queries
from desync_archival.services import audit, auth_service, jobs
from desync_archival.services.downloader import StorageUnavailableError, queue_models
from desync_archival.services.logger import log_layer

SCRAPE = "scrape"
INGEST = "ingest"
INTEGRITY_MODES = ("fast", "deep", "reverse")
DEFAULTS = {
    "sync_enabled": "0",
    "sync_interval_days": "7",
    "sync_auto_download": "0",
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
        "sync_enabled": "1" if data.get("sync_enabled") else "0",
        "sync_interval_days": _days(data.get("sync_interval_days"), "The sync interval"),
        "sync_auto_download": "1" if data.get("sync_auto_download") else "0",
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


def _finish_sync(conn: sqlite3.Connection, result: str) -> None:
    with conn:
        auth_service.set_settings(conn, {
            "sync_stage": "", "sync_task_id": "", "sync_new_keys": "",
            "last_sync_epoch": str(time.time()), "last_sync_result": result,
        })
    log_layer("sql", logging.INFO, f"Scheduled sync finished: {result}")


def _note_sync(conn: sqlite3.Connection, result: str) -> None:
    # It stays due, so the sync runs once the problem clears. The note is what Health shows meanwhile.
    with conn:
        auth_service.set_settings(conn, {"last_sync_result": result})


def _stored_ids(conn: sqlite3.Connection, folder: str, root: Path) -> list[int]:
    target = root / "scrapes" / folder / "model_ids.txt"
    lines = target.read_text(encoding="utf-8").splitlines()
    return [int(digits) for line in lines if (digits := re.sub(r"\D", "", line))]


def _task(conn: sqlite3.Connection, value: str) -> tuple[str, dict[str, Any]] | None:
    row = conn.execute(queries.SELECT_TASK, (value or "0",)).fetchone()
    if not row:
        return None
    try:
        return row[0], json.loads(row[1] or "{}")
    except ValueError:
        return row[0], {}


def _start_sync(conn: sqlite3.Connection, tasks: Any) -> None:
    if not auth_service.get_auth_status(conn)["connected"]:
        _note_sync(conn, "Waiting: the MMF sign-in has expired")
        return
    if not auth_service.get_setting(conn, "cookies_json", "").strip("[] \n"):
        _note_sync(conn, "Waiting: no MMF session cookies are saved")
        return
    started = tasks.start_task("library_scrape")
    if started.get("success"):
        with conn:
            auth_service.set_settings(conn, {"sync_stage": SCRAPE, "sync_task_id": str(started["task_id"])})


def _after_scrape(conn: sqlite3.Connection, tasks: Any, status: str, summary: dict[str, Any]) -> None:
    if status != jobs.COMPLETED:
        _finish_sync(conn, f"Failed: the scrape ended as {status}")
        return
    ids = _stored_ids(conn, str(summary.get("folder") or ""), Path(tasks.root_dir))
    known = {row[0] for row in conn.execute(queries.SELECT_EXTERNAL_KEYS)}
    fresh = [str(i) for i in ids if str(i) not in known]
    if not fresh:
        _finish_sync(conn, "Completed: nothing new")
        return
    result, code = tasks.start_metadata_ingest(ids, "Scheduled sync")
    if code < 400 and result.get("task_id"):
        with conn:
            auth_service.set_settings(conn, {
                "sync_stage": INGEST, "sync_task_id": str(result["task_id"]), "sync_new_keys": json.dumps(fresh),
            })


def _after_ingest(conn: sqlite3.Connection, downloads: Any, status: str) -> None:
    if status != jobs.COMPLETED:
        _finish_sync(conn, f"Failed: the ingest ended as {status}")
        return
    keys = json.loads(auth_service.get_setting(conn, "sync_new_keys", "[]") or "[]")
    marks = ",".join("?" * len(keys))
    ids = [r[0] for r in conn.execute(f"SELECT id FROM collections WHERE external_key IN ({marks})", keys)]
    queued = 0
    if settings(conn)["sync_auto_download"] == "1":
        queued = queue_models(conn, downloads, ids)
    _finish_sync(conn, f"Completed: {len(ids)} new, {queued} queued for download")


def _sync(conn: sqlite3.Connection, tasks: Any, downloads: Any) -> None:
    stage = auth_service.get_setting(conn, "sync_stage", "")
    if not stage:
        if settings(conn)["sync_enabled"] == "1" and _due(conn, "sync"):
            _start_sync(conn, tasks)
        return
    found = _task(conn, auth_service.get_setting(conn, "sync_task_id", ""))
    if found is None:
        _finish_sync(conn, "Failed: its task record is gone")
        return
    status, summary = found
    if status not in jobs.FINISHED_STATES:
        return
    if stage == SCRAPE:
        _after_scrape(conn, tasks, status, summary)
    else:
        _after_ingest(conn, downloads, status)


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
    _sync(conn, tasks, downloads)
    _integrity(conn, tasks, downloads)
