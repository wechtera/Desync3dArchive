"""
services/task_manager.py - Persistent background pipeline task orchestrator.

Supports:
- Playwright library scrapers (task_type = 'library_scrape')
- Async REST metadata ingestion (task_type = 'metadata_ingest')
- Vault integrity reconciliation (task_type = 'vault_reconcile')
- Retroactive archive peeker backfill (task_type = 'archive_peek_backfill')
- Background Slicer Stash materialization (task_type = 'stash_export')
- In-place External Library Reconciliation (task_type = 'external_library_reconcile')
Instrumented with dynamic topic-based subsystem logging ('http', 'storage', 'sql').
"""

import asyncio
import json
import logging
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from desync_archival import backup, external
from desync_archival import settings as app_config
from desync_archival.db.queries import task_manager as queries
from desync_archival.services import (
    audit,
    auth_service,
    catalog_levels,
    identity,
    inactive_log,
    jobs,
    secret_store,
)
from desync_archival.services.analytics import run_startup_analytics
from desync_archival.services.db import scoped_connection
from desync_archival.services.downloader import StorageUnavailableError
from desync_archival.services.logger import job_logged, log_layer, logger, redact
from desync_archival.services.paths import sanitize_segment

from .reconciler import VaultReconciler

KNOWN_TOP_LEVEL_KEYS = {
    "id", "url", "name", "visibility", "visibility_name", "listed",
    "status", "status_name", "description", "description_html",
    "printing_details", "printing_details_html", "featured", "support",
    "complexity", "complexity_name", "dimensions", "material_quantity",
    "images", "views", "likes", "published_at", "tags", "licenses",
    "filters", "file_mode", "categories", "files", "prints", "designer",
    "license", "archive_download_url", "is_liked", "user_collections",
    "is_saved", "price", "purchase_url", "is_bought",
}


def parse_scrape_event(line: str) -> dict[str, Any] | None:
    """Reads one scraper event. Anything else, such as a Node error, stays plain text."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and "event" in event else None


def parse_cookies(text: str) -> dict[str, str]:
    """Reads a Cookie-Editor list or a name-value object into a name-value dict."""
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        return {}
    if isinstance(parsed, list):
        return {c["name"]: str(c["value"]) for c in parsed if isinstance(c, dict) and "name" in c and "value" in c}
    if isinstance(parsed, dict):
        return {str(k): str(v) for k, v in parsed.items()}
    return {}


def inspect_schema_drift(model_id: int, data: dict) -> list[tuple[str, str]]:
    """Flags fields MMF added or dropped, so API changes show up in the data."""
    anomalies = []
    incoming_keys = set(data.keys())
    unexpected = incoming_keys - KNOWN_TOP_LEVEL_KEYS
    if unexpected:
        anomalies.append(("UNEXPECTED_FIELDS", f"New unhandled fields: {sorted(unexpected)}"))
    missing_anchors = {"id", "name", "files"} - incoming_keys
    if missing_anchors:
        anomalies.append(("MISSING_CORE_FIELDS", f"Missing core fields: {sorted(missing_anchors)}"))
    return anomalies


def extract_normalized_records(model_id: int, data: dict, backup_path: Path):
    designer_data = data.get("designer") or {}
    designer_id = designer_data.get("id") if isinstance(designer_data, dict) else None
    designer_name = (
        designer_data.get("name")
        or designer_data.get("username")
        or "Unknown Designer"
    ) if isinstance(designer_data, dict) else "Unknown Designer"
    designer_username = designer_data.get("username") if isinstance(designer_data, dict) else None

    primary_category, parent_category = None, None
    categories_obj = data.get("categories") or {}
    if isinstance(categories_obj, dict):
        cat_items = categories_obj.get("items") or []
        if isinstance(cat_items, list) and len(cat_items) > 0 and isinstance(cat_items[0], dict):
            primary_category = cat_items[0].get("name")
            parent_obj = cat_items[0].get("parent") or {}
            if isinstance(parent_obj, dict):
                parent_category = parent_obj.get("name")

    primary_image_url = None
    images = data.get("images") or []
    if isinstance(images, list) and len(images) > 0:
        target_img = next((img for img in images if isinstance(img, dict) and img.get("is_primary")), images[0])
        if isinstance(target_img, dict):
            primary_image_url = (
                (target_img.get("large") or {}).get("url")
                or (target_img.get("standard") or {}).get("url")
                or (target_img.get("original") or {}).get("url")
                or (target_img.get("thumbnail") or {}).get("url")
            )

    price_obj = data.get("price") or {}
    price_val, price_curr = None, None
    if isinstance(price_obj, dict):
        try:
            price_val = float(price_obj.get("value")) if price_obj.get("value") is not None else None
        except (ValueError, TypeError):
            price_val = None
        price_curr = price_obj.get("currency")

    pre_supported = 1 if data.get("support") else 0

    model_row = (
        model_id,
        data.get("name") or f"Model {model_id}",
        data.get("url"),
        data.get("description"),
        data.get("published_at"),
        designer_id,
        designer_name,
        designer_username,
        primary_category,
        parent_category,
        pre_supported,
        1 if data.get("featured") else 0,
        data.get("views") or 0,
        data.get("likes") or 0,
        price_val,
        price_curr,
        primary_image_url,
        app_config.stored_data_path(backup_path),
    )

    tag_rows = []
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                tag_rows.append((model_id, t.strip().lower()))

    file_rows = []
    files_obj = data.get("files") or {}
    file_items = files_obj.get("items") if isinstance(files_obj, dict) else (files_obj if isinstance(files_obj, list) else [])
    if isinstance(file_items, list):
        for item in file_items:
            if isinstance(item, dict) and item.get("id"):
                try:
                    size_int = int(item.get("size")) if item.get("size") is not None else None
                except (ValueError, TypeError):
                    size_int = None
                file_rows.append((
                    item["id"],
                    model_id,
                    item.get("filename") or item.get("path"),
                    size_int,
                    item.get("download_url"),
                    item.get("created_at"),
                ))

    archive_dl_url = data.get("archive_download_url")
    if not file_rows and archive_dl_url:
        clean_title = sanitize_segment(data.get("name") or f"Model_{model_id}")
        file_rows.append((
            identity.archive_file_key(model_id),
            model_id,
            f"{clean_title}.zip",
            None,
            archive_dl_url,
            data.get("published_at"),
        ))

    return model_row, tag_rows, file_rows


LOG_FLUSH_SECONDS = 1.0


class TaskLogBuffer:
    """Holds task log lines briefly, so a burst is one write.

    Each line used to be a connection and a commit, and each append rewrites the whole log value.
    Every line also goes to vault.log first, so a write that fails loses nothing for good.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Held for the whole write, so a blocked one can't be overtaken and swap a task's lines.
        self._writing = threading.Lock()
        self._lines: dict[tuple[Path, int], list[str]] = {}
        self._timer: threading.Timer | None = None

    def add(self, db_path: Path, task_id: int, line: str) -> None:
        with self._lock:
            self._lines.setdefault((db_path, task_id), []).append(line)
            if self._timer is None:
                self._timer = threading.Timer(LOG_FLUSH_SECONDS, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self, db_path: Path | None = None, task_id: int | None = None) -> None:
        """Writes one task's lines, or everyone's when called with nothing."""
        with self._writing:
            with self._lock:
                if task_id is None:
                    pending, self._lines = self._lines, {}
                    self._timer = None
                else:
                    key = (db_path, task_id)
                    pending = {key: self._lines.pop(key)} if key in self._lines else {}
            for (path, owner), lines in pending.items():
                try:
                    with scoped_connection(path) as conn:
                        conn.execute(queries.UPDATE_PIPELINE_TASKS_LOG_OUTPUT_BY_ID, ("\n".join(lines), owner))
                except sqlite3.Error as e:
                    logger.warning(f"Task #{owner}: {len(lines)} log lines stayed in vault.log only: {e}")


