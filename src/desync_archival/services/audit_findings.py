"""Vault Audit findings a person dismissed, and which version of the app saved a scan.

Dismissals are a handful of rows, kept in one app_settings entry so they need no schema change. One
matches a finding by model, defect and file, and holds only while the finding reads the same: a
finding whose detail changes, such as a file truncated by a different amount, shows again.
"""

import json
import sqlite3
from datetime import UTC, datetime
from importlib import metadata
from typing import Any

from desync_archival.db.queries import audit_findings as queries
from desync_archival.services import audit

KEY = "vault_audit_dismissed"


class FindingRefused(ValueError):
    """Nothing changed. The message is shown to the user."""


def app_version() -> str:
    try:
        return metadata.version("desync-archival")
    except metadata.PackageNotFoundError:
        return "unknown"


def _key(model_id: Any, defect: str, filename: str | None) -> tuple[int, str, str]:
    return int(model_id), str(defect), filename or ""


def dismissed(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    row = conn.execute(queries.SELECT_SETTING, (KEY,)).fetchone()
    return json.loads(row[0]) if row and row[0] else []


def _save(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    with conn:
        conn.execute(queries.UPSERT_SETTING, (KEY, json.dumps(items)))


def dismiss(conn: sqlite3.Connection, model_id: int, defect: str, filename: str, detail: str, model_name: str = "") -> dict[str, Any]:
    if not defect:
        raise FindingRefused("Say which finding to dismiss.")
    key = _key(model_id, defect, filename)
    items = [d for d in dismissed(conn) if _key(d["model_id"], d["defect"], d.get("filename")) != key]
    items.append({
        "model_id": int(model_id), "defect": defect, "filename": filename or "", "detail": detail,
        "model_name": model_name, "dismissed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    })
    _save(conn, items)
    result = {"success": True, "model_id": int(model_id), "defect": defect, "filename": filename or ""}
    audit.record(conn, "audit_finding_dismiss", False, result)
    return result


def restore(conn: sqlite3.Connection, model_id: int, defect: str, filename: str) -> dict[str, Any]:
    key = _key(model_id, defect, filename)
    items = dismissed(conn)
    kept = [d for d in items if _key(d["model_id"], d["defect"], d.get("filename")) != key]
    if len(kept) == len(items):
        raise FindingRefused("That finding isn't dismissed.")
    _save(conn, kept)
    result = {"success": True, "model_id": int(model_id), "defect": defect, "filename": filename or ""}
    audit.record(conn, "audit_finding_restore", False, result)
    return result


def view(conn: sqlite3.Connection, report: dict[str, Any]) -> dict[str, Any]:
    """The saved report as the screen shows it: dismissed findings set aside, and counts to match."""
    index = {_key(d["model_id"], d["defect"], d.get("filename")): d for d in dismissed(conn)}
    shown, hidden = [], []
    for model in report.get("discrepancies", []):
        open_issues = []
        for issue in model.get("issues", []):
            match = index.get(_key(model["model_id"], issue["defect"], issue.get("filename")))
            if match and match.get("detail") == issue.get("detail"):
                hidden.append({**issue, "model_id": model["model_id"], "model_name": model.get("model_name", ""),
                               "creator_name": model.get("creator_name", "")})
            else:
                open_issues.append(issue)
        if open_issues:
            shown.append({**model, "issues": open_issues})
    summary = dict(report.get("summary") or {})
    cleared = len(report.get("discrepancies", [])) - len(shown)
    if cleared and "discrepancies" in summary:
        summary["discrepancies"] = max(0, summary["discrepancies"] - cleared)
    return {**report, "summary": summary, "discrepancies": shown, "dismissed": hidden}
