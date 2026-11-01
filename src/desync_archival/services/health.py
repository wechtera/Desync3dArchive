"""One report on whether the app is well, for Settings > Health.

Built from the database and the cached disk reading, so opening it never walks the NAS.
"""

import sqlite3
import time
from typing import Any

from desync_archival import migrations
from desync_archival.db.queries import health as queries
from desync_archival.services import auth_service, jobs, scheduler

OK = "ok"
WARNING = "warning"
PROBLEM = "problem"
ORDER = (OK, WARNING, PROBLEM)
TOKEN_WARNING = 2 * jobs.DAY
LOW_SPACE = 0.05
BACKUP_LATE = 35 * jobs.DAY


def _check(key: str, label: str, state: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "state": state, "detail": detail}


def _ago(epoch: float) -> str:
    days = (time.time() - epoch) / jobs.DAY
    return "today" if days < 1 else f"{int(days)} days ago"


def _database(conn: sqlite3.Connection) -> dict[str, str]:
    have, want = migrations.current_version(conn), migrations.latest_version()
    if have != want:
        return _check("database", "Database", PROBLEM, f"Schema {have}, this code expects {want}.")
    return _check("database", "Database", OK, f"Schema {have}.")


def _vault(metrics: dict[str, Any]) -> dict[str, str]:
    nas = metrics["nas"]
    if not nas.get("mounted"):
        return _check("vault", "Vault", WARNING, "Offline. Browsing still works. Downloads and exports wait.")
    free = nas["free_bytes"] / max(nas["total_bytes"], 1)
    if free < LOW_SPACE:
        return _check("vault", "Vault", WARNING, f"Only {free:.0%} free.")
    return _check("vault", "Vault", OK, f"Mounted, {free:.0%} free.")


def _sign_in(conn: sqlite3.Connection) -> dict[str, str]:
    # MMF gives no refresh tokens, so an expired sign-in is only fixed by signing in again.
    status = auth_service.get_auth_status(conn)
    if not status["connected"]:
        return _check("sign_in", "MMF sign-in", WARNING, "Expired or not signed in. Sign in under MMF Sign-In.")
    left = status["expires_at"] - time.time()
    if left < TOKEN_WARNING:
        return _check("sign_in", "MMF sign-in", WARNING, f"Expires in {max(int(left / 3600), 1)} hours.")
    return _check("sign_in", "MMF sign-in", OK, f"Valid for {int(left / jobs.DAY)} more days.")


def _cookies(conn: sqlite3.Connection) -> dict[str, str]:
    if not auth_service.get_setting(conn, "cookies_json", "").strip("[] \n"):
        return _check("cookies", "Session cookies", WARNING, "None saved. Scrapes can't run.")
    expiry = auth_service.cookie_expiry(conn)
    if expiry is not None and expiry <= time.time():
        return _check("cookies", "Session cookies", WARNING, "Expired. Paste fresh ones under Session & Cookies before a scrape.")
    if expiry is not None and expiry - time.time() < jobs.DAY:
        return _check("cookies", "Session cookies", WARNING, f"Expire in {max(int((expiry - time.time()) / 3600), 1)} hours.")
    saved = float(auth_service.get_setting(conn, "cookies_saved_at", "0") or 0)
    return _check("cookies", "Session cookies", OK, f"Saved, refreshed {_ago(saved)}." if saved else "Saved.")


def _backup(conn: sqlite3.Connection) -> dict[str, str]:
    last = float(auth_service.get_setting(conn, "last_backup_epoch", "0") or 0)
    attempted = float(auth_service.get_setting(conn, "last_backup_attempt_epoch", "0") or 0)
    result = auth_service.get_setting(conn, "last_backup_result", "")
    if result.startswith("Failed"):
        return _check("backup", "Backups", PROBLEM, f"The last one failed, {_ago(attempted or last)}.")
    if not last:
        return _check("backup", "Backups", WARNING, "None yet.")
    if time.time() - last > BACKUP_LATE:
        return _check("backup", "Backups", WARNING, f"The last one was {_ago(last)}.")
    return _check("backup", "Backups", OK, f"Last one {_ago(last)}.")


def _workers(downloads: Any) -> dict[str, str]:
    counts = downloads.worker_counts() if downloads else {"running": 0, "expected": 0}
    if not counts["expected"]:
        return _check("workers", "Download workers", OK, "Not started.")
    if counts["running"] < counts["expected"]:
        return _check(
            "workers", "Download workers", PROBLEM,
            f"{counts['running']} of {counts['expected']} are running. Restart the app and check the log.",
        )
    return _check("workers", "Download workers", OK, f"All {counts['expected']} are running.")


def _integrity(conn: sqlite3.Connection) -> dict[str, str]:
    if scheduler.settings(conn)["integrity_enabled"] != "1":
        return _check("integrity", "Scheduled integrity check", OK, "Off.")
    task = conn.execute(queries.SELECT_TASK_STATUS, (auth_service.get_setting(conn, "last_integrity_task_id", "0") or "0",)).fetchone()
    if not task:
        result = auth_service.get_setting(conn, "last_integrity_result", "") or "Not run yet."
        return _check("integrity", "Scheduled integrity check", WARNING if result.startswith("Waiting") else OK, result)
    label = task[1] or task[0]
    return _check("integrity", "Scheduled integrity check", PROBLEM if task[0] == jobs.FAILED else OK, label)


def _tasks(conn: sqlite3.Connection) -> dict[str, str]:
    stuck = conn.execute(queries.SELECT_LONG_RUNNING, (f"-{jobs.DEFAULT_TIME_LIMIT} seconds",)).fetchall()
    if stuck:
        names = ", ".join(f"#{row[0]} {row[1]}" for row in stuck)
        return _check("tasks", "Jobs", PROBLEM, f"Running for over a day: {names}.")
    return _check("tasks", "Jobs", OK, "Nothing stuck.")


def report(conn: sqlite3.Connection, downloads: Any) -> dict[str, Any]:
    metrics = downloads.storage.get_disk_metrics() if downloads else {"nas": {}}
    checks = [
        _database(conn), _vault(metrics), _sign_in(conn), _cookies(conn), _backup(conn),
        _workers(downloads), _integrity(conn), _tasks(conn),
    ]
    columns = ("task_type", "runs", "failed", "average_seconds", "last_run")
    return {
        "status": max((c["state"] for c in checks), key=ORDER.index),
        "checks": checks,
        "jobs": [dict(zip(columns, tuple(row), strict=True)) for row in conn.execute(queries.SELECT_TASK_METRICS)],
        "schedules": scheduler.settings(conn),
    }