TASK_LOGS = TaskLogBuffer()

CANCELLING_LABEL = "Cancelling. Waiting for the task to stop."
KILLING_LABEL = "Force kill sent. If this stays here, the worker is stuck, so restart the server."


@dataclass
class Task:
    """The task a worker is running. Its stop flag and tool die with it, so it can't touch the next one's."""
    id: int
    stop: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen] = None


COVER_CONCURRENCY = 5

# Set by start_managers. Routes that aren't handed the manager reach it through this.
current: Optional["TaskManager"] = None


class TaskManager:
    """Runs one background pipeline task at a time."""

    def __init__(self, db_path: Path, root_dir: Optional[Path] = None):
        self.db_path = Path(db_path).resolve()
        self.root_dir = Path(root_dir).resolve() if root_dir else self.db_path.parent.resolve()
        self.scrapes_dir = self.root_dir / "scrapes"
        self.scrapes_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.active_task_id: Optional[int] = None
        # Kept per ID, because a stopped worker can outlive its slot and must still read as stopped.
        self._tasks: dict[int, Task] = {}
        self._scrape_error: str = ""

    def _task(self, task_id: Optional[int]) -> Optional[Task]:
        return self._tasks.get(task_id) if task_id is not None else None

    def _get_db(self) -> sqlite3.Connection:
        return scoped_connection(self.db_path)

    def _cache_cover(self, model_id: int, image_url: str | None) -> None:
        if not image_url:
            return
        cover_dir = (self.root_dir / "previews" / str(model_id)).resolve()
        cover_file = cover_dir / "cover.jpg"
        if cover_file.exists() and cover_file.stat().st_size > 0:
            return
        try:
            cover_dir.mkdir(parents=True, exist_ok=True)
            # NOTE: no browser signals here, by the owner's rule. A plain request works (S-15).
            with httpx.Client(timeout=12.0, follow_redirects=True) as client_img:
                resp = client_img.get(image_url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    cover_file.write_bytes(resp.content)
                    log_layer("http", logging.DEBUG, f"Model #{model_id}: Cached cover image")
        except (httpx.HTTPError, OSError) as e:
            log_layer("http", logging.WARNING, f"Model #{model_id}: Cover download failed: {e}")

    def _get_setting(self, key: str, default: str = "") -> str:
        if key in secret_store.KEYS and key in secret_store.load():
            return secret_store.load()[key]
        with self._get_db() as conn:
            row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY, (key,)).fetchone()
            return row["value"] if row and row["value"] is not None else default

    def _append_log(self, task_id: int, message: str):
        # This text lands in the database and the UI, which the logger's own filter never sees.
        message = redact(str(message))
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}"
        # FIXME: stdout goes to server.out, which nothing rotates. The logger line below already records this.
        print(f"[Task #{task_id}] {log_line}")
        logger.info(f"[Task #{task_id}] {message}")
        TASK_LOGS.add(self.db_path, task_id, log_line)

    def _flush_log(self, task_id: int) -> None:
        TASK_LOGS.flush(self.db_path, task_id)

    def _update_progress(self, task_id: int, pct: float, label: str):
        with self._get_db() as conn:
            with conn:
                conn.execute(queries.UPDATE_PIPELINE_TASKS_PROGRESS_PCT_BY_ID, (round(pct, 1), label, task_id))

    def _finalize_task(
        self,
        task_id: int,
        status: str,
        label: str,
        summary: dict,
        artifacts: Optional[dict] = None,
    ):
        self._flush_log(task_id)
        summary_json = json.dumps(summary)
        artifacts_json = json.dumps(artifacts or {})
        with self._get_db() as conn:
            with conn:
                conn.execute(queries.UPDATE_PIPELINE_TASKS_STATUS_BY_ID, (status, status, label, summary_json, artifacts_json, artifacts_json, task_id))

    def get_active_task(self) -> Optional[Dict[str, Any]]:
        with self._get_db() as conn:
            row = conn.execute(queries.SELECT_PIPELINE_TASKS_BY_STATUS).fetchone()

            if not row:
                return None

            data = dict(row)
            try:
                data["params"] = json.loads(row["params_json"] or "{}")
            except Exception:
                data["params"] = {}
            try:
                data["result_summary"] = json.loads(row["result_summary_json"] or "{}")
            except Exception:
                data["result_summary"] = {}
            try:
                data["artifacts"] = json.loads(row["artifacts_json"] or "{}")
            except Exception:
                data["artifacts"] = {}
            return data

    def get_recent_tasks(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._get_db() as conn:
            rows = conn.execute(queries.SELECT_PIPELINE_TASKS, (limit,)).fetchall()

        results = []
        for r in rows:
            item = dict(r)
            try:
                item["params"] = json.loads(r["params_json"] or "{}")
            except Exception:
                item["params"] = {}
            try:
                item["result_summary"] = json.loads(r["result_summary_json"] or "{}")
            except Exception:
                item["result_summary"] = {}
            try:
                item["artifacts"] = json.loads(r["artifacts_json"] or "{}")
            except Exception:
                item["artifacts"] = {}
            results.append(item)
        return results

    def _handle_scrape_line(self, task_id: int, line: str, expected_count: int) -> None:
        """Turns one line of scraper output into task log and progress."""
        event = parse_scrape_event(line)
        if event is None:
            self._append_log(task_id, line)
            return

        name = event.get("event")
        count = int(event.get("count") or 0)
        if name == "cycle":
            total = max(expected_count, count, 1)
            pct = min(90.0, 10.0 + (count / total) * 80.0)
            self._update_progress(task_id, pct, f"Cycle {event.get('cycle')}: {count} models intercepted...")
            self._append_log(task_id, f"Cycle {event.get('cycle')}: {count} unique models collected")
        elif name == "api_batch":
            self._append_log(task_id, f"API batch of {count} items from {event.get('path')}")
        elif name == "finalizing":
            self._update_progress(task_id, 95.0, "Finalizing outputs on disk...")
        elif name == "done":
            self._append_log(task_id, f"Scraper finished with {count} model IDs")
        elif name == "error":
            self._scrape_error = str(event.get("message", "Scrape failed"))
            self._append_log(task_id, self._scrape_error)
        else:
            self._append_log(task_id, str(event.get("message", line)))

    def run_scheduled_backup(self) -> bool:
        """Starts a backup if the last one is a month old. Returns whether it started one."""
        try:
            app_config.backup_dir()
        except app_config.SettingsError:
            return False
        last = self._get_setting("last_backup_epoch", "0")
        if time.time() - float(last or 0) < jobs.BACKUP_INTERVAL:
            return False
        attempted = self._get_setting("last_backup_attempt_epoch", "0")
        if time.time() - float(attempted or 0) < jobs.BACKUP_RETRY_INTERVAL:
            return False
        return bool(self.start_task("database_backup").get("success"))

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_backup_worker(self, task_id: int, params: dict[str, Any]):
        try:
            path = backup.create_backup(self.db_path, full=True)
            result = f"OK: {path.name}"
            status, label = jobs.COMPLETED, "Database backup complete"
        except (backup.BackupError, app_config.SettingsError, sqlite3.Error, OSError) as e:
            logger.error(f"Scheduled backup failed: {e}")
            result, status, label = f"Failed: {type(e).__name__}", jobs.FAILED, "Database backup failed"

        finished = str(time.time())
        stamps = {
            "last_backup_attempt_epoch": finished,
            "last_backup_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_backup_result": result,
        }
        # Only a good backup restarts the month. A failed one retries after BACKUP_RETRY_INTERVAL.
        if status == jobs.COMPLETED:
            stamps["last_backup_epoch"] = finished
        with self._get_db() as conn:
            auth_service.set_settings(conn, stamps)
        self._finalize_task(task_id, status, label, {"result": result})
        self._release(task_id)

    def record_finished(self, task_type: str, label: str, summary: dict[str, Any], status: str = jobs.COMPLETED) -> None:
        """A check that answers at once still gets a job row, so its last run can be shown."""
        with self._get_db() as conn:
            with conn:
                conn.execute(queries.INSERT_FINISHED_TASK, (task_type, status, label, json.dumps(summary)))

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_catalog_preview_worker(self, task_id: int, params: dict[str, Any]):
        try:
            report = VaultReconciler(self.db_path).backfill_catalog_previews(
                model_ids=params.get("model_ids"),
                progress_cb=lambda pct, label: self._update_progress(task_id, pct, label),
                log_cb=lambda message: self._append_log(task_id, message),
                cancel_cb=lambda: self._cancelled(task_id),
            )
            status = jobs.CANCELLED if self._cancelled(task_id) else jobs.COMPLETED
            label = f"{report.get('downloaded', 0)} cover images cached"
        except (OSError, sqlite3.Error) as e:
            self._append_log(task_id, f"Preview backfill stopped: {e}")
            report, status, label = {}, jobs.FAILED, "Preview backfill failed"
        self._finalize_task(task_id, status, label, report)
        self._release(task_id)

    def _run_path_realign_worker(self, task_id: int, params: dict[str, Any]):
        try:
            report = VaultReconciler(self.db_path).realign_model_paths(model_ids=params.get("model_ids"))
            for problem in report.get("errors", []):
                self._append_log(task_id, problem)
            moved = report.get("realigned_count", 0)
            status = jobs.FAILED if report.get("errors") and not moved else jobs.COMPLETED
            label = f"{moved} folders moved, {len(report.get('errors', []))} refused or failed"
        except (OSError, sqlite3.Error, StorageUnavailableError) as e:
            self._append_log(task_id, f"Path realign stopped: {e}")
            report, status, label = {}, jobs.FAILED, "Path realign failed"
        with self._get_db() as conn:
            audit.record(conn, "path_realign", False, {"task_id": task_id, **report})
        self._finalize_task(task_id, status, label, report)
        self._release(task_id)

    def _run_untracked_ingest_worker(self, task_id: int, params: dict[str, Any]):
        try:
            report = VaultReconciler(self.db_path).ingest_untracked_files(params.get("items") or [])
            for problem in report.get("errors", []):
                self._append_log(task_id, problem)
            added = report.get("ingested_count", 0)
            status = jobs.FAILED if report.get("errors") and not added else jobs.COMPLETED
            label = f"{added} files added, {len(report.get('errors', []))} refused or failed"
        except (OSError, sqlite3.Error, StorageUnavailableError) as e:
            self._append_log(task_id, f"Adding untracked files stopped: {e}")
            report, status, label = {}, jobs.FAILED, "Adding untracked files failed"
        with self._get_db() as conn:
            audit.record(conn, "ingest_untracked", False, {"task_id": task_id, **report})
        self._finalize_task(task_id, status, label, report)
        self._release(task_id)

    def _run_library_measure_worker(self, task_id: int, params: dict[str, Any]):
        from desync_archival.services.external_indexer import folder_totals

        try:
            files, size = folder_totals(Path(params["path"]), lambda: self._cancelled(task_id))
            if self._cancelled(task_id):
                self._finalize_task(task_id, jobs.CANCELLED, "Library size check cancelled", {})
            else:
                with self._get_db() as conn:
                    conn.execute(queries.UPDATE_EXTERNAL_LIBRARIES_DISK_BYTES_BY_ID, (size, files, params["library_id"]))
                self._finalize_task(task_id, jobs.COMPLETED, f"{files} files measured", {"files": files, "bytes": size})
        except (OSError, sqlite3.Error) as e:
            self._append_log(task_id, f"Measuring the library stopped: {e}")
            self._finalize_task(task_id, jobs.FAILED, "Library size check failed", {})
        self._release(task_id)

    def _inventory_library_pdfs(self, task_id: int, library_id: int) -> None:
        from desync_archival.services import documents

        try:
            with self._get_db() as conn:
                documents.inventory_one(
                    conn, library_id, lambda message: self._append_log(task_id, message), lambda: self._cancelled(task_id),
                )
        except (OSError, sqlite3.Error) as e:
            # The scan itself worked, and Update PDFs can pick these up later.
            self._append_log(task_id, f"PDFs weren't recorded: {e}")

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_document_worker(self, task_id: int):
        from desync_archival.services import documents

        try:
            with self._get_db() as conn:
                totals = documents.update(
                    conn, lambda message: self._append_log(task_id, message),
                    lambda pct, label: self._update_progress(task_id, pct, label),
                    lambda: self._cancelled(task_id),
                )
            status = jobs.CANCELLED if self._cancelled(task_id) else jobs.COMPLETED
            label = f"{totals['added']} new PDFs, {totals['made']} previews made, {totals['failed']} couldn't be drawn"
        except (OSError, sqlite3.Error) as e:
            self._append_log(task_id, f"PDF update stopped: {e}")
            totals, status, label = {}, jobs.FAILED, "PDF update failed"
        self._finalize_task(task_id, status, label, totals)
        self._release(task_id)

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_file_import_worker(self, task_id: int, params: dict[str, Any]):
        from desync_archival.services import file_import

        try:
            with self._get_db() as conn:
                totals = file_import.import_paths(
                    conn, self.db_path, [Path(p) for p in params["paths"]], params.get("creator", ""),
                    lambda message: self._append_log(task_id, message),
                    lambda pct, label: self._update_progress(task_id, pct, label),
                    lambda: self._cancelled(task_id),
                )
            label = f"Imported {totals['imported']} files, {totals['repeats']} already in the catalog"
            if totals["archives"]:
                label += f", {totals['archives']} archives to review"
            status = jobs.COMPLETED
        except file_import.ImportCancelled:
            self._append_log(task_id, "Import stopped. Anything already finished stays in the catalog.")
            totals, status, label = {}, jobs.CANCELLED, "Import cancelled"
        except (file_import.ImportRefused, OSError, sqlite3.Error) as e:
            self._append_log(task_id, f"Import stopped: {e}")
            totals, status, label = {}, jobs.FAILED, "Import failed"
        self._finalize_task(task_id, status, label, totals)
        self._release(task_id)

    def _rebuild_releases(self, task_id: int) -> None:
        """Ingest changes the catalog, so releases are rebuilt. A failure here keeps the ingest result."""
        try:
            with self._get_db() as conn:
                run_startup_analytics(conn)
            self._append_log(task_id, "Monthly release groupings rebuilt.")
        except Exception:
            logger.exception("Release rebuild failed after ingest")
            self._append_log(task_id, "Release rebuild failed. See the application log.")

    def _close_row(self, task_id: int) -> None:
        """Cancel leaves closing the row to the worker, so one that returned without a result would look busy forever."""
        self._flush_log(task_id)
        if self._cancelled(task_id):
            status, label = jobs.CANCELLED, "Cancelled by operator"
        else:
            status, label = jobs.FAILED, "The task ended without a result. See the application log."
        with self._get_db() as conn:
            conn.execute(queries.UPDATE_RUNNING_TASK_RESULT, (status, status, label, task_id))

    def _spawn(self, task_id: int, task_type: str, target: Callable, args: tuple) -> None:
        """The one place background jobs start, with a time limit and a guard against crashes."""
        timer = threading.Timer(jobs.time_limit(task_type), self._on_time_limit, args=(task_id, task_type))
        timer.daemon = True

        def guarded() -> None:
            external.stop_when(lambda: self._cancelled(task_id))
            try:
                target(*args)
            except Exception:
                logger.exception(f"Task #{task_id} ({task_type}) crashed")
                self._finalize_task(task_id, jobs.FAILED, "Task crashed. See the application log.", {})
            finally:
                timer.cancel()
                try:
                    self._close_row(task_id)
                finally:
                    self._release(task_id)

        timer.start()
        threading.Thread(target=guarded, daemon=True, name=f"task-{task_id}").start()

    def _on_time_limit(self, task_id: int, task_type: str) -> None:
        if self.active_task_id != task_id:
            return
        logger.error(f"Task #{task_id} ({task_type}) passed its time limit and is being cancelled")
        self._append_log(task_id, "Time limit reached. Cancelling.")
        self.cancel_task(task_id)

    def start_task(self, task_type: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatches an arbitrary pipeline task asynchronously."""
        with self._lock:
            with self._get_db() as conn:
                running = conn.execute(
                    queries.SELECT_PIPELINE_TASKS_BY_STATUS_V2
                ).fetchone()

                if running:
                    return {
                        "success": False,
                        "error": f"A pipeline task (#{running['id']} {running['task_type']}) is already running.",
                        "task_id": running["id"],
                    }

                task_params = dict(params or {})
                if "db_path" not in task_params:
                    task_params["db_path"] = str(self.db_path)

                params_json = json.dumps(task_params)
                cursor = conn.execute(queries.INSERT_PIPELINE_TASKS, (task_type, params_json))
                task_id = cursor.lastrowid
                assert task_id is not None
                conn.execute(queries.UPDATE_PIPELINE_TASKS_STARTED_AT_BY_ID, (task_id,))

            self._begin(task_id)

        log_layer("sql", logging.INFO, f"Dispatched background task #{task_id} (Type: {task_type})")

        workers = {
            "vault_reconcile": (self._run_reconcile_worker, (task_id, task_params)),
            "archive_peek_backfill": (self._run_archive_peek_worker, (task_id, task_params)),
            "library_scrape": (self._run_scrape_worker, (task_id,)),
            "stash_export": (self._run_stash_export_worker, (task_id, task_params)),
            "external_library_reconcile": (self._run_external_indexer_worker, (task_id, task_params)),
            "database_backup": (self._run_backup_worker, (task_id, task_params)),
            "file_import": (self._run_file_import_worker, (task_id, task_params)),
            "document_update": (self._run_document_worker, (task_id,)),
            "catalog_preview_backfill": (self._run_catalog_preview_worker, (task_id, task_params)),
            "path_realign": (self._run_path_realign_worker, (task_id, task_params)),
            "untracked_ingest": (self._run_untracked_ingest_worker, (task_id, task_params)),
            "library_measure": (self._run_library_measure_worker, (task_id, task_params)),
        }
        if task_type not in workers:
            self._finalize_task(task_id, "failed", f"Unsupported task type: {task_type}", {})
            self._release(task_id)
            return {"success": False, "error": f"Unknown task_type '{task_type}'"}

        target, args = workers[task_type]
        self._spawn(task_id, task_type, target, args)

        return {"success": True, "task_id": task_id, "task_type": task_type}

    def _begin(self, task_id: int) -> None:
        """Called holding `_lock`."""
        self.active_task_id = task_id
        self._tasks[task_id] = Task(task_id)

    def _cancelled(self, task_id: int) -> bool:
        task = self._task(task_id)
        return task is not None and task.stop.is_set()

    def _release(self, task_id: int) -> None:
        """Gives up the slot, only if this task still holds it."""
        with self._lock:
            self._tasks.pop(task_id, None)
            if self.active_task_id == task_id:
                self.active_task_id = None

    def _ask_to_stop(self, task_id: int | None, force: bool, note: str) -> bool:
        """Called holding `_lock`. Returns False if no worker here is running the task.

        The row stays `running` until the worker exits, so busy checks stay true and nothing starts
        beside it. A Python thread can't be stopped from outside, so a stuck one keeps the slot
        until a restart, which is safer than running a second task next to it.
        """
        task = self._task(task_id)
        if task is None:
            return False
        task.stop.set()
        if task.process and task.process.poll() is None:
            try:
                if force:
                    task.process.kill()
                else:
                    task.process.terminate()
            except OSError:
                pass
        with self._get_db() as conn:
            conn.execute(queries.UPDATE_RUNNING_TASK_LABEL, (note, task_id))
        return True

    def cancel_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            if not self._ask_to_stop(task_id, force=False, note=CANCELLING_LABEL):
                with self._get_db() as conn:
                    conn.execute(queries.UPDATE_PIPELINE_TASKS_STATUS_BY_ID_V2, (task_id,))
            logger.info(f"[TaskManager] Cancelled task #{task_id} by operator.")
            return {"success": True, "task_id": task_id}

    def kill_task(self, task_id: int) -> Dict[str, Any]:
        """Kills any subprocess and asks the worker to stop. See `_ask_to_stop` on the slot."""
        with self._lock:
            if not self._ask_to_stop(task_id, force=True, note=KILLING_LABEL):
                with self._get_db() as conn:
                    conn.execute(queries.UPDATE_PIPELINE_TASKS_STATUS_BY_ID_V3, (task_id,))
            logger.warning(f"[TaskManager] Force killed task #{task_id} by operator.")
            return {"success": True, "task_id": task_id}

    def get_status(self) -> Dict[str, Any]:
        with self._get_db() as conn:
            active_row = conn.execute(queries.SELECT_PIPELINE_TASKS_BY_STATUS_V3).fetchone()

            recent_rows = conn.execute(queries.SELECT_PIPELINE_TASKS_V2).fetchall()

        active_data = None
        if active_row:
            active_data = dict(active_row)
            try:
                active_data["result_summary"] = json.loads(active_row["result_summary_json"] or "{}")
            except Exception:
                active_data["result_summary"] = {}
            try:
                active_data["artifacts"] = json.loads(active_row["artifacts_json"] or "{}")
            except Exception:
                active_data["artifacts"] = {}

        return {
            "active_task": active_data,
            "recent_tasks": [dict(row) for row in recent_rows],
        }

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        """One task with its log and reports, which the status poll leaves out."""
        with self._get_db() as conn:
            row = conn.execute(queries.SELECT_PIPELINE_TASK_BY_ID, (task_id,)).fetchone()
        if not row:
            return None
        task = dict(row)
        for field, column in (("result_summary", "result_summary_json"), ("artifacts", "artifacts_json")):
            try:
                task[field] = json.loads(task.pop(column) or "{}")
            except ValueError:
                task[field] = {}
        return task

    def cancel_active_task(self) -> bool:
        with self._lock:
            if not self._ask_to_stop(self.active_task_id, force=False, note=CANCELLING_LABEL):
                with self._get_db() as conn:
                    conn.execute(queries.UPDATE_PIPELINE_TASKS_STATUS_BY_STATUS)
            return True

    def kill_active_task(self) -> bool:
        with self._lock:
            if not self._ask_to_stop(self.active_task_id, force=True, note=KILLING_LABEL):
                with self._get_db() as conn:
                    conn.execute(queries.UPDATE_PIPELINE_TASKS_STATUS_BY_STATUS_V2)
            return True

    def count_clearable_tasks(self) -> int:
        with self._get_db() as conn:
            return conn.execute(
                queries.COUNT_PIPELINE_TASKS_BY_STATUS
            ).fetchone()[0]

    def clear_task_history(self) -> int:
        """Deletes finished tasks, which also clears saved duplicate results."""
        with self._get_db() as conn:
            with conn:
                cursor = conn.execute(queries.DELETE_PIPELINE_TASKS_BY_STATUS)
                return cursor.rowcount

    def list_scrape_directories(self) -> List[Dict[str, Any]]:
        results = []
        if not self.scrapes_dir.exists():
            return results

        for entry in sorted(self.scrapes_dir.iterdir(), reverse=True):
            if entry.is_dir() and entry.name.startswith("scrape_"):
                id_file = entry / "model_ids.txt"
                json_file = entry / "models.json"
                count = 0
                if id_file.exists():
                    try:
                        count = sum(1 for line in id_file.read_text(encoding="utf-8").splitlines() if line.strip())
                    except Exception:
                        count = 0

                results.append({
                    "folder": entry.name,
                    "relative_path": f"scrapes/{entry.name}",
                    "has_ids": id_file.exists(),
                    "has_models_json": json_file.exists(),
                    "model_count": count,
                    "created_at": entry.stat().st_mtime,
                })
        return results

    # -------------------------------------------------------------------------
    # 1. Scraper Execution Worker
    # -------------------------------------------------------------------------

    def start_library_scrape(self) -> tuple[dict, int]:
        res = self.start_task("library_scrape")
        if not res.get("success"):
            return {"error": res.get("error")}, 409
        return res, 200

    def remove_scrape_cookies(self) -> int:
        """Deletes every cookie file a scrape left. Each is a live session, so none may outlast its run."""
        found = list(self.scrapes_dir.glob("*/cookies.json"))
        for path in found:
            path.unlink(missing_ok=True)
        if found:
            logger.warning(f"[TaskManager] Removed {len(found)} session cookie files left in scrape folders.")
        return len(found)

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_scrape_worker(self, task_id: int):
        try:
            self._scrape_library(task_id)
        finally:
            # One scrape runs at a time, so anything here belongs to this run.
            self.remove_scrape_cookies()

    def _scrape_library(self, task_id: int):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        target_dir = self.scrapes_dir / f"scrape_{timestamp}"
        target_dir.mkdir(parents=True, exist_ok=True)

        self._append_log(task_id, f"Target directory initialized: scrapes/scrape_{timestamp}")

        cookies_json_val = self._get_setting("cookies_json", "[]").strip()
        cookies_dest = target_dir / "cookies.json"
        try:
            parsed = json.loads(cookies_json_val) if cookies_json_val else []
            cookies_dest.touch(mode=0o600)
            cookies_dest.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            count_label = len(parsed) if isinstance(parsed, list) else "valid"
            self._append_log(task_id, f"Exported {count_label} session cookies to {target_dir.name}/cookies.json")
            log_layer("http", logging.DEBUG, f"Scraper exported session cookies to {target_dir.name}/cookies.json")
        except Exception as e:
            self._append_log(task_id, f"Notice: Error exporting cookies: {e}. Writing fallback empty list.")
            cookies_dest.write_text("[]", encoding="utf-8")

        node_bin = shutil.which("node") or "/opt/homebrew/bin/node"
        scrape_script = app_config.scraper_script()
        if not scrape_script.exists():
            err_msg = f"Fatal: scrape.js script not found at {scrape_script}"
            self._append_log(task_id, err_msg)
            self._finalize_task(task_id, "failed", err_msg, {})
            self._release(task_id)
            return

        self._update_progress(task_id, 5.0, "Launching Playwright browser...")

        with self._get_db() as conn:
            expected_count = conn.execute(queries.COUNT_COLLECTIONS_BY_SOURCE).fetchone()[0]
        cmd = [node_bin, str(scrape_script), "--cookies", str(cookies_dest), "--expected", str(expected_count)]
        if self._get_setting("scraper_headless", "1") != "0":
            cmd.append("--headless")
        self._scrape_error = ""
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(target_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                task = self._task(task_id)
                if task:
                    task.process = process

            for line in process.stdout:
                line_str = line.strip()
                if not line_str:
                    continue
                self._handle_scrape_line(task_id, line_str, expected_count)

            process.wait()
            return_code = process.returncode
            if return_code == 0 and not self._cancelled(task_id) and cookies_dest.exists():
                with self._get_db() as conn:
                    renewed = auth_service.keep_fresh_cookies(conn, cookies_dest.read_text(encoding="utf-8"))
                self._append_log(task_id, f"Saved {renewed} renewed session cookies.")

            if self._cancelled(task_id):
                self._finalize_task(task_id, "cancelled", "Scrape cancelled by user.", {})
                return

            if return_code != 0:
                err = self._scrape_error or f"scrape.js exited with non-zero return code: {return_code}"
                self._finalize_task(task_id, "failed", err, {})
                return

            ids_file = target_dir / "model_ids.txt"
            models_file = target_dir / "models.json"

            model_count = 0
            if ids_file.exists():
                lines = [l for l in ids_file.read_text(encoding="utf-8").splitlines() if l.strip()]
                model_count = len(lines)

            artifacts = {
                "folder": target_dir.name,
                "model_ids_path": f"scrapes/{target_dir.name}/model_ids.txt",
                "models_json_path": f"scrapes/{target_dir.name}/models.json" if models_file.exists() else None,
                "model_count": model_count,
            }

            summary = {
                "status": "success",
                "folder": target_dir.name,
                "total_models_collected": model_count,
                "completed_at": datetime.now().isoformat(),
            }

            self._append_log(task_id, f"Scrape complete! Saved {model_count} models in scrapes/{target_dir.name}/")
            self._finalize_task(task_id, "completed", f"Scrape complete ({model_count} models)", summary, artifacts)

        except Exception as e:
            self._finalize_task(task_id, "failed", f"Execution error: {e}", {})
        finally:
            self._release(task_id)

    # -------------------------------------------------------------------------
    # 2. Metadata Ingestion Pipeline Worker
    # -------------------------------------------------------------------------

    def start_metadata_ingest(self, model_ids: List[int], source_label: str) -> tuple[dict, int]:
        clean_ids: List[int] = []
        seen = set()
        for mid in model_ids:
            if mid > 0 and mid not in seen:
                seen.add(mid)
                clean_ids.append(mid)

        if not clean_ids:
            return {"error": "No valid model IDs provided for ingestion."}, 400

        with self._lock:
            with self._get_db() as conn:
                running = conn.execute(queries.SELECT_PIPELINE_TASKS_ID_BY_STATUS).fetchone()
                if running:
                    return {"error": f"A pipeline task (Task #{running['id']}) is already running."}, 409

                active_ids = {
                    row["external_key"]
                    for row in conn.execute(
                        queries.SELECT_COLLECTIONS_EXTERNAL_KEY_BY_SOURCE
                    ).fetchall()
                }
                pending_ids = [mid for mid in clean_ids if str(mid) not in active_ids]
                skipped_count = len(clean_ids) - len(pending_ids)

                if not pending_ids:
                    summary = {
                        "status": "completed",
                        "source": source_label,
                        "submitted": len(clean_ids),
                        "skipped": skipped_count,
                        "ingested": 0,
                        "tombstoned": 0,
                        "failed": 0,
                        "creators": {},
                        "completed_at": datetime.now().isoformat(),
                    }
                    cursor = conn.execute(queries.INSERT_PIPELINE_TASKS_V2, (
                        f"Source: {source_label}\nSubmitted: {len(clean_ids)}\nSkipped (already active in DB): {skipped_count}\nPending/Retry: 0\nDatabase up to date.",
                        json.dumps(summary),
                    ))
                    return {
                        "success": True,
                        "task_id": cursor.lastrowid,
                        "message": "Database is already up to date. Zero new or unavailable models to process.",
                        "submitted": len(clean_ids),
                        "skipped": skipped_count,
                        "new": 0,
                    }, 200

                cursor = conn.execute(queries.INSERT_PIPELINE_TASKS_V3, (json.dumps({"source": source_label, "count": len(clean_ids)}),))
                task_id = cursor.lastrowid
                assert task_id is not None
                conn.execute(queries.UPDATE_PIPELINE_TASKS_STARTED_AT_BY_ID, (task_id,))

            self._begin(task_id)

        self._spawn(
            task_id, "metadata_ingest", self._run_ingest_worker,
            (task_id, pending_ids, skipped_count, source_label, len(clean_ids)),
        )

        return {
            "success": True,
            "task_id": task_id,
            "submitted": len(clean_ids),
            "skipped": skipped_count,
            "new": len(pending_ids),
        }, 200

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_ingest_worker(
        self,
        task_id: int,
        pending_ids: List[int],
        skipped_count: int,
        source_label: str,
        total_submitted: int,
    ):
        try:
            asyncio.run(self._async_ingest(task_id, pending_ids, skipped_count, source_label, total_submitted))
        except Exception as e:
            self._append_log(task_id, f"Fatal ingestion exception: {e}")
            self._finalize_task(task_id, "failed", f"Ingestion error: {e}", {})
        finally:
            self._release(task_id)

    def _save_ingested(
        self, model_id: int, data: dict[str, Any], model_row: tuple, tag_rows: list, file_rows: list,
        anomalies: list, is_tombstone: bool,
    ) -> int:
        """Writes one model's catalog rows off the event loop, and returns its local ID."""
        with self._get_db() as conn:
            local_id = identity.save_mmf_model(conn, model_id, model_row[1:])

            archive_dl = data.get("archive_download_url")
            if archive_dl:
                conn.execute(queries.UPDATE_COLLECTIONS_ARCHIVE_DOWNLOAD_URL_BY_ID, (archive_dl, local_id))

            conn.execute(queries.DELETE_COLLECTION_TAGS_BY_MODEL_ID, (local_id,))
            if tag_rows:
                conn.executemany(
                    queries.INSERT_COLLECTION_TAGS,
                    [(local_id, catalog_levels.clean_tag(tag)) for _, tag in tag_rows],
                )

            identity.save_mmf_files(
                conn, local_id, [(row[0], row[2], row[3], row[4], row[5]) for row in file_rows]
            )

            if anomalies:
                conn.executemany(queries.INSERT_SCHEMA_ANOMALIES, [(local_id, anom[0], anom[1]) for anom in anomalies])

            if is_tombstone:
                inactive_log.record(
                    conn, inactive_log.MODEL, local_id, data.get("name"), inactive_log.METADATA_INGEST,
                    inactive_log.UNAVAILABLE, data.get("description"),
                )
            else:
                inactive_log.resolve(conn, inactive_log.MODEL, local_id, inactive_log.UNAVAILABLE)
        return local_id

    async def _cache_covers(self, task_id: int, covers: list[tuple[int, str | None]]) -> None:
        limit = asyncio.Semaphore(COVER_CONCURRENCY)

        async def fetch(model_id: int, url: str | None) -> None:
            async with limit:
                # Skipped covers aren't lost. The catalog previews job picks them up.
                if not self._cancelled(task_id):
                    await asyncio.to_thread(self._cache_cover, model_id, url)

        await asyncio.gather(*(fetch(model_id, url) for model_id, url in covers))

    async def _async_ingest(
        self,
        task_id: int,
        pending_ids: List[int],
        skipped_count: int,
        source_label: str,
        total_submitted: int,
    ):
        metadata_setting = self._get_setting("metadata_backup_path", "").strip()
        metadata_dir = (self.root_dir / metadata_setting).resolve() if metadata_setting else (self.root_dir / "metadata").resolve()
        metadata_dir.mkdir(parents=True, exist_ok=True)

        self._append_log(task_id, f"Starting ingestion for {len(pending_ids)} models (source: {source_label}).")
        self._append_log(task_id, f"Metadata raw backup directory: {metadata_dir}")
        if skipped_count > 0:
            self._append_log(task_id, f"Skipping {skipped_count} active models already recorded in SQLite.")

        # NOTE: metadata is OAuth only, as a plain client, by the owner's rule (S-15). A plain request
        # with a token was tested against /objects/{id} on 2026-11-01. Saved metadata needs no sign-in.
        auth_token = self._get_setting("mmf_access_token", "").strip()
        headers = {"Accept": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            self._append_log(task_id, "Using your MMF sign-in.")
        else:
            self._append_log(task_id, "No MMF sign-in, so only saved metadata can be read. Sign in under Settings to fetch the rest.")

        semaphore = asyncio.Semaphore(5)
        db_lock = asyncio.Lock()
        creator_breakdown: Dict[str, Dict[str, int]] = {}
        covers: list[tuple[int, str | None]] = []

        total_pending = len(pending_ids)
        processed_count = 0
        ingested_count = 0
        tombstone_count = 0
        failed_count = 0

        async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True) as client:

            async def process_single_model(model_id: int):
                nonlocal processed_count, ingested_count, tombstone_count, failed_count, auth_token
                if self._cancelled(task_id):
                    return

                backup_file = metadata_dir / f"model_{model_id}.json"
                data = None
                is_tombstone = False

                if backup_file.exists() and backup_file.stat().st_size > 0:
                    try:
                        with open(backup_file, "r", encoding="utf-8") as f:
                            cached = json.load(f)
                            if not cached.get("__tombstone__"):
                                data = cached
                                log_layer("storage", logging.DEBUG, f"Model #{model_id}: Loaded cached raw metadata.")
                    except Exception:
                        data = None

                if data is None and auth_token:
                    url = f"https://www.myminifactory.com/api/v2/objects/{model_id}"
                    async with semaphore:
                        for attempt in range(1, 4):
                            if self._cancelled(task_id):
                                return
                            if not auth_token:
                                break
                            try:
                                log_layer("http", logging.DEBUG, f"Requesting metadata: GET {url} (Attempt {attempt})")
                                res = await client.get(url)
                                if res.status_code == 200:
                                    data = res.json()
                                    with open(backup_file, "w", encoding="utf-8") as f:
                                        json.dump(data, f, indent=2)
                                    await asyncio.sleep(0.15)
                                    break
                                elif res.status_code == 401:
                                    # Every other model would answer the same, so one message and no more requests.
                                    if auth_token:
                                        auth_token = ""
                                        self._append_log(task_id, "Your MMF sign-in has expired (401). Sign in again under Settings.")
                                    log_layer("http", logging.WARNING, f"Model #{model_id}: 401, sign-in expired.")
                                    break
                                elif res.status_code == 429:
                                    wait_time = int(res.headers.get("Retry-After", 5))
                                    self._append_log(task_id, f"Rate limited on model {model_id}. Waiting {wait_time}s...")
                                    log_layer("http", logging.WARNING, f"Model #{model_id}: 429 Rate limited. Sleeping {wait_time}s.")
                                    await asyncio.sleep(wait_time)
                                    continue
                                elif res.status_code in (404, 410):
                                    self._append_log(task_id, f"Model {model_id} currently unavailable (HTTP {res.status_code}). Tombstone retained.")
                                    log_layer("http", logging.INFO, f"Model #{model_id}: Returned HTTP {res.status_code} (Tombstone).")
                                    is_tombstone = True
                                    data = {
                                        "id": model_id,
                                        "name": f"[Unavailable Model {model_id}]",
                                        "description": f"Object returned HTTP {res.status_code} during ingestion.",
                                        "__tombstone__": True,
                                    }
                                    with open(backup_file, "w", encoding="utf-8") as f:
                                        json.dump(data, f, indent=2)
                                    break
                                elif res.status_code == 403:
                                    self._append_log(task_id, f"Model {model_id} access forbidden (HTTP 403).")
                                    log_layer("http", logging.ERROR, f"Model #{model_id}: Access forbidden HTTP 403.")
                                    break
                                else:
                                    self._append_log(task_id, f"Model {model_id} unexpected HTTP {res.status_code} on attempt {attempt}.")
                                    log_layer("http", logging.WARNING, f"Model #{model_id}: Unexpected HTTP {res.status_code}.")
                            except Exception as ex:
                                log_layer("http", logging.WARNING, f"Model #{model_id} network exception: {ex}")
                                await asyncio.sleep(1.0 * attempt)

                if not data:
                    failed_count += 1
                    processed_count += 1
                    return

                anomalies = inspect_schema_drift(model_id, data) if not is_tombstone else []
                if anomalies:
                    log_layer("sql", logging.WARNING, f"Model #{model_id} schema anomalies detected: {anomalies}")

                model_row, tag_rows, file_rows = extract_normalized_records(model_id, data, backup_file)

                designer_name = model_row[6]
                pre_supported = bool(model_row[10])

                try:
                    async with db_lock:
                        local_id = await asyncio.to_thread(
                            self._save_ingested, model_id, data, model_row, tag_rows, file_rows, anomalies, is_tombstone,
                        )
                    covers.append((local_id, model_row[16]))

                    if is_tombstone:
                        tombstone_count += 1
                    else:
                        ingested_count += 1
                        if designer_name not in creator_breakdown:
                            creator_breakdown[designer_name] = {"count": 0, "pre_supported": 0}
                        creator_breakdown[designer_name]["count"] += 1
                        if pre_supported:
                            creator_breakdown[designer_name]["pre_supported"] += 1

                    log_layer("sql", logging.DEBUG, f"Model #{model_id}: Persisted metadata to library.db ({len(file_rows)} files)")

                except Exception as db_err:
                    self._append_log(task_id, f"Database write error on model {model_id}: {db_err}")
                    log_layer("sql", logging.ERROR, f"Database write error on Model #{model_id}: {db_err}")
                    failed_count += 1

                processed_count += 1
                pct = round((processed_count / total_pending) * 100.0, 1)

                if processed_count % 10 == 0 or processed_count == total_pending:
                    await asyncio.to_thread(
                        self._update_progress,
                        task_id,
                        pct,
                        f"Ingesting: {processed_count}/{total_pending} ({ingested_count} saved, {tombstone_count} unavailable, {failed_count} errors)",
                    )

            batch_size = 25
            for i in range(0, len(pending_ids), batch_size):
                if self._cancelled(task_id):
                    break
                batch = pending_ids[i:i + batch_size]
                await asyncio.gather(*(process_single_model(mid) for mid in batch))

        # A slow cover used to hold up every request in its batch, so covers wait for the metadata.
        await self._cache_covers(task_id, covers)

        summary = {
            "status": "cancelled" if self._cancelled(task_id) else "completed",
            "source": source_label,
            "total_submitted": total_submitted,
            "skipped_already_in_db": skipped_count,
            "ingested": ingested_count,
            "tombstoned": tombstone_count,
            "failed": failed_count,
            "creators": creator_breakdown,
            "completed_at": datetime.now().isoformat(),
        }

        artifacts = {
            "metadata_dir": str(metadata_dir),
            "models_ingested": ingested_count,
            "tombstoned_count": tombstone_count,
        }

        if self._cancelled(task_id):
            self._append_log(task_id, f"Ingestion cancelled by user ({ingested_count} models saved before stop).")
            self._finalize_task(task_id, "cancelled", f"Ingestion cancelled ({ingested_count} saved)", summary, artifacts)
        else:
            self._append_log(
                task_id,
                f"Ingestion finished! Ingested {ingested_count} models, {tombstone_count} unavailable (tombstoned), {failed_count} failed, {skipped_count} active skipped.",
            )
            self._rebuild_releases(task_id)
            self._finalize_task(
                task_id,
                "completed",
                f"Ingestion complete ({ingested_count} ingested, {tombstone_count} unavailable)",
                summary,
                artifacts,
            )

    # -------------------------------------------------------------------------
    # 3. Vault Reconciliation Pipeline Worker
    # -------------------------------------------------------------------------

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_reconcile_worker(self, task_id: int, params: Dict[str, Any]):
        scan_mode = params.get("scan_mode", "fast")
        self._append_log(task_id, f"Vault Reconciliation worker started (Mode: {scan_mode.upper()}).")

        def log_cb(msg: str):
            self._append_log(task_id, msg)

        def progress_cb(pct: float, label: str):
            self._update_progress(task_id, pct, label)

        def cancel_cb() -> bool:
            return self._cancelled(task_id)

        try:
            reconciler = VaultReconciler(self.db_path)
            report = reconciler.execute_reconciliation(
                scan_mode=scan_mode,
                progress_cb=progress_cb,
                log_cb=log_cb,
                cancel_cb=cancel_cb,
            )

            if self._cancelled(task_id):
                self._append_log(task_id, "Reconciliation scan cancelled by operator.")
                self._finalize_task(
                    task_id,
                    "cancelled",
                    "Reconciliation cancelled by operator",
                    report.get("summary", {}),
                    report,
                )
                return

            if not report.get("success", False):
                err = report.get("error", "Reconciliation error occurred")
                self._append_log(task_id, f"Reconciliation failed: {err}")
                self._finalize_task(
                    task_id,
                    "failed",
                    err,
                    report.get("summary", {}),
                    report,
                )
                return

            summary = report.get("summary", {})
            total_audited = summary.get("total_audited", 0)
            healthy = summary.get("healthy", 0)
            discrepancies = summary.get("discrepancies", 0)

            label = f"Reconciliation {scan_mode.upper()} completed: {healthy}/{total_audited} healthy, {discrepancies} discrepancies"
            self._append_log(task_id, label)
            self._finalize_task(
                task_id,
                "completed",
                label,
                summary,
                report,
            )

        except Exception as e:
            tb = traceback.format_exc()
            self._append_log(task_id, f"Fatal reconciliation exception: {e}\n{tb}")
            self._finalize_task(task_id, "failed", f"Reconciliation exception: {e}", {})
        finally:
            self._release(task_id)

    # -------------------------------------------------------------------------
    # 4. Retroactive Archive Peeker Sweep Worker
    # -------------------------------------------------------------------------

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_archive_peek_worker(self, task_id: int, params: Dict[str, Any]):
        self._append_log(task_id, "Archive Peek Backfill worker started.")

        def log_cb(msg: str):
            self._append_log(task_id, msg)

        def progress_cb(pct: float, label: str):
            self._update_progress(task_id, pct, label)

        def cancel_cb() -> bool:
            return self._cancelled(task_id)

        try:
            reconciler = VaultReconciler(self.db_path)
            model_ids = params.get("model_ids")
            report = reconciler.backfill_archive_peeks(
                model_ids=model_ids,
                progress_cb=progress_cb,
                log_cb=log_cb,
                cancel_cb=cancel_cb,
            )

            if self._cancelled(task_id):
                self._append_log(task_id, "Archive peek backfill cancelled by operator.")
                self._finalize_task(
                    task_id,
                    "cancelled",
                    "Archive peek backfill cancelled by operator",
                    report,
                    report,
                )
                return

            if not report.get("success", False):
                err = report.get("error", "Backfill error occurred")
                self._append_log(task_id, f"Archive peek backfill failed: {err}")
                self._finalize_task(
                    task_id,
                    "failed",
                    err,
                    report,
                    report,
                )
                return

            inspected = report.get("inspected", 0)
            total = report.get("total_archives", 0)
            submodels = report.get("submodels_created", 0)
            images = report.get("images_extracted", 0)

            label = f"Archive Peek complete: {inspected}/{total} archives ({submodels} submodels, {images} images)"
            self._append_log(task_id, label)
            self._finalize_task(
                task_id,
                "completed",
                label,
                report,
                report,
            )

        except Exception as e:
            tb = traceback.format_exc()
            self._append_log(task_id, f"Fatal archive peek backfill exception: {e}\n{tb}")
            self._finalize_task(task_id, "failed", f"Archive peek exception: {e}", {})
        finally:
            self._release(task_id)

    # -------------------------------------------------------------------------
    # 5. Background Slicer Stash Export Worker
    # -------------------------------------------------------------------------

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_stash_export_worker(self, task_id: int, params: Dict[str, Any]):
        stash_id = int(params.get("stash_id", 0))
        filter_mode = str(params.get("filter_mode", "all"))
        db_path_str = params.get("db_path")
        target_db = Path(db_path_str).resolve() if db_path_str else self.db_path
        self._append_log(task_id, f"Slicer Stash Export worker started for Stash #{stash_id} (Filter: {filter_mode.upper()}).")

        def log_cb(msg: str):
            self._append_log(task_id, msg)

        def progress_cb(pct: float, label: str):
            self._update_progress(task_id, pct, label)

        def cancel_cb() -> bool:
            return self._cancelled(task_id)

        try:
            from desync_archival.services.stash_exporter import StashExporter
            exporter = StashExporter(db_path=target_db)
            result = exporter.export_stash(
                stash_id=stash_id,
                filter_mode=filter_mode,
                progress_cb=progress_cb,
                log_cb=log_cb,
                cancel_cb=cancel_cb,
            )

            if self._cancelled(task_id):
                self._append_log(task_id, "Stash export cancelled by operator.")
                self._finalize_task(task_id, "cancelled", "Stash export cancelled by operator", result, result)
                return

            label = f"Export ready: {result['total_files']} files staged to {result['folder_name']}"
            self._append_log(task_id, label)
            self._finalize_task(task_id, "completed", label, result, result)

        except (InterruptedError, external.ExtractionStopped):
            self._append_log(task_id, "Stash export cancelled by operator.")
            self._finalize_task(task_id, "cancelled", "Stash export cancelled by operator", {})
        except Exception as e:
            tb = traceback.format_exc()
            self._append_log(task_id, f"Fatal stash export exception: {e}\n{tb}")
            self._finalize_task(task_id, "failed", f"Stash export error: {e}", {})
        finally:
            self._release(task_id)

    # -------------------------------------------------------------------------
    # 6. In-Place External Library Reconciler Worker (Phase 8)
    # -------------------------------------------------------------------------

    @job_logged(lambda self, task_id, *_: f"task-{task_id}")
    def _run_external_indexer_worker(self, task_id: int, params: Dict[str, Any]):
        library_id = params.get("library_id")
        lib_name = params.get("library_name") or f"Library #{library_id}"
        self._append_log(task_id, f"In-Place External Library Reconciler started for '{lib_name}' (ID: {library_id}).")

        def log_cb(msg: str):
            self._append_log(task_id, msg)

        def progress_cb(pct: float, label: str):
            self._update_progress(task_id, pct, label)

        def cancel_cb() -> bool:
            return self._cancelled(task_id)

        try:
            from desync_archival.services.external_indexer import (
                ADD,
                FULL,
                PREVIEW,
                ExternalLibraryIndexer,
            )
            indexer = ExternalLibraryIndexer(self.db_path)
            mode = params.get("mode", FULL)
            run_pass = {
                PREVIEW: indexer.preview_library,
                ADD: indexer.add_approved,
                FULL: indexer.rescan_known,
            }[mode]
            result = run_pass(int(library_id), progress_cb=progress_cb, log_cb=log_cb, cancel_cb=cancel_cb)

            # This job already has the NAS awake, and a scan only records printable files.
            if mode != PREVIEW and result.get("success") and not self._cancelled(task_id):
                self._inventory_library_pdfs(task_id, int(params["library_id"]))

            if self._cancelled(task_id):
                self._append_log(task_id, "Library crawl cancelled by operator.")
                self._finalize_task(
                    task_id,
                    "cancelled",
                    "Library scan cancelled by operator",
                    result,
                    result,
                )
                return

            if not result.get("success", False):
                err = result.get("error", "Library indexing failed")
                self._append_log(task_id, f"Library crawl failed: {err}")
                self._finalize_task(
                    task_id,
                    "failed",
                    err,
                    result,
                    result,
                )
                return

            if mode == PREVIEW:
                label = f"Preview ready: {result.get('new', 0)} new folders to review"
            elif mode == ADD:
                label = f"Added {result.get('models_added', 0)} models, {result.get('files_added', 0)} files"
            elif result.get("missing_held"):
                label = f"Rescan held: {result['missing_held']} folders look missing, check the mount"
            else:
                label = f"Rescan complete: {result.get('models_checked', 0)} models, {result.get('files_checked', 0)} files"
            self._append_log(task_id, label)
            self._finalize_task(task_id, "completed", label, result, result)

        except Exception as e:
            tb = traceback.format_exc()
            self._append_log(task_id, f"Fatal external indexer exception: {e}\n{tb}")
            self._finalize_task(task_id, "failed", f"Indexer error: {e}", {})
        finally:
            self._release(task_id)
