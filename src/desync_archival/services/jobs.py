"""Job states, restart recovery and time limits for background work.

pipeline_tasks and download_jobs are the job records. A process can die at any moment, so startup
turns anything still marked as in progress into a state the app can act on.
"""

import sqlite3
from typing import Any

from desync_archival.db.queries import jobs as queries
from desync_archival.services.logger import logger

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
FINISHED_STATES = (COMPLETED, FAILED, CANCELLED, INTERRUPTED)

HOUR = 3600
DAY = 24 * HOUR
BACKUP_INTERVAL = 30 * DAY
BACKUP_RETRY_INTERVAL = 6 * HOUR
BACKUP_CHECK_SECONDS = HOUR
SCHEDULE_CHECK_SECONDS = 300
FIRST_BACKUP_CHECK_SECONDS = 60
DEFAULT_TIME_LIMIT = 24 * HOUR
TIME_LIMITS = {"library_scrape": 2 * HOUR, "archive_peek_backfill": 12 * HOUR}


def time_limit(task_type: str) -> int:
    return TIME_LIMITS.get(task_type, DEFAULT_TIME_LIMIT)


def recover_interrupted(conn: sqlite3.Connection) -> dict[str, Any]:
    """Runs at startup, before any worker exists, so nothing in progress can be live."""
    with conn:
        tasks = conn.execute(
            queries.UPDATE_PIPELINE_TASKS_STATUS_BY_STATUS,
            (INTERRUPTED, RUNNING),
        ).rowcount
        downloads = conn.execute(
            queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS
        ).rowcount
        offloads = conn.execute(
            queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS_V2
        ).rowcount

    result = {"tasks_interrupted": tasks, "downloads_requeued": downloads, "offloads_requeued": offloads}
    if tasks or downloads or offloads:
        logger.warning(f"Recovered after restart: {result}")
    return result


def busy_reason(conn, download_manager=None) -> str | None:
    """Why a destructive action must wait, or None. Job starts refuse each other on their own."""
    running = conn.execute(
        queries.SELECT_PIPELINE_TASKS_BY_STATUS
    ).fetchone()
    if running:
        return f"Task #{running[0]} ({running[1]}) is running. Wait for it, or cancel it, then try again."
    active = getattr(download_manager, "active_downloads", None)
    if isinstance(active, dict) and active:
        return "Downloads are running. Pause them and wait for them to finish, then try again."
    offloads = getattr(download_manager, "active_offloads", None)
    if isinstance(offloads, dict) and offloads:
        return "Files are moving to the vault. Wait for that to finish, then try again."
    # The lists above miss a job waiting out an offload retry, whose staged files are the only copy.
    live = conn.execute(queries.COUNT_LIVE_DOWNLOAD_JOBS).fetchone()[0]
    if live:
        return f"{live} downloads are in progress or waiting to reach the vault. Let them finish, then try again."
    return None
