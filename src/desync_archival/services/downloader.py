"""
services/downloader.py - Decoupled background download manager for 3D digital assets.
Supports OAuth 2.0 Bearer authenticated downloads, live URL hydration, and dual-tier staging.
Instrumented with dynamic topic-based subsystem logging ('http', 'storage', 'archive').
"""

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

import httpx

from desync_archival import settings as app_config
from desync_archival.db.queries import downloader as queries
from desync_archival.services import auth_service, download_issues, identity, secret_store
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.db import scoped_connection
from desync_archival.services.logger import job_logged, log_layer, logger
from desync_archival.services.paths import (
    distinct_filename,
    sanitize_segment,
    stored_filename,
)

STREAM_CHUNK_SIZE_DEFAULT = 1024 * 1024       # 1 MB download buffer
OFFLOAD_CHUNK_SIZE_DEFAULT = 4 * 1024 * 1024  # 4 MB network transfer buffer


TOKEN_EXPIRY_MARGIN_SECONDS = 600
WORKER_ERROR_PAUSE_SECONDS = 5.0
# Idle workers wait this long for a wake, because asking SQLite every second opened a connection per worker.
IDLE_WAIT_SECONDS = 15.0
CREATOR_COLUMN = "COALESCE(NULLIF(m.designer_name, ''), NULLIF(m.designer_username, ''), 'Unknown_Creator') AS creator_name"
MONTH_COLUMN = "COALESCE(NULLIF(m.release_month, ''), 'Unassigned') AS release_month"
OFFLOAD_RETRY_BASE_SECONDS = 30
OFFLOAD_RETRY_MAX_SECONDS = 3600


def read_number(settings: dict[str, Any], key: str, default: Any, kind: Callable[[Any], Any]) -> Any:
    """Falls back with a warning, so one bad row can't stop the app starting."""
    raw = settings.get(key, default)
    try:
        value = kind(raw)
        if math.isfinite(value):
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    logger.warning(f"Setting {key} isn't a usable number, so {default} is used instead.")
    return default


def sha256_of(path: Path, chunk_size: int) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(block)
    return hasher.hexdigest()


def can_resume(status_code: int, content_range: str | None, resume_from: int) -> bool:
    """Only a 206 that starts exactly at the saved size can be appended. Anything else restarts."""
    if status_code != 206 or not resume_from or not content_range:
        return False
    match = re.match(r"bytes (\d+)-", content_range)
    return bool(match) and int(match.group(1)) == resume_from


class StorageUnavailableError(Exception):
    pass


# -----------------------------------------------------------------------------
# Path Sanitization & Validation Helpers
# -----------------------------------------------------------------------------

def queue_models(conn: sqlite3.Connection, download_manager: Any, model_ids: list[int]) -> int:
    queued = 0
    with conn:
        for model_id in model_ids:
            files, size = conn.execute(queries.COUNT_MODEL_FILES_FOR_QUEUE, (model_id,)).fetchone()
            # Check first: the update changes nothing for a live job, and that mustn't read as "no job yet".
            if conn.execute(queries.SELECT_DOWNLOAD_JOB_BY_MODEL, (model_id,)).fetchone():
                queued += conn.execute(queries.UPDATE_DOWNLOAD_JOBS_FOR_QUEUE, (files or 0, size or 0, model_id)).rowcount
            else:
                conn.execute(queries.INSERT_DOWNLOAD_JOBS_FOR_QUEUE, (model_id, files or 0, size or 0))
                queued += 1
    if download_manager:
        if not download_manager.is_running:
            download_manager.start()
        elif download_manager.is_paused:
            download_manager.resume()
        download_manager.wake()
    return queued


def remove_waiting_jobs(conn: sqlite3.Connection, model_ids: list[int]) -> int:
    """Takes models off the queue, leaving live and completed jobs. Returns how many went."""
    if not model_ids:
        return 0
    marks = ",".join("?" for _ in model_ids)
    return conn.execute(queries.DELETE_WAITING_JOBS_BY_MODEL.format(marks=marks), model_ids).rowcount


class StorageManager:
    """Resolves storage paths and caches disk metrics, so polling doesn't stat the NAS."""
    # The UI asks every few seconds, and a dropped mount showing for this long does no harm.
    METRICS_SECONDS = 30.0

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self._metrics: tuple[float, dict[str, Any]] | None = None

    def _get_setting(self, key: str) -> str:
        with scoped_connection(self.db_path) as conn:
            row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY, (key,)).fetchone()
            return row[0] if row and row[0] is not None else ""

    def configured_root(self) -> Path:
        """Returns the vault root as stored, without touching it.

        Raises:
            StorageUnavailableError: The vault is unconfigured.
        """
        val = self._get_setting("download_root_path")
        if not val:
            raise StorageUnavailableError("NAS target storage path is unconfigured.")
        return Path(val).expanduser()

    def get_root_path(self) -> Path:
        """Returns the vault root, or raises if it can't be reached right now.

        Raises:
            StorageUnavailableError: The vault is unconfigured or unreachable.
        """
        p = self.configured_root().resolve()
        if not p.exists() or not p.is_dir():
            raise StorageUnavailableError(f"NAS storage path is unmounted or unreachable: {p}")
        return p

    def get_work_path(self) -> Path:
        val = self._get_setting("download_work_path")
        if not val:
            return app_config.staging_dir().resolve()
        p = Path(val).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_disk_metrics(self) -> dict[str, Any]:
        if self._metrics and time.monotonic() - self._metrics[0] < self.METRICS_SECONDS:
            return self._metrics[1]
        metrics = self._read_disk_metrics()
        self._metrics = (time.monotonic(), metrics)
        return metrics

    def _read_disk_metrics(self) -> dict[str, Any]:
        metrics = {
            "nas": {"configured": False, "mounted": False, "total_bytes": 0, "free_bytes": 0, "path": "", "error": None},
            "local": {"configured": False, "mounted": False, "total_bytes": 0, "free_bytes": 0, "path": "", "error": None},
        }
        try:
            nas_p = self.get_root_path()
            usage = shutil.disk_usage(nas_p)
            metrics["nas"] = {"configured": True, "mounted": True, "total_bytes": usage.total, "free_bytes": usage.free, "path": str(nas_p), "error": None}
        except Exception as e:
            metrics["nas"]["error"] = str(e)

        try:
            local_p = self.get_work_path()
            usage = shutil.disk_usage(local_p)
            metrics["local"] = {"configured": True, "mounted": True, "total_bytes": usage.total, "free_bytes": usage.free, "path": str(local_p), "error": None}
        except Exception as e:
            metrics["local"]["error"] = str(e)

        return metrics


