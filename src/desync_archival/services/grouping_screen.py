"""What the LLM Grouping screen shows, and the steps it takes around the E54 services.

An answer that can't be read, or that answers no export of ours, isn't kept: the person gets what's
wrong and a fix request to paste back to the LLM. An answer with anything usable is kept as a
checked import, so confirming applies exactly what the dry run showed.
"""

import json
import sqlite3
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import grouping as queries
from desync_archival.services import (
    grouping_answer,
    grouping_export,
    grouping_suggestions,
    picture_jobs,
)
from desync_archival.services.grouping_answer import APPLY, REFUSE, SUGGEST
from desync_archival.services.grouping_apply import ApplyRefused

RECENT_IMPORTS = 15
RECENT_EXPORTS = 5
# Room for the JSON wrapper around the answer inside the request's body limit.
MAX_ANSWER_CHARS = app_config.MAX_BODY_BYTES - 64 * 1024


def _counts(report: dict[str, Any]) -> dict[str, int]:
    counts = {APPLY: 0, SUGGEST: 0, REFUSE: 0}
    for unit in report.get("units", []):
        counts[unit["outcome"]] = counts.get(unit["outcome"], 0) + 1
    return counts


def _waiting_tags(conn: sqlite3.Connection, import_id: int, plans: dict[str, Any]) -> list[str]:
    """New tags an applied import suggested that nobody has approved yet."""
    applied = {s["unit_key"] for s in conn.execute(queries.SELECT_SNAPSHOTS, (import_id,)) if not s["restored_at"]}
    existing = {r[0] for r in conn.execute(queries.SELECT_TAGS)}
    wanted = {t for key, plan in plans.items() if key in applied for t in plan.get("suggested_tags", [])}
    return sorted(wanted - existing)


def imports(conn: sqlite3.Connection, limit: int = RECENT_IMPORTS) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    items = []
    for row in conn.execute(queries.SELECT_IMPORTS, (limit,)).fetchall():
        report = json.loads(row["report_json"])
        item = {
            "id": row["id"], "name": row["source_name"], "status": row["status"], "export_id": row["export_key"] or "",
            "created_at": row["created_at"], "applied_at": row["applied_at"], "undone_at": row["undone_at"],
            "counts": _counts(report), "units": len(report.get("units", [])), "tags": [],
        }
        if row["status"] == "applied":
            plans = json.loads(conn.execute(queries.SELECT_IMPORT, (row["id"],)).fetchone()["answer_json"])
            item["tags"] = _waiting_tags(conn, row["id"], plans)
        items.append(item)
    return items


def exports(conn: sqlite3.Connection, limit: int = RECENT_EXPORTS) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(queries.SELECT_EXPORTS, (limit,)).fetchall()]


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "waiting": grouping_suggestions.waiting(conn),
        "imports": imports(conn),
        "exports": exports(conn),
        "pictures": picture_jobs.status(conn),
        "batch_lines": {
            "default": grouping_export.DEFAULT_BATCH_LINES, "min": grouping_export.MIN_BATCH_LINES,
            "max": grouping_export.MAX_BATCH_LINES,
        },
        "max_answer_chars": MAX_ANSWER_CHARS,
    }


def check_upload(conn: sqlite3.Connection, text: str, name: str) -> dict[str, Any]:
    """The dry run. Keeps the answer as a checked import when any unit can apply or wait for review."""
    if len(text) > MAX_ANSWER_CHARS:
        error = "That answer is too long to check at once. Upload one batch's answer at a time."
        return {"export_id": "", "notes": [], "error": error, "units": [], "import_id": None,
                "counts": _counts({}), "fix_request": ""}
    report = grouping_answer.check(conn, text)
    report["counts"] = _counts(report)
    if report["counts"][APPLY] or report["counts"][SUGGEST]:
        grouping_answer.record(conn, report, name or "pasted answer")
    else:
        report.pop("plans", None)
        report["import_id"] = None
    return report


def discard(conn: sqlite3.Connection, import_id: int) -> dict[str, Any]:
    """Drops a checked import nobody confirmed. Nothing was applied, so nothing changes.

    Raises:
        ApplyRefused: The import doesn't exist, or was already applied, undone or discarded.
    """
    with conn:
        changed = conn.execute(queries.DISCARD_IMPORT, (import_id,)).rowcount
    if not changed:
        raise ApplyRefused("Only an import waiting for confirmation can be discarded.")
    return {"success": True, "import_id": import_id}
