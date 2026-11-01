"""
ui/routes/reconcile.py - Endpoints for background vault storage auditing and state reconciliation.
"""

import sqlite3
from pathlib import Path
from typing import Any, Optional

from desync_archival.db.queries import reconcile as queries
from desync_archival.services import (
    audit,
    duplicates,
    jobs,
    pending_deletion,
    run_history,
)
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.task_manager import TaskManager


def _get_managers(conn: sqlite3.Connection) -> tuple[VaultReconciler, TaskManager]:
    db_path = Path(conn.execute(queries.PRAGMA_ROW).fetchone()[2])
    return VaultReconciler(db_path), TaskManager(db_path)


def _record_check(task_mgr: TaskManager, task_type: str, res: dict[str, Any], label: str) -> None:
    ok = res.get("success")
    task_mgr.record_finished(task_type, label if ok else str(res.get("error") or "Failed"), {}, jobs.COMPLETED if ok else jobs.FAILED)


def handle_post_reconcile_scan(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Starts background vault reconciliation task via TaskManager and returns immediately."""
    scan_mode = body.get("scan_mode", "fast")
    _, task_mgr = _get_managers(conn)

    # A file mid-download looks like damage to a reconcile.
    if reason := jobs.busy_reason(conn, download_manager):
        return {"success": False, "error": reason}, 409

    res = task_mgr.start_task("vault_reconcile", {"scan_mode": scan_mode})
    if not res.get("success"):
        return res, 400

    return {
        "success": True,
        "task_id": res["task_id"],
        "task_type": "vault_reconcile",
        "scan_mode": scan_mode,
        "message": f"Vault reconciliation ({scan_mode.upper()}) dispatched to background pipeline worker.",
    }, 200


def handle_get_reconcile_status(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Live task progress if running, or the latest completed audit report."""
    reconciler, task_mgr = _get_managers(conn)
    active_task = task_mgr.get_active_task()

    if active_task and active_task.get("task_type") == "vault_reconcile":
        scan_mode = (active_task.get("params") or {}).get("scan_mode", "fast")
        return {
            "status": "running",
            "task_id": active_task["id"],
            "scan_mode": scan_mode,
            "progress_pct": active_task.get("progress_pct", 0.0),
            "current_item": active_task.get("progress_label", "Auditing storage..."),
            "log_output": active_task.get("log_output", ""),
        }, 200

    status_doc = reconciler.get_status()
    return status_doc, 200


def handle_post_reconcile_cancel(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    _, task_mgr = _get_managers(conn)
    active_task = task_mgr.get_active_task()
    if active_task and active_task.get("task_type") == "vault_reconcile":
        task_mgr.cancel_task(active_task["id"])
    return {"success": True}, 200


def handle_post_reconcile_reset(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    if not model_ids:
        return {"error": "Missing model_ids list in request body"}, 400
    reconciler, _ = _get_managers(conn)
    dry_run = not body.get("confirm")
    if not dry_run and (reason := jobs.busy_reason(conn, download_manager)):
        return {"success": False, "error": reason}, 409
    res = reconciler.reset_reconciliation_models(model_ids, dry_run=dry_run)
    audit.record(conn, "reconcile_reset", dry_run, {**res, "model_ids": model_ids})
    return res, 200 if res.get("success") else 400


def handle_post_reconcile_delete_orphans(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    paths = body.get("paths", [])
    if not paths:
        return {"error": "Missing paths list in request body"}, 400
    reconciler, _ = _get_managers(conn)
    dry_run = not body.get("confirm")
    # A folder a running job is writing looks like an orphan until its row lands.
    if not dry_run and (reason := jobs.busy_reason(conn, download_manager)):
        return {"success": False, "error": reason}, 409
    res = reconciler.delete_orphan_directories(paths, dry_run=dry_run)
    audit.record(conn, "delete_orphans", dry_run, {**res, "paths": paths})
    return res, 200 if res.get("success") else 400


def handle_post_reconcile_ingest_untracked(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    items = body.get("items", [])
    if not items:
        return {"error": "Missing items list in request body"}, 400
    reconciler, _ = _get_managers(conn)
    res = reconciler.ingest_untracked_files(items)
    return res, 200 if res.get("success") else 400


def handle_get_reconcile_staging_garbage(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    reconciler, task_mgr = _get_managers(conn)
    res = reconciler.scan_staging_garbage()
    _record_check(task_mgr, "staging_scan", res, f"{len(res.get('items') or [])} leftovers found")
    return res, 200 if res.get("success") else 500


def handle_post_reconcile_staging_purge(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    paths = body.get("paths")
    reconciler, _ = _get_managers(conn)
    dry_run = not body.get("confirm")
    # Staging holds whatever a running download is writing right now.
    if not dry_run and (reason := jobs.busy_reason(conn, download_manager)):
        return {"success": False, "error": reason}, 409
    res = reconciler.purge_staging_garbage(target_paths=paths, dry_run=dry_run)
    audit.record(conn, "staging_purge", dry_run, res)
    return res, 200 if res.get("success") else 400


def handle_get_reconcile_path_drift(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    reconciler, task_mgr = _get_managers(conn)
    res = reconciler.scan_path_drift()
    _record_check(task_mgr, "path_drift_scan", res, f"{len(res.get('items') or [])} folders drifted")
    return res, 200 if res.get("success") else 500


def handle_get_reconcile_duplicates(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    include_shared = params.get("include_shared", ["0"])[0] == "1"
    found = duplicates.find_duplicates(conn, include_shared=include_shared)
    _, task_mgr = _get_managers(conn)
    task_mgr.record_finished("duplicate_scan", f"{found['summary']['pairs']} pairs to review", found)
    pending_deletion.mark_blocked(conn, found["pairs"], jobs.busy_reason(conn, download_manager))
    return found, 200


def handle_get_reconcile_duplicates_latest(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    saved = duplicates.latest(conn)
    pending_deletion.mark_blocked(conn, saved["pairs"], jobs.busy_reason(conn, download_manager))
    return saved, 200


def handle_post_reconcile_duplicate_decision(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    try:
        return duplicates.decide(conn, int(body["first_file_id"]), int(body["second_file_id"]), str(body["decision"])), 200
    except duplicates.DecisionRefused as e:
        return {"success": False, "error": str(e)}, 400


def handle_post_reconcile_duplicate_delete(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    file_id = int(body["file_id"])
    if not body.get("confirm"):
        return {"success": True, "dry_run": True, "file_id": file_id}, 200
    # A move while a download or job is working on the same file would leave both half done.
    if reason := jobs.busy_reason(conn, download_manager):
        return {"success": False, "error": reason}, 409
    try:
        return pending_deletion.delete_file(conn, file_id), 200
    except pending_deletion.DeleteRefused as e:
        return {"success": False, "error": str(e)}, e.code


def handle_get_pending_deletion(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    return pending_deletion.summary(conn), 200


def handle_get_pending_deletion_items(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    return pending_deletion.listing(conn), 200


def handle_post_pending_deletion_restore(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    if reason := jobs.busy_reason(conn, download_manager):
        return {"success": False, "error": reason}, 409
    try:
        return pending_deletion.restore_file(conn, int(body["id"])), 200
    except pending_deletion.DeleteRefused as e:
        return {"success": False, "error": str(e)}, e.code


def handle_post_pending_deletion_purge(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    try:
        return pending_deletion.purge(conn, bool(body.get("confirm")), [int(i) for i in body.get("ids") or []]), 200
    except pending_deletion.DeleteRefused as e:
        return {"success": False, "error": str(e)}, e.code


def handle_post_reconcile_path_realign(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids")
    reconciler, _ = _get_managers(conn)
    res = reconciler.realign_model_paths(model_ids=model_ids)
    return res, 200 if res.get("success") else 400

def handle_post_reconcile_catalog_previews(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    # This used to be a bare thread, which never showed as a job and could overlap one.
    _, task_mgr = _get_managers(conn)
    res = task_mgr.start_task("catalog_preview_backfill", {"model_ids": body.get("model_ids")})
    if not res.get("success"):
        return res, 409
    return {
        "success": True,
        "task_id": res["task_id"],
        "message": "Catalog preview cache sweep dispatched in background.",
    }, 200


def handle_get_reconcile_history(
    conn: sqlite3.Connection,
    download_manager: DownloadManager | None,
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    return {"runs": run_history.latest(conn)}, 200
