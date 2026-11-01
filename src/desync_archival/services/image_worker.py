"""Linked library covers and archive peeks. Inventory comes first, so this runs behind it.

Failed jobs stay failed until someone hits retry. The old scan re-rendered them on every rescan.
"""

import logging
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from desync_archival import settings as app_config
from desync_archival.db.queries import image_worker as queries
from desync_archival.services import inactive_log
from desync_archival.services.external_indexer import COVER, ExternalLibraryIndexer
from desync_archival.services.logger import log_layer, logger
from desync_archival.services.mesh_renderer import MeshRenderer

# In app_settings, not on the worker, so a pause survives a restart.
PAUSED_SETTING = "image_worker_paused"
IDLE_SECONDS = 5.0

PAUSED = "paused"
WAITING_FOR_INVENTORY = "waiting_for_inventory"
STORAGE_OFFLINE = "storage_offline"
BLENDER_UNAVAILABLE = "blender_unavailable"
IDLE = "idle"
WORKING = "working"
STOPPED = "stopped"

# The running worker, so the routes can report its state.
current: Optional["ImageWorker"] = None


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    """Stores pause in app_settings, so it survives a restart."""
    value = "1" if paused else "0"
    with conn:
        if not conn.execute(queries.UPDATE_APP_SETTINGS_VALUE_BY_KEY, (value, PAUSED_SETTING)).rowcount:
            conn.execute(queries.INSERT_APP_SETTINGS, (PAUSED_SETTING, value))


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY, (PAUSED_SETTING,)).fetchone()
    return bool(row) and row[0] == "1"


