"""Audit trail for destructive actions."""

import json
import sqlite3
from typing import Any

from desync_archival.db.queries import audit as queries
from desync_archival.services.logger import current_request_id, logger


def record(conn: sqlite3.Connection, action: str, dry_run: bool, result: dict[str, Any]) -> None:
    """Records one destructive call. Paths in detail are operational data, not secrets."""
    outcome = "ok" if result.get("success", True) else "failed"
    detail = json.dumps(result, default=str)[:4000]
    with conn:
        conn.execute(
            queries.INSERT_AUDIT_LOG,
            (action, int(dry_run), outcome, detail, current_request_id()),
        )
    mode = "dry run" if dry_run else "executed"
    logger.warning(f"Audit: {action} {mode}: {outcome}")