class FileProblem(Exception):
    """One file went wrong. The job carries on with the rest, and this file goes to Download Issues.

    Anything else raised while staging stops the whole job: a sign-in that expired, a Cloudflare
    check, a dropped connection or a full disk would fail every file after it the same way.
    """

    def __init__(self, message: str, http_status: int | None = None, retry: bool = True):
        super().__init__(message)
        self.http_status = http_status
        self.retry = retry


class DeadDownloadLink(FileProblem):
    """A download link answered 404 or 410, so MMF is asked whether it has a newer one."""


@dataclass
class Fetched:
    """What one try delivered, before anything decides whether to keep it."""

    size: int
    content_type: str
    sent_name: str
    page: bool

    def described(self) -> str:
        kind = self.content_type.split(";")[0].strip() or "no stated type"
        return f"{kind}, named {self.sent_name}" if self.sent_name else f"{kind}, with no name given"


class SizeDiffers(FileProblem):
    def __init__(self, filename: str, expected: int, fetched: Fetched, page: bool):
        what = "a web page, not the file" if page else f"sent as {fetched.described()}"
        super().__init__(f"Size mismatch on {filename}: expected {expected}, got {fetched.size} ({what})")
        self.fetched = fetched
        self.received = fetched.size
        self.page = page


# A Cloudflare challenge or error page arrives as a 200. It's never kept, whatever its size, unless
# the file itself is a web page.
PAGE_SUFFIXES = (".html", ".htm")
PAGE_STARTS = (b"<!doctype html", b"<html")
NAMES_IN_SUMMARY = 3


def looks_like_page(path: Path, content_type: str) -> bool:
    if content_type.split(";")[0].strip().lower() == "text/html":
        return True
    with open(path, "rb") as handle:
        head = handle.read(512).lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(PAGE_STARTS)


def disposition_name(header: str) -> str:
    """The filename a server gave in Content-Disposition, or an empty string."""
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", header, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def check_zip(path: Path, name: str) -> None:
    """Raises FileProblem for a damaged ZIP, and removes it so a retry starts clean."""
    if path.suffix.lower() != ".zip" and Path(name).suffix.lower() != ".zip":
        return
    try:
        with zipfile.ZipFile(path, "r") as zf:
            bad_file = zf.testzip()
            if bad_file:
                raise zipfile.BadZipFile(f"Corrupt entry in archive: {bad_file}")
    except Exception as e:
        path.unlink(missing_ok=True)
        log_layer("archive", logging.ERROR, f"ZIP CRC failed on {name}: {e}")
        raise FileProblem(f"ZIP validation failed for {name}: {e}") from e


def nothing_downloaded(issues: list[dict[str, Any]]) -> str:
    named = "; ".join(f"{issue['filename']}: {issue['detail']}" for issue in issues[:NAMES_IN_SUMMARY])
    more = f"; and {len(issues) - NAMES_IN_SUMMARY} more" if len(issues) > NAMES_IN_SUMMARY else ""
    return f"No file could be downloaded. {named}{more}"


def job_attempt(job: sqlite3.Row) -> int:
    """Which run of the job this is, so History's attempts don't all read 1."""
    return (job["retry_count"] or 0) + 1


def model_folder(creator: str, month: str, external_key: Any, model_id: int, name: str | None) -> Path:
    """A model's folder under the vault root. Staging, offload and uploads must all agree on it."""
    folder = f"{external_key or model_id}_{sanitize_segment(name or f'Model_{model_id}')}"
    return Path(sanitize_segment(creator)) / sanitize_segment(month) / folder


def write_model_metadata_json(conn: sqlite3.Connection, model_id: int, target_dir: Path) -> None:
    """Dumps formatted metadata.json directly into the object directory on the NAS."""
    model_row = conn.execute(queries.SELECT_COLLECTIONS_BY_ID_V2, (model_id,)).fetchone()
    raw_payload = None

    if model_row and model_row["raw_backup_path"]:
        p = app_config.resolve_data_path(model_row["raw_backup_path"])
        if p.exists() and p.is_file():
            try:
                raw_payload = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                raw_payload = None

    if not raw_payload:
        fallback = app_config.metadata_dir() / f"model_{model_row['external_key']}.json" if model_row else Path()
        if fallback.is_file():
            try:
                raw_payload = json.loads(fallback.read_text(encoding="utf-8"))
            except Exception:
                raw_payload = None

    target_file = target_dir / "metadata.json"
    if raw_payload:
        target_file.write_text(json.dumps(raw_payload, indent=2), encoding="utf-8")
    elif model_row:
        meta = dict(model_row)
        meta["tags"] = [r[0] for r in conn.execute(queries.SELECT_COLLECTION_TAGS_TAG_BY_MODEL_ID, (model_id,)).fetchall()]
        target_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")


class StagedFileMissing(FileNotFoundError):
    """A staged file a job needs is gone, so no retry can finish the job."""