def retry_failed(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute(
            queries.UPDATE_LIBRARY_WORK_QUEUE_STATUS_BY_STATUS
        ).rowcount


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {"pending": 0, "done": 0, "failed": 0, "no_source": 0, "skipped": 0}
    for row in conn.execute(queries.SELECT_LIBRARY_WORK_QUEUE).fetchall():
        counts[row[0]] = row[1]
    return {"paused": is_paused(conn), "state": current.state if current else STOPPED, "counts": counts}


class ImageWorker:
    """Works through covers and peeks one at a time, behind any running inventory."""
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.renderer = MeshRenderer()
        self.indexer = ExternalLibraryIndexer(self.db_path)
        self.state = STOPPED
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 45000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="image-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # NOTE: can't interrupt Blender. A render in progress runs out, capped at 120s by the renderer.
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.state = STOPPED

    def _run(self) -> None:
        logger.info("[ImageWorker] Started")
        while not self._stop.is_set():
            try:
                worked = self.run_once() == WORKING
            except Exception as e:  # noqa: BLE001 - one bad job must not end the worker
                logger.error(f"[ImageWorker] Unexpected error: {e}")
                worked = False
            if not worked:
                self._stop.wait(IDLE_SECONDS)

    def _set_state(self, state: str) -> str:
        if state != self.state:
            log_layer("storage", logging.INFO, f"[ImageWorker] {self.state} -> {state}")
            self.state = state
        return state

    def run_once(self) -> str:
        with self._get_db() as conn:
            if is_paused(conn):
                return self._set_state(PAUSED)
            if conn.execute(
                queries.SELECT_PIPELINE_TASKS_1_BY_STATUS
            ).fetchone():
                return self._set_state(WAITING_FOR_INVENTORY)

            job = conn.execute(queries.SELECT_LIBRARY_WORK_QUEUE_BY_STATUS).fetchone()
            if not job:
                return self._set_state(IDLE)

            # Offline just means wait. It says nothing about the model, so the job stays pending.
            if not Path(job["library_path"]).is_dir():
                return self._set_state(STORAGE_OFFLINE)
            if not job["is_active"]:
                self._finish(conn, job, "skipped", "The model is inactive")
                return self._set_state(WORKING)

            self._set_state(WORKING)
            if job["kind"] == COVER:
                return self._cover(conn, job)
            return self._peek(conn, job)

    def _finish(self, conn: sqlite3.Connection, job: sqlite3.Row, state: str, detail: str = "") -> None:
        with conn:
            conn.execute(
                queries.UPDATE_LIBRARY_WORK_QUEUE_STATUS_BY_ID,
                (state, detail, job["id"]),
            )

    def _fail(self, conn: sqlite3.Connection, job: sqlite3.Row, state: str, reason: str, detail: str) -> str:
        with conn:
            inactive_log.record(
                conn, inactive_log.MODEL, job["model_id"], job["name"], inactive_log.IMAGE_WORKER, reason, detail,
            )
        self._finish(conn, job, state, detail)
        return WORKING

    # Asked after any failure, so a mount drop mid-job can't be recorded as a bad model.
    def _still_mounted(self, job: sqlite3.Row) -> bool:
        return Path(job["library_path"]).is_dir()

    def _best_stl(self, conn: sqlite3.Connection, job: sqlite3.Row, model_dir: Path) -> Path | None:
        # Artifacts first: a card's folder can hold another card's carved-out files, and a
        # character's main folder can be Lychee-only.
        root = Path(job["library_path"])
        artifacts = conn.execute(queries.SELECT_MODEL_ARTIFACTS_LOOSE_STL_PATHS, (job["model_id"],)).fetchall()
        best = self.renderer.best_of(root / r[0] for r in artifacts)
        if best:
            return best
        rows = conn.execute(queries.SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_MODEL_ID, (job["model_id"],)).fetchall()
        for folder in [model_dir, *(root / r[0] for r in rows)]:
            # A review card can own single files, and rglob finds nothing under a file.
            if folder.is_file():
                if folder.suffix.lower() == ".stl":
                    return folder
                continue
            best = self.renderer.select_best_render_candidate(folder)
            if best:
                return best
        return None

    def _render_cover(self, conn: sqlite3.Connection, job: sqlite3.Row, model_dir: Path, cover: Path) -> str | None:
        """Renders from a loose mesh, then from inside an archive. Returns a state to stop on, or None."""
        best_stl = self._best_stl(conn, job, model_dir)
        with tempfile.TemporaryDirectory(prefix="desync-cover-") as scratch:
            if not best_stl:
                best_stl = self.renderer._resolve_model_stl_candidate(conn, job["model_id"], Path(scratch))
            if not best_stl:
                return self._fail(conn, job, "no_source", inactive_log.NO_SOURCE, "No artwork and no STL, loose or in an archive")
            if not self.renderer.is_available():
                return self._set_state(BLENDER_UNAVAILABLE)
            log_layer("storage", logging.INFO, f"Rendering cover for Model #{job['model_id']} from {best_stl.name}")
            if not self.renderer.render_primary_thumbnail(best_stl, cover):
                if not self._still_mounted(job):
                    return self._set_state(STORAGE_OFFLINE)
                return self._fail(conn, job, "failed", inactive_log.RENDER_FAILED, f"Blender failed on {best_stl.name}")
        return None

    def _cover(self, conn: sqlite3.Connection, job: sqlite3.Row) -> str:
        model_id = job["model_id"]
        model_dir = Path(job["vault_path"])
        release_dir = Path(job["library_path"]) / job["external_key"].split("/", 2)[1]
        cover = app_config.previews_dir().resolve() / str(model_id) / "cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)

        if not cover.exists() or cover.stat().st_size == 0:
            artwork = self.indexer._find_model_artwork(model_dir.parent if model_dir.is_file() else model_dir, release_dir)
            if artwork:
                try:
                    shutil.copy2(str(artwork), str(cover))
                except OSError as e:
                    if not self._still_mounted(job):
                        return self._set_state(STORAGE_OFFLINE)
                    return self._fail(conn, job, "failed", inactive_log.RENDER_FAILED, f"Copying {artwork.name}: {e}")
            else:
                if not self._still_mounted(job):
                    return self._set_state(STORAGE_OFFLINE)
                stopped = self._render_cover(conn, job, model_dir, cover)
                if stopped is not None:
                    return stopped

        with conn:
            conn.execute(queries.UPDATE_COLLECTIONS_PRIMARY_IMAGE_URL_BY_ID, (f"/previews/{model_id}/cover.jpg", model_id))
            inactive_log.resolve(conn, inactive_log.MODEL, model_id, inactive_log.RENDER_FAILED)
            inactive_log.resolve(conn, inactive_log.MODEL, model_id, inactive_log.NO_SOURCE)
        self._finish(conn, job, "done")
        return WORKING

    def _peek(self, conn: sqlite3.Connection, job: sqlite3.Row) -> str:
        from desync_archival.services.archive_peeker import ArchivePeeker

        row = conn.execute(queries.SELECT_DOWNLOADED_FILES_VAULT_PATH_BY_FILE_ID, (job["file_id"],)).fetchone()
        if not row:
            self._finish(conn, job, "skipped", "The file is no longer in the catalog")
            return WORKING
        path = Path(row["vault_path"])
        try:
            result = ArchivePeeker(self.db_path).inspect_and_record(job["model_id"], job["file_id"], path)
        except Exception as e:  # noqa: BLE001 - a corrupt archive fails its own job, nothing more
            if not self._still_mounted(job):
                return self._set_state(STORAGE_OFFLINE)
            return self._fail(conn, job, "failed", inactive_log.PEEK_FAILED, f"{path.name}: {e}")
        if not result.get("success"):
            if not self._still_mounted(job):
                return self._set_state(STORAGE_OFFLINE)
            return self._fail(conn, job, "failed", inactive_log.PEEK_FAILED, f"{path.name}: {result.get('error', 'peek failed')}")
        with conn:
            inactive_log.resolve(conn, inactive_log.MODEL, job["model_id"], inactive_log.PEEK_FAILED)
        self._finish(conn, job, "done")
        return WORKING
