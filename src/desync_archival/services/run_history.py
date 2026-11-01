"""The last run of each Vault Reconcile tool, read from pipeline_tasks."""

import sqlite3
from typing import Any

from desync_archival.db.queries import run_history as queries


def latest(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return {row["card"]: {k: row[k] for k in dict(row) if k != "card"} for row in conn.execute(queries.SELECT_LATEST_RUNS)}