class DownloadManager:
    # The Active view polls every second, so a 4,000 model queue can't go in the answer each time.
    QUEUED_JOBS_PAGE = 200

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.storage = StorageManager(db_path)
        self.is_running = False
        self.is_paused = False
        self._lock = threading.Lock()

        self.workers: List[threading.Thread] = []
        self.offloader_workers: List[threading.Thread] = []
        self._downloads_ready = threading.Event()
        self._offloads_ready = threading.Event()
        self._offload_not_before: dict[int, float] = {}

        self.active_downloads: Dict[int, Dict[str, Any]] = {}
        self.active_offloads: Dict[int, Dict[str, Any]] = {}

        self.worker_concurrency = 2
        self.offloader_concurrency = 1
        self.batch_size = 5
        self.batch_cooldown_seconds = 3.0
        self.stream_chunk_size = STREAM_CHUNK_SIZE_DEFAULT
        self.offload_chunk_size = OFFLOAD_CHUNK_SIZE_DEFAULT
        self.cookies: Dict[str, str] = {}
        self.access_token: str = ""
        self.token_expires_at: int = 0

        self._load_settings()

    def _get_db(self) -> sqlite3.Connection:
        return scoped_connection(self.db_path)

    def needs_reconnect(self) -> bool:
        """True when the token is about to expire, because only a new sign-in replaces it."""
        if not self.access_token or not self.token_expires_at:
            return False
        return time.time() >= self.token_expires_at - TOKEN_EXPIRY_MARGIN_SECONDS

    def _load_settings(self):
        with self._get_db() as conn:
            rows = conn.execute(queries.SELECT_APP_SETTINGS).fetchall()
            settings = {r["key"]: r["value"] for r in rows}

            self.worker_concurrency = max(1, min(16, read_number(settings, "worker_concurrency", 2, int)))
            self.offloader_concurrency = max(1, min(8, read_number(settings, "offloader_concurrency", 1, int)))
            self.batch_size = max(1, min(100, read_number(settings, "batch_size", 5, int)))
            self.batch_cooldown_seconds = max(0.0, read_number(settings, "batch_cooldown_seconds", 3.0, float))

            stream_mb = read_number(settings, "stream_chunk_size_mb", 1.0, float)
            self.stream_chunk_size = max(64 * 1024, int(stream_mb * 1024 * 1024))

            offload_mb = read_number(settings, "offload_chunk_size_mb", 4.0, float)
            self.offload_chunk_size = max(256 * 1024, int(offload_mb * 1024 * 1024))

            secret_store.overlay(settings)
            self.access_token = settings.get("mmf_access_token", "").strip()
            try:
                self.token_expires_at = int(settings.get("mmf_token_expires_at") or 0)
            except ValueError:
                self.token_expires_at = 0

            raw_cookies = settings.get("cookies_json", "{}").strip()
            try:
                parsed = json.loads(raw_cookies)
                if isinstance(parsed, list):
                    self.cookies = {c["name"]: c["value"] for c in parsed if "name" in c and "value" in c}
                elif isinstance(parsed, dict):
                    self.cookies = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                self.cookies = {}

        log_layer("storage", logging.DEBUG, f"Engine settings loaded: Workers={self.worker_concurrency}, Offloaders={self.offloader_concurrency}, OAuth={'YES' if self.access_token else 'NO'}")

    def is_circuit_breaker_tripped(self) -> bool:
        """Reads the breaker from the database, so it survives restarts."""
        with self._get_db() as conn:
            row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY_V2).fetchone()
            return bool(row and row["value"] == "1")

    def trip_circuit_breaker(self, reason: str):
        """Holds all downloads after an auth failure, so a bad token can't hammer MMF."""
        with self._get_db() as conn:
            conn.execute(queries.INSERT_APP_SETTINGS)
            conn.execute(queries.INSERT_APP_SETTINGS_V2, (reason,))
        logger.error(f"[Circuit Breaker] TRIPPED: {reason}")

    def reset_circuit_breaker(self):
        with self._get_db() as conn:
            conn.execute(queries.UPDATE_APP_SETTINGS_VALUE_BY_KEY_V2)
            conn.execute(queries.UPDATE_APP_SETTINGS_VALUE_BY_KEY_V3)
        logger.info("[Circuit Breaker] Reset by operator.")

    def start(self):
        """Starts the worker threads. Does nothing if already running."""
        if self.is_running:
            return
        self._load_settings()
        self.is_running = True
        self.is_paused = False

        self.workers = []
        for i in range(self.worker_concurrency):
            t = threading.Thread(target=self._download_worker_loop, args=(i + 1,), daemon=True)
            self.workers.append(t)
            t.start()

        self.offloader_workers = []
        for i in range(self.offloader_concurrency):
            t = threading.Thread(target=self._offload_worker_loop, args=(i + 1,), daemon=True)
            self.offloader_workers.append(t)
            t.start()

        auth_mode = "OAuth Bearer Token" if self.access_token else "Cookie Fallback"
        logger.info(f"[DownloadManager] Started {self.worker_concurrency} download workers ({auth_mode}) and {self.offloader_concurrency} NAS offloaders.")

    def stop(self):
        self.is_running = False
        self.wake()

    def wake(self) -> None:
        """Tells idle workers there's a job, so they don't have to keep asking SQLite."""
        self._downloads_ready.set()
        self._offloads_ready.set()

    @staticmethod
    def _wait_for_work(ready: threading.Event) -> None:
        # The timeout catches jobs made by code that can't reach this manager, such as a reconcile reset.
        ready.wait(IDLE_WAIT_SECONDS)
        ready.clear()

    def pause(self):
        """Stops workers claiming new jobs. Transfers in progress run on."""
        self.is_paused = True
        logger.info("[DownloadManager] Engine paused by operator.")

    def resume(self):
        """Reloads settings and lets workers claim jobs again."""
        self._load_settings()
        self.is_paused = False
        self.wake()
        logger.info("[DownloadManager] Engine resumed by operator.")

    def _claim_next_download_job(self) -> Optional[sqlite3.Row]:
        with self._get_db() as conn:
            query = f"""
                SELECT dj.*, m.name AS model_name, m.external_key, {CREATOR_COLUMN}, {MONTH_COLUMN}
                FROM download_jobs dj
                JOIN collections m ON dj.model_id = m.id
                WHERE dj.status = 'pending'
                ORDER BY dj.enqueued_at ASC LIMIT 1;
            """
            row = conn.execute(query).fetchone()

            # The status check makes this atomic: if two workers read one row, only one updates it.
            if row and conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID, (row["id"],)).rowcount == 1:
                return row
        return None

    def _download_worker_loop(self, worker_id: int):
        while self.is_running:
            # An error that ended this thread would stall the queue until a restart, with is_running still True.
            try:
                self._download_next(worker_id)
            except Exception:
                logger.exception(f"[DownloadManager] Download worker #{worker_id} hit an error and will retry")
                time.sleep(WORKER_ERROR_PAUSE_SECONDS)

    def _download_next(self, worker_id: int) -> None:
        if self.is_paused or self.is_circuit_breaker_tripped() or self.needs_reconnect():
            time.sleep(1.0)
            return

        try:
            work_dir = self.storage.get_work_path()
        except StorageUnavailableError:
            time.sleep(2.0)
            return

        job = self._claim_next_download_job()
        if not job:
            self._wait_for_work(self._downloads_ready)
            return

        model_id = job["model_id"]
        log_layer("storage", logging.DEBUG, f"Worker #{worker_id}: Claimed Model #{model_id} ('{job['model_name']}')")

        with self._lock:
            self.active_downloads[worker_id] = {
                "model_id": model_id,
                "model_name": job["model_name"] or f"Model #{model_id}",
                "creator": job["creator_name"] or "Unknown",
                "status": "downloading",
                "speed_bps": 0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "current_file": "Initializing",
            }

        try:
            self._process_download_to_staging(worker_id, job, work_dir)
        except Exception as e:
            self._handle_download_failure(job, str(e))
        finally:
            with self._lock:
                self.active_downloads.pop(worker_id, None)

        time.sleep(self.batch_cooldown_seconds)

    @job_logged(lambda self, worker_id, job, *_: f"download-{job['id']}")
    def _process_download_to_staging(self, worker_id: int, job: sqlite3.Row, work_dir: Path):
        # Links don't change once issued: stored and live matched on every file checked, so asking MMF
        # before each model only added a request. It's asked when a link is missing or has died.
        fresh = self._links_missing(job["model_id"])
        if fresh:
            self._refresh_links(worker_id, job)
        self._stage_files(worker_id, job, work_dir, links_fresh=fresh)

    def _links_missing(self, model_id: int) -> bool:
        with self._get_db() as conn:
            rows = conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID, (model_id,)).fetchall()
        return not rows or any(not row["download_url"] for row in rows)

    def _current_links(self, model_id: int) -> dict[int, str]:
        with self._get_db() as conn:
            return {row["file_id"]: row["download_url"] for row in conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID, (model_id,))}

    def _refresh_links(self, worker_id: int, job: sqlite3.Row) -> bool:
        """Asks MMF's API for current links, as a plain client. Returns whether a stored one changed."""
        model_id = job["model_id"]
        mmf_id = job["external_key"]
        if not self.access_token or not mmf_id:
            return False
        api_url = f"https://www.myminifactory.com/api/v2/objects/{mmf_id}"
        auth_headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        changed = False
        try:
            log_layer("http", logging.DEBUG, f"Worker #{worker_id}: Checking download links for Model #{model_id}")
            with httpx.Client(timeout=15.0, follow_redirects=True) as meta_client:
                resp = meta_client.get(api_url, headers=auth_headers)
            if resp.status_code == 401:
                self.trip_circuit_breaker("MyMiniFactory OAuth token expired (HTTP 401). Please re-authenticate in settings.")
                raise RuntimeError("OAuth token expired (HTTP 401)")
            if resp.status_code != 200:
                return False
            obj_data = resp.json()
            archive_dl_url = obj_data.get("archive_download_url")
            remote_files = (
                obj_data.get("files", {}).get("items", [])
                if isinstance(obj_data.get("files"), dict)
                else (obj_data.get("files") or [])
            )
            with self._get_db() as conn:
                with conn:
                    stored = {row["external_key"]: row["download_url"] for row in conn.execute(queries.SELECT_MODEL_FILE_LINKS, (model_id,))}
                    if archive_dl_url:
                        conn.execute(queries.UPDATE_COLLECTIONS_ARCHIVE_DOWNLOAD_URL_BY_ID, (archive_dl_url, model_id))
                    for rf in remote_files:
                        rf_id, rf_url = rf.get("id"), rf.get("download_url")
                        if rf_id and rf_url:
                            changed = changed or stored.get(str(rf_id)) != rf_url
                            conn.execute(queries.UPDATE_MODEL_FILES_DOWNLOAD_URL_BY_SOURCE, (rf_url, rf.get("size"), str(rf_id)))
            log_layer("http", logging.DEBUG, f"Worker #{worker_id}: Download links checked for Model #{model_id}")
        except httpx.RequestError as req_err:
            log_layer("http", logging.WARNING, f"Worker #{worker_id}: Could not check download links for Model #{model_id}: {req_err}")
        return changed

    def _files_for(self, job: sqlite3.Row) -> list[sqlite3.Row]:
        model_id = job["model_id"]
        mmf_id = job["external_key"]
        with self._get_db() as conn:
            files = conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID, (model_id,)).fetchall()

            # Fallback: Adopt archive_download_url as synthetic ZIP file if no separate file items exist
            if not files:
                m_row = conn.execute(queries.SELECT_COLLECTIONS_BY_ID, (model_id,)).fetchone()
                if m_row and m_row["archive_download_url"] and mmf_id:
                    clean_m = sanitize_segment(m_row["name"] or f"Model_{model_id}")
                    archive_filename = f"{mmf_id}_{clean_m}.zip"
                    with conn:
                        conn.execute(queries.INSERT_MODEL_FILES, ("mmf", identity.archive_file_key(mmf_id), model_id, archive_filename, m_row["archive_download_url"]))
                    files = conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID_V2, (model_id,)).fetchall()
                    log_layer("archive", logging.INFO, f"Adopted archive_download_url as synthetic bundle '{archive_filename}' for Model #{model_id}")

        if not files:
            raise RuntimeError(f"No file metadata recorded for Model ID {model_id}")
        return files

    def _show_progress(self, worker_id: int, **values: Any) -> None:
        with self._lock:
            if worker_id in self.active_downloads:
                self.active_downloads[worker_id].update(values)

    def _log_attempt(self, job: sqlite3.Row, file_id: int, status: str, detail: str, http_status: int | None = None) -> None:
        with self._get_db() as conn:
            conn.execute(queries.INSERT_FILE_ATTEMPT, (job["model_id"], file_id, job_attempt(job), status, http_status, detail))

    def _stage_files(self, worker_id: int, job: sqlite3.Row, work_dir: Path, links_fresh: bool = False):
        model_id = job["model_id"]
        files = self._files_for(job)

        in_flight_dir = work_dir / "in_flight"
        in_flight_dir.mkdir(parents=True, exist_ok=True)
        staged_model_dir = work_dir / "staged" / str(model_id)
        staged_model_dir.mkdir(parents=True, exist_ok=True)
        folder = model_folder(job["creator_name"], job["release_month"], job["external_key"], model_id, job["model_name"])

        total_bytes = sum(f["size_bytes"] or 0 for f in files)
        done_bytes = 0
        staged_files_info = []
        issues: list[dict[str, Any]] = []
        taken_names: set[str] = set()
        links = {f["file_id"]: f["download_url"] for f in files}
        self._show_progress(worker_id, total_bytes=total_bytes)

        # NOTE: the one disguised MMF request left, kept by the owner's decision on 2026-11-01. File bytes
        # sit on the website behind a Cloudflare challenge, which a plain client with a valid token
        # can't pass, and the API has no route to them (S-15, E50). Remove this when MMF offers one.
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.myminifactory.com/library",
        }

        with httpx.Client(headers=headers, cookies=auth_service.mmf_cookies(self.cookies), timeout=30.0, follow_redirects=True) as client:
            for file_row in files:
                file_id = file_row["file_id"]
                filename = file_row["filename"]
                expected_size = file_row["size_bytes"] or 0
                cleaned_name = distinct_filename(stored_filename(filename), file_id, taken_names)
                part_path = in_flight_dir / f"{model_id}_{file_id}.part"
                staged_path = staged_model_dir / cleaned_name
                self._show_progress(worker_id, current_file=cleaned_name)

                # Each file gets one more try. A second try that brings the same wrong size is MMF's
                # listing being wrong, not a bad transfer: Duncan Shadow's files did this every time.
                first_size: int | None = None
                kept: Fetched | None = None
                problem: FileProblem | None = None
                for try_number in (1, 2):
                    last_try = try_number == 2
                    try:
                        fetched = self._fetch_file(worker_id, client, links[file_id], file_id, part_path, done_bytes)
                        page = fetched.page and Path(filename).suffix.lower() not in PAGE_SUFFIXES
                        if (expected_size and fetched.size != expected_size) or page:
                            if not last_try or page or fetched.size != first_size:
                                raise SizeDiffers(filename, expected_size, fetched, page)
                            kept = fetched
                        check_zip(part_path, cleaned_name)
                        problem = None
                        break
                    except DeadDownloadLink as e:
                        problem = e
                        retry = not last_try and not links_fresh and self._refresh_links(worker_id, job)
                        if retry:
                            links_fresh = True
                            links.update(self._current_links(model_id))
                            log_layer("http", logging.INFO, f"Worker #{worker_id}: {cleaned_name} has a new link. Trying once more.")
                    except SizeDiffers as e:
                        problem = e
                        retry = not last_try
                        part_path.unlink(missing_ok=True)
                        first_size = None if e.page else e.fetched.size
                    except FileProblem as e:
                        problem = e
                        retry = not last_try and e.retry
                    self._log_attempt(job, file_id, "retrying" if retry else "failed", str(problem), problem.http_status)
                    if not retry:
                        break

                if problem:
                    log_layer("storage", logging.WARNING, f"Worker #{worker_id}: {cleaned_name} needs a manual download: {problem}")
                    issues.append({
                        "file_id": file_id, "filename": filename, "kind": download_issues.NOT_DOWNLOADED,
                        "listed": expected_size or None, "received": getattr(problem, "received", None), "detail": str(problem),
                    })
                    continue

                final_size = part_path.stat().st_size
                file_sha256 = sha256_of(part_path, self.stream_chunk_size)
                log_layer("storage", logging.DEBUG, f"Worker #{worker_id}: In-flight buffer finished for {cleaned_name} (SHA-256: {file_sha256[:10]}...)")
                if kept:
                    detail = (
                        f"MMF lists {expected_size:,} bytes for {filename}, and both tries delivered {final_size:,} bytes. "
                        f"The file was kept. MMF sent it as {kept.described()}."
                    )
                    log_layer("storage", logging.WARNING, f"Worker #{worker_id}: {detail}")
                    self._log_attempt(job, file_id, "kept", detail)
                    issues.append({
                        "file_id": file_id, "filename": filename, "kind": download_issues.SIZE_DIFFERS,
                        "listed": expected_size, "received": final_size, "detail": detail,
                    })

                shutil.move(str(part_path), str(staged_path))
                self._peek(worker_id, model_id, file_id, staged_path, cleaned_name)
                done_bytes += final_size

                staged_files_info.append({
                    "file_id": file_id,
                    "original_filename": filename,
                    "cleaned_filename": cleaned_name,
                    "local_relative_path": str(folder / "files" / cleaned_name),
                    "expected_bytes": expected_size,
                    "actual_bytes": final_size,
                    "file_extension": Path(cleaned_name).suffix.lower(),
                    "staging_path": str(staged_path),
                    "sha256": file_sha256,
                    "clean": not kept,
                })

        with self._get_db() as conn:
            with conn:
                for issue in issues:
                    download_issues.record(
                        conn, issue["file_id"], model_id, issue["kind"], issue["listed"], issue["received"], issue["detail"],
                    )
                for s in staged_files_info:
                    if s["clean"]:
                        download_issues.resolve(conn, s["file_id"], download_issues.DOWNLOADED)
                    updated = conn.execute(queries.UPDATE_DOWNLOADED_FILES_STORAGE_TIER_BY_FILE_ID, (s["staging_path"], s["local_relative_path"], s["actual_bytes"], s["sha256"], s["file_id"]))
                    if updated.rowcount:
                        continue
                    conn.execute(queries.INSERT_DOWNLOADED_FILES, (
                        s["file_id"], model_id, s["original_filename"], s["cleaned_filename"],
                        s["local_relative_path"], s["expected_bytes"], s["actual_bytes"],
                        s["file_extension"], s["staging_path"], s["sha256"]
                    ))

                if staged_files_info:
                    conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V2, (len(staged_files_info), done_bytes, total_bytes, job["id"]))

        if not staged_files_info:
            raise RuntimeError(nothing_downloaded(issues))

        flagged = f", {len(issues)} need attention in Download Issues" if issues else ""
        log_layer("storage", logging.INFO, f"Worker #{worker_id}: Model #{model_id} staging complete ({len(staged_files_info)} files, {done_bytes} bytes{flagged}). Queued for NAS offload.")
        self._offloads_ready.set()

    def _fetch_file(
        self, worker_id: int, client: httpx.Client, dl_url: str | None, file_id: int, part_path: Path, done_bytes: int,
    ) -> "Fetched":
        """Streams one file into its .part and says what arrived. A full transfer of the wrong thing still returns."""
        if not dl_url:
            raise FileProblem(f"No download link recorded for file #{file_id}", retry=False)
        # The URL comes from MMF's response, so it decides here whether the token may go along.
        if urlsplit(dl_url).scheme != "https":
            raise FileProblem(f"Refusing a download URL that isn't https for file #{file_id}", retry=False)

        resume_from = part_path.stat().st_size if part_path.exists() else 0
        request_headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
        if self.access_token and auth_service.is_mmf_url(dl_url):
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        log_layer("http", logging.DEBUG, f"Worker #{worker_id}: Streaming file #{file_id}")

        t0 = time.time()
        received = 0
        with client.stream("GET", dl_url, headers=request_headers) as resp:
            if resp.status_code in (404, 410):
                raise DeadDownloadLink(f"HTTP {resp.status_code} downloading file {file_id}", http_status=resp.status_code)
            if resp.status_code in (401, 403):
                challenged = resp.status_code == 403 and resp.headers.get("cf-mitigated")
                reason = (
                    f"Cloudflare asked for a browser check on file {file_id}. Paste fresh session cookies, then resume."
                    if challenged else
                    f"MyMiniFactory OAuth token expired (HTTP {resp.status_code}) on file {file_id}"
                    if self.access_token else
                    f"Cloudflare/MMF Session expired (HTTP {resp.status_code}) on file {file_id}"
                )
                self.trip_circuit_breaker(reason)
                raise RuntimeError(f"Authentication failure (HTTP {resp.status_code})")
            if resp.status_code == 416:
                part_path.unlink(missing_ok=True)
                raise FileProblem(f"Saved partial file for {file_id} was refused. Restarting it.", http_status=416)
            if resp.status_code not in (200, 206):
                raise FileProblem(f"HTTP {resp.status_code} downloading file {file_id}", http_status=resp.status_code)
            # Host only, because the full URL is signed. This shows where MMF's downloads really go.
            if resp.url.host != urlsplit(dl_url).hostname:
                log_layer("http", logging.INFO, f"Worker #{worker_id}: file #{file_id} redirected to {resp.url.host}")

            resuming = can_resume(resp.status_code, resp.headers.get("Content-Range"), resume_from)
            if resuming:
                log_layer("http", logging.INFO, f"Resuming file #{file_id} from byte {resume_from}")
            already = resume_from if resuming else 0
            with open(part_path, "ab" if resuming else "wb") as f_out:
                for chunk in resp.iter_bytes(chunk_size=self.stream_chunk_size):
                    if not chunk:
                        continue
                    f_out.write(chunk)
                    received += len(chunk)
                    speed = int(received / max(0.001, time.time() - t0))
                    self._show_progress(worker_id, downloaded_bytes=done_bytes + already + received, speed_bps=speed)
            content_type = resp.headers.get("content-type", "")
            sent_name = disposition_name(resp.headers.get("content-disposition", ""))

        # A dropped connection raises above and keeps its .part for the next attempt.
        return Fetched(part_path.stat().st_size, content_type, sent_name, looks_like_page(part_path, content_type))

    def _peek(self, worker_id: int, model_id: int, file_id: int, staged_path: Path, cleaned_name: str) -> None:
        """Indexes an archive's contents. A failure here never costs the download."""
        if staged_path.suffix.lower() not in (".zip", ".rar", ".7z"):
            return
        try:
            peeker = ArchivePeeker(self.db_path)
            peek_res = peeker.inspect_and_record(model_id, file_id, staged_path)
            if not peek_res.get("success"):
                warn_msg = f"Archive peaking notice for file {file_id}: {peek_res.get('error')}"
                log_layer("archive", logging.WARNING, f"Worker #{worker_id}: {warn_msg}")
                with self._get_db() as conn:
                    conn.execute(queries.INSERT_DOWNLOAD_AUDIT_LOG_V7, (model_id, warn_msg))
            else:
                log_layer(
                    "archive",
                    logging.INFO,
                    f"Worker #{worker_id}: Peaked into {cleaned_name} - Indexed {peek_res.get('submodels_count')} submodels, extracted {peek_res.get('images_extracted')} previews."
                )
        except Exception as pe:
            warn_msg = f"Archive peaking failed for {cleaned_name}: {pe}"
            log_layer("archive", logging.WARNING, f"Worker #{worker_id}: {warn_msg}")
            try:
                with self._get_db() as conn:
                    conn.execute(queries.INSERT_DOWNLOAD_AUDIT_LOG_V7, (model_id, warn_msg))
            except Exception:
                pass

    def _handle_download_failure(self, job: sqlite3.Row, error_msg: str):
        job_id, model_id = job["id"], job["model_id"]
        logger.error(f"[DownloadManager] Job #{job_id} (Model #{model_id}) failed: {error_msg}")
        with self._get_db() as conn:
            conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V3, (error_msg, job_id))

            conn.execute(queries.INSERT_DOWNLOAD_AUDIT_LOG_V8, (model_id, job_attempt(job), error_msg))

    def _claim_next_offload_job(self) -> Optional[sqlite3.Row]:
        now = time.time()
        with self._lock:
            waiting = [job_id for job_id, due in self._offload_not_before.items() if due > now]
        marks = ",".join("?" * len(waiting))
        with self._get_db() as conn:
            row = conn.execute(f"""
                SELECT dj.*, m.name as model_name, m.external_key, {CREATOR_COLUMN}, {MONTH_COLUMN}
                FROM download_jobs dj
                JOIN collections m ON dj.model_id = m.id
                WHERE dj.status = 'staging_complete' AND dj.id NOT IN ({marks})
                ORDER BY dj.enqueued_at ASC LIMIT 1;
            """, waiting).fetchone()

            if row and conn.execute(
                queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V4,
                (row["id"],),
            ).rowcount == 1:
                with self._lock:
                    self._offload_not_before.pop(row["id"], None)
                return row
        return None

    def _offload_worker_loop(self, worker_id: int):
        while self.is_running:
            try:
                self._offload_next(worker_id)
            except Exception:
                logger.exception(f"[DownloadManager] Offload worker #{worker_id} hit an error and will retry")
                time.sleep(WORKER_ERROR_PAUSE_SECONDS)

    def _offload_next(self, worker_id: int) -> None:
        if self.is_paused:
            time.sleep(1.0)
            return

        try:
            nas_root = self.storage.get_root_path()
        except StorageUnavailableError:
            time.sleep(3.0)
            return

        job = self._claim_next_offload_job()
        if not job:
            self._wait_for_work(self._offloads_ready)
            return

        model_id = job["model_id"]
        log_layer("storage", logging.DEBUG, f"Offloader #{worker_id}: Claimed Model #{model_id} for NAS transfer")

        with self._lock:
            self.active_offloads[worker_id] = {
                "model_id": model_id,
                "model_name": job["model_name"] or f"Model #{model_id}",
                "status": "offloading",
                "speed_bps": 0,
                "transferred_bytes": 0,
                "total_bytes": job["downloaded_bytes"] or 0,
                "current_file": "Initializing",
            }

        try:
            self._process_nas_offload(worker_id, job, nas_root)
        except StagedFileMissing as e:
            # Only unfinished files are selected here, so waiting can't help. A job stuck in
            # staging_complete would also hold every destructive action behind busy_reason.
            log_layer("storage", logging.ERROR, f"Offloader #{worker_id}: Model #{model_id} can't finish: {e}")
            with self._get_db() as conn:
                conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V3, (f"NAS Offload Failed: {e}", job["id"]))
        except Exception as e:
            log_layer("storage", logging.ERROR, f"Offloader #{worker_id}: Transfer failed for Model #{model_id}: {e}")
            self._delay_offload(job)
            with self._get_db() as conn:
                conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V5, (f"NAS Offload Delayed: {e}", job["id"]))
        finally:
            with self._lock:
                self.active_offloads.pop(worker_id, None)

        time.sleep(0.5)

    def _delay_offload(self, job: sqlite3.Row) -> None:
        # The job has to stay in staging_complete, because its staged files are the only copy and the
        # queue routes can delete a failed job. Without a wait it would rewrite every file in a loop.
        delay = min(OFFLOAD_RETRY_MAX_SECONDS, OFFLOAD_RETRY_BASE_SECONDS * 2 ** min(job["retry_count"] or 0, 8))
        with self._lock:
            self._offload_not_before[job["id"]] = time.time() + delay

    def worker_counts(self) -> dict[str, int]:
        threads = [*self.workers, *self.offloader_workers] if self.is_running else []
        return {"running": sum(thread.is_alive() for thread in threads), "expected": len(threads)}

    def _write_model_metadata_json(self, conn: sqlite3.Connection, model_id: int, target_dir: Path):
        write_model_metadata_json(conn, model_id, target_dir)

    @job_logged(lambda self, worker_id, job, *_: f"offload-{job['id']}")
    def _process_nas_offload(self, worker_id: int, job: sqlite3.Row, nas_root: Path):
        model_id = job["model_id"]
        folder = model_folder(job["creator_name"], job["release_month"], job["external_key"], model_id, job["model_name"])

        model_dest_dir = nas_root / folder
        files_dest_dir = model_dest_dir / "files"
        files_dest_dir.mkdir(parents=True, exist_ok=True)

        with self._get_db() as conn:
            records = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_MODEL_ID, (model_id,)).fetchall()

            self._write_model_metadata_json(conn, model_id, model_dest_dir)

        total_bytes = sum(r["actual_bytes"] for r in records)
        transferred_bytes_acc = 0

        for record in records:
            file_id = record["file_id"]
            staging_path = Path(record["staging_path"]) if record["staging_path"] else None
            cleaned_name = record["cleaned_filename"]

            if not staging_path or not staging_path.exists():
                raise StagedFileMissing(f"Staged file missing at '{staging_path}'")

            # Measured here, because a job resumed by an older build recorded only its last attempt.
            expected_bytes = staging_path.stat().st_size
            expected_hash = record["sha256"] or sha256_of(staging_path, self.offload_chunk_size)
            target_nas_path = files_dest_dir / cleaned_name
            temp_nas_path = files_dest_dir / f"{cleaned_name}.part"

            with self._lock:
                if worker_id in self.active_offloads:
                    self.active_offloads[worker_id]["current_file"] = cleaned_name

            t0 = time.time()
            bytes_streamed = 0
            try:
                with open(staging_path, "rb") as f_in, open(temp_nas_path, "wb") as f_out:
                    while True:
                        buf = f_in.read(self.offload_chunk_size)
                        if not buf:
                            break
                        f_out.write(buf)
                        bytes_streamed += len(buf)
                        transferred_bytes_acc += len(buf)

                        elapsed = max(0.001, time.time() - t0)
                        speed = int(bytes_streamed / elapsed)
                        with self._lock:
                            if worker_id in self.active_offloads:
                                self.active_offloads[worker_id]["transferred_bytes"] = transferred_bytes_acc
                                self.active_offloads[worker_id]["speed_bps"] = speed
                    f_out.flush()
                    os.fsync(f_out.fileno())

                # Staging is about to go, and production has no second copy, so this reads the whole
                # file back. A size check misses same-length corruption and writes SMB failed late.
                nas_size = temp_nas_path.stat().st_size
                if nas_size != expected_bytes:
                    raise OSError(f"Byte mismatch on NAS for {cleaned_name} (Expected {expected_bytes}, got {nas_size})")
                if sha256_of(temp_nas_path, self.offload_chunk_size) != expected_hash:
                    raise OSError(f"Checksum mismatch on NAS for {cleaned_name}")
                os.replace(temp_nas_path, target_nas_path)
            finally:
                with contextlib.suppress(OSError):
                    temp_nas_path.unlink(missing_ok=True)

            nas_rel_path = str(folder / "files" / cleaned_name)
            with self._get_db() as conn:
                conn.execute(queries.UPDATE_DOWNLOADED_FILES_STORAGE_TIER_BY_FILE_ID_V2, (str(target_nas_path), nas_rel_path, file_id))

            # Last on purpose. If this ran before the row update, a crash between them would leave
            # the catalog pointing at a staged file that no longer exists.
            staging_path.unlink(missing_ok=True)

        try:
            staged_parent = Path(records[0]["staging_path"]).parent
            if staged_parent.exists() and not any(staged_parent.iterdir()):
                staged_parent.rmdir()
        except Exception:
            pass

        with self._get_db() as conn:
            with conn:
                conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V6, (str(model_dest_dir), job["id"]))

                conn.execute(queries.UPDATE_COLLECTIONS_VAULT_PATH_BY_ID, (str(model_dest_dir), model_id))

                conn.execute(queries.INSERT_DOWNLOAD_AUDIT_LOG_V9, (model_id, job_attempt(job), f"Archived to: {model_dest_dir}"))

        log_layer("storage", logging.INFO, f"Offloader #{worker_id}: Model #{model_id} permanently archived to {model_dest_dir}")

    def get_status(self) -> Dict[str, Any]:
        with self._get_db() as conn:
            cb_row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY_V2).fetchone()
            cb_reason = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY_V3).fetchone()

            counts = conn.execute(queries.SELECT_DOWNLOAD_JOBS).fetchall()
            status_map = {r["status"]: r["c"] for r in counts}
            open_issues = download_issues.open_count(conn)

            queued_query = f"""
                SELECT dj.id, dj.model_id, dj.status, dj.enqueued_at, dj.total_files, dj.total_bytes,
                       m.name AS model_name, {CREATOR_COLUMN}
                FROM download_jobs dj
                JOIN collections m ON dj.model_id = m.id
                WHERE dj.status IN ('pending', 'downloading', 'staging_complete', 'offloading')
                ORDER BY dj.enqueued_at ASC
                LIMIT ?;
            """
            queued_rows = conn.execute(queued_query, (self.QUEUED_JOBS_PAGE,)).fetchall()
            queued_jobs = [dict(r) for r in queued_rows]

        with self._lock:
            active_dl_list = list(self.active_downloads.values())
            active_off_list = list(self.active_offloads.values())

        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "reconnect_required": self.needs_reconnect(),
            "circuit_breaker_tripped": bool(cb_row and cb_row["value"] == "1"),
            "circuit_breaker_reason": cb_reason["value"] if cb_reason else "",
            "active_downloads": active_dl_list,
            "active_offloads": active_off_list,
            "active_workers": active_dl_list,
            "queued_jobs": queued_jobs,
            "queued_total": sum(status_map.get(status, 0) for status in ("pending", *queries.LIVE_JOB_STATUSES)),
            "open_issues": open_issues,
            "counts": {
                "pending": status_map.get("pending", 0),
                "downloading": status_map.get("downloading", 0),
                "staging_complete": status_map.get("staging_complete", 0),
                "offloading": status_map.get("offloading", 0),
                "completed": status_map.get("completed", 0),
                "failed": status_map.get("failed", 0),
            }
        }
