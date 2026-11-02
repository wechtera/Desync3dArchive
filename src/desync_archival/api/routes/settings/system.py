"""Documents, health, schedules, disk usage, browsing, logging and purges."""

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.api.routes.settings.core import DEFAULT_STASH_EXPORT_PATH, format_bytes
from desync_archival.db.queries import settings as queries
from desync_archival.services import (
    audit,
    documents,
    file_import,
    health,
    locations,
    scheduler,
)
from desync_archival.services.external_indexer import (
    folder_totals,
)
from desync_archival.services.logger import (
    LOGS_DIR,
    clear_ui_logs,
    get_logging_config,
    log_layer,
    set_logging_config,
)

# -------------------------------------------------------------------------
# Directory Metrics & System Browse Endpoints
# -------------------------------------------------------------------------

def handle_get_documents(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """GET /api/documents. Pages PDFs from the catalog, so it works offline."""
    query = params or {}
    def one(name: str) -> str:
        return (query.get(name) or [""])[0].strip()
    page = max(int(one("page")) if one("page").isdigit() else 1, 1)
    return documents.listing(conn, one("q"), one("kind"), one("creator"), one("source"), one("state"), page), 200


def handle_post_document_stash(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        return {"success": True, **documents.add_to_stash(conn, str((body or {}).get("key") or ""))}, 200
    except documents.DocumentUnavailable as e:
        return {"success": False, "error": str(e)}, e.code


def handle_post_document_update(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    result = task_manager.start_task("document_update")
    return result, 200 if result.get("success") else 409


def handle_get_health(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    return health.report(conn, download_manager), 200


def handle_post_schedules(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        return {"success": True, "schedules": scheduler.save(conn, body or {})}, 200
    except scheduler.ScheduleRefused as e:
        return {"success": False, "error": str(e)}, 400


def handle_get_locations(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    return {"locations": locations.listing(conn)}, 200


def handle_get_directory_usage(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """Gathers storage usage metrics across scratch staging, vault NAS, slicer queue, previews, metadata, and logs."""
    settings = {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(queries.SELECT_APP_SETTINGS_V2).fetchall()
        settings = {r["key"]: r["value"] for r in rows} if rows else {}
    except Exception:
        pass

    staging_setting = settings.get("download_work_path", "")
    staging_path = Path(staging_setting).expanduser().resolve() if staging_setting else app_config.staging_dir().resolve()

    nas_setting = settings.get("download_root_path", "")
    nas_path = Path(nas_setting).expanduser().resolve() if nas_setting else None

    stash_setting = settings.get("stash_download_path", "")
    stash_path = Path(stash_setting).expanduser().resolve() if stash_setting else Path(DEFAULT_STASH_EXPORT_PATH).resolve()

    directories = [
        ("Fast Local Staging", "staging", staging_path),
        ("Slicer Print Queue Staging", "stash_export", stash_path),
        ("Cached Previews", "previews", app_config.previews_dir().resolve()),
        ("Raw JSON Metadata", "metadata", app_config.metadata_dir().resolve()),
        ("Scrape Data Artifacts", "scrapes", app_config.scrapes_dir().resolve()),
        ("Application System Logs", "logs", LOGS_DIR.resolve()),
        ("PDF Cache", "document_cache", (app_config.data_dir() / documents.CACHE_FOLDER).resolve()),
    ]

    results: list[dict[str, Any]] = []

    for label, key, dir_p in directories:
        cnt, sz = folder_totals(dir_p) if dir_p.exists() and dir_p.is_dir() else (0, 0)
        results.append({
            "name": label,
            "key": key,
            "path": str(dir_p),
            "file_count": cnt,
            "size_bytes": sz,
            "formatted_size": format_bytes(sz),
        })

    # NAS Target Vault insertion at index 1
    if nas_path and nas_path.exists() and nas_path.is_dir():
        # The catalog already knows this, and walking the vault took minutes on every visit.
        cnt, sz = conn.execute(queries.SUM_VAULT_FILES, (f"{nas_path}/%",)).fetchone()
        results.insert(1, {
            "name": "NAS Target Vault",
            "key": "nas",
            "path": str(nas_path),
            "file_count": cnt,
            "size_bytes": sz,
            "formatted_size": format_bytes(sz),
        })
    else:
        results.insert(1, {
            "name": "NAS Target Vault",
            "key": "nas",
            "path": str(nas_path) if nas_path else "Unconfigured",
            "file_count": 0,
            "size_bytes": 0,
            "formatted_size": "0 B",
            "error": "Unmounted or unconfigured",
        })

    # SQLite row count tracking
    audit_count = 0
    task_count = 0
    try:
        tables = {r[0] for r in conn.execute(queries.SELECT_SQLITE_MASTER_NAME_BY_TYPE).fetchall()}
        if "download_audit_log" in tables:
            audit_count = conn.execute(queries.COUNT_DOWNLOAD_AUDIT_LOG).fetchone()[0]
        if "pipeline_tasks" in tables:
            task_count = conn.execute(queries.COUNT_PIPELINE_TASKS).fetchone()[0]
    except Exception:
        pass

    results.append({
        "name": "SQLite Audit Records",
        "key": "db_audit",
        "path": f"download_audit_log ({audit_count:,} rows), pipeline_tasks ({task_count:,} rows)",
        "file_count": audit_count + task_count,
        "size_bytes": 0,
        "formatted_size": "0 B",
    })

    return {
        "success": True,
        "directories": results,
        "items": results,
    }, 200


def handle_get_system_browse(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    folder_val = ""
    if params:
        folder_val = (params.get("folder", [""])[0] or params.get("path", [""])[0] or "").strip()

    roots = app_config.allowed_roots()
    if folder_val:
        current = Path(folder_val).expanduser().resolve()
        if not app_config.is_allowed(current):
            return {"error": "That folder is outside the allowed roots."}, 403
    else:
        current = roots[0]

    if not current.exists() or not current.is_dir():
        current = roots[0]

    dirs, files = [], []
    want_files = bool(params and params.get("files"))
    try:
        # scandir carries each entry's type, so a listing doesn't stat every entry on a slow share.
        with os.scandir(current) as found:
            entries = sorted(found, key=lambda e: e.name.lower())
        for entry in entries:
            hidden = entry.name.startswith(".")
            if entry.is_dir() and not hidden:
                dirs.append({"name": entry.name, "path": entry.path})
            elif (
                want_files and not hidden and entry.is_file()
                and Path(entry.name).suffix.lower() in file_import.IMPORTABLE
            ):
                files.append({"name": entry.name, "path": entry.path, "size_bytes": entry.stat().st_size})
    except (PermissionError, OSError):
        pass

    parent = str(current.parent) if current not in roots and app_config.is_allowed(current.parent) else None

    return {
        "success": True,
        "current_path": str(current),
        "parent_path": parent,
        "directories": dirs,
        "dirs": [d["name"] for d in dirs],
        **({"files": files, "roots": [str(r) for r in roots]} if want_files else {}),
    }, 200


def handle_get_logging_config(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    return {"success": True, "config": get_logging_config()}, 200


def handle_post_logging_config(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    level = str(data.get("level", "INFO")).upper()
    layers = data.get("layers", [])

    if not isinstance(layers, list):
        layers = []

    layers_str = ",".join(layers)

    with conn:
        conn.execute(queries.INSERT_APP_SETTINGS_V2, (level,))
        conn.execute(queries.INSERT_APP_SETTINGS_V3, (layers_str,))

    set_logging_config(level, layers)

    return {
        "success": True,
        "message": f"Logging configuration saved: Level={level}, Layers={layers}",
        "config": get_logging_config(),
    }, 200


def handle_post_logs_purge(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    try:
        days = int(data.get("days", 14))
    except (ValueError, TypeError):
        return {"success": False, "error": "days must be a whole number."}, 400
    if days < 1:
        return {"success": False, "error": "days must be at least 1."}, 400

    cutoff = time.time() - days * 86400
    old_logs = [path for path in LOGS_DIR.glob("vault.log.*") if path.stat().st_mtime < cutoff] if LOGS_DIR.exists() else []
    bytes_found = sum(path.stat().st_size for path in old_logs)
    rows_found = conn.execute(queries.COUNT_OLD_AUDIT_ROWS, (f"-{days} days",)).fetchone()[0]
    confirm = bool(data.get("confirm"))
    # First, because this route deletes evidence, and the record of that has to survive a failure.
    audit.record(conn, "logs_purge", not confirm, {"days": days, "files": len(old_logs), "rows": rows_found})
    if not confirm:
        return {
            "success": True, "dry_run": True, "would_delete_files": len(old_logs), "reclaimable_bytes": bytes_found,
            "would_prune_rows": rows_found,
        }, 200

    purged_disk_files = 0
    reclaimed_bytes = 0
    problems = []
    for path in old_logs:
        try:
            size = path.stat().st_size
            path.unlink()
            purged_disk_files += 1
            reclaimed_bytes += size
        except OSError as e:
            problems.append(f"{path.name}: {e.strerror or e}")
    clear_ui_logs()

    pruned_rows = 0
    try:
        with conn:
            pruned_rows += conn.execute(queries.DELETE_DOWNLOAD_AUDIT_LOG_BY_LOGGED_AT, (f"-{days} days",)).rowcount
            pruned_rows += conn.execute(queries.DELETE_PIPELINE_TASKS_BY_STATUS, (f"-{days} days",)).rowcount
    except sqlite3.Error as e:
        problems.append(f"audit rows: {e}")

    log_layer("sql", logging.INFO, f"Purged {purged_disk_files} rotated log files and {pruned_rows} audit rows")
    result = {
        "purged_files_count": purged_disk_files,
        "reclaimed_disk_bytes": reclaimed_bytes,
        "pruned_db_rows": pruned_rows,
    }
    if problems:
        return {"success": False, "dry_run": False, "error": "; ".join(problems), **result}, 500
    return {
        "success": True, "dry_run": False, **result,
        "message": f"Purged {purged_disk_files} rotated files and {pruned_rows} audit rows older than {days} days.",
    }, 200


def _purge_refusal(conn: sqlite3.Connection, stash_path: Path) -> str | None:
    """Stops a stash path that overlaps the vault or a library, because purging there destroys originals."""
    for (root,) in conn.execute(queries.SELECT_PROTECTED_ROOTS).fetchall():
        resolved = Path(root).expanduser().resolve()
        if stash_path == resolved or resolved in stash_path.parents or stash_path in resolved.parents:
            return f"The print queue folder overlaps {resolved}, so nothing was purged."
    return None


def handle_post_stashes_purge(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """Purges export folders in stash_download_path older than X days."""
    data = body or {}
    try:
        days = int(data.get("days", 14))
    except (ValueError, TypeError):
        days = 14
    if days < 1:
        return {"error": "Purging needs an age of at least 1 day."}, 400

    settings = {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(queries.SELECT_APP_SETTINGS_BY_KEY).fetchall()
        settings = {r["key"]: r["value"] for r in rows}
    except Exception:
        pass

    stash_setting = settings.get("stash_download_path", "")
    stash_path = Path(stash_setting).expanduser().resolve() if stash_setting else Path(DEFAULT_STASH_EXPORT_PATH).resolve()

    if refusal := _purge_refusal(conn, stash_path):
        return {"error": refusal}, 400

    dry_run = not data.get("confirm")
    purged_folders = 0
    reclaimed_bytes = 0
    skipped = 0
    errors = []

    cutoff_time = time.time() - (days * 86400)

    if stash_path.exists() and stash_path.is_dir():
        for item in stash_path.iterdir():
            if item.is_dir():
                # The exporter's manifest is what proves a folder is an export, not something else.
                if not (item / "manifest.json").is_file():
                    skipped += 1
                    continue
                try:
                    mtime = item.stat().st_mtime
                    if mtime < cutoff_time:
                        folder_sz = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                        if not dry_run:
                            shutil.rmtree(item)
                        purged_folders += 1
                        reclaimed_bytes += folder_sz
                except Exception as e:
                    errors.append(f"{item.name}: {e}")

    if dry_run:
        result = {
            "success": True, "dry_run": True, "would_purge": purged_folders,
            "reclaimable_bytes": reclaimed_bytes, "errors": errors, "days": days, "skipped_no_manifest": skipped,
        }
        audit.record(conn, "stashes_purge", True, result)
        return result, 200

    log_layer("storage", logging.INFO, f"Purged {purged_folders} stash export folders older than {days} days ({reclaimed_bytes} bytes reclaimed).")
    audit.record(conn, "stashes_purge", False, {
        "purged_count": purged_folders, "reclaimed_bytes": reclaimed_bytes, "days": days, "errors": errors,
    })

    return {
        "success": True,
        "purged_count": purged_folders,
        "reclaimed_bytes": reclaimed_bytes,
        "errors": errors,
        "skipped_no_manifest": skipped,
        "message": f"Purged {purged_folders} print queue folders older than {days} days.",
    }, 200
