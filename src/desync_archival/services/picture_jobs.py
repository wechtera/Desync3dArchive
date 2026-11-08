"""Pictures E54 chose that need the NAS: an archive image never extracted, a linked library's image,
or a render of the artifact the LLM picked. The image worker runs these when its own queue is idle.

Each job sets a picture only where nothing better arrived meanwhile: a locked picture, or one a
person or the matcher set, is never replaced. The exception is a person's own choice that had to wait
for the NAS, which replaces anything and is locked. An offline NAS leaves a job pending, because that
says nothing about the job.
"""

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import picture_jobs as queries
from desync_archival.services import grouping_apply
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.logger import log_layer
from desync_archival.services.mesh_renderer import MeshRenderer

IDLE, WORKING, OFFLINE, NO_BLENDER = "idle", "working", "storage_offline", "blender_unavailable"
OWNER = "owner"
RENDERABLE = (".stl",)
# A picture only these may be replaced by a job: nothing, or a stand-in.
REPLACEABLE = (None, "group", "cover", "render")


def _finish(conn: sqlite3.Connection, job_id: int, status: str, detail: str = "") -> str:
    with conn:
        conn.execute(queries.FINISH_JOB, (status, detail, job_id))
    return WORKING


def _set(conn: sqlite3.Connection, job: sqlite3.Row, path: str, source: str) -> None:
    with conn:
        carried = None if source == OWNER else _untouched_snapshot(conn, job)
        if job["model_id"]:
            update = queries.SET_OWNER_PICTURE if source == OWNER else queries.SET_MODEL_PICTURE
            conn.execute(update, (path, job["model_id"]) if source == OWNER else (path, source, job["model_id"]))
        if job["group_id"]:
            conn.execute(queries.SET_GROUP_PICTURE, (path, source, job["group_id"]))
        if carried:
            snapshot_id, unit_key = carried
            conn.execute(queries.UPDATE_SNAPSHOT_HASH, (grouping_apply.state_hash(grouping_apply.state(conn, unit_key)), snapshot_id))


def _untouched_snapshot(conn: sqlite3.Connection, job: sqlite3.Row) -> tuple[int, str] | None:
    """The apply snapshot this job belongs to, if the unit still stands exactly as that apply left it.

    The picture is part of what the apply did, so it moves the snapshot's after state with it.
    Without this, undo would read the app's own late picture as a person's edit and refuse. A unit
    someone changed meanwhile doesn't match, so their edit still stops undo.
    """
    snapshot_id = json.loads(job["source_json"] or "{}").get("snapshot")
    if not snapshot_id:
        return None
    row = conn.execute(queries.SELECT_OPEN_SNAPSHOT, (snapshot_id,)).fetchone()
    if not row or row[1] != grouping_apply.state_hash(grouping_apply.state(conn, row[0])):
        return None
    return snapshot_id, row[0]


def _previews() -> Path:
    return app_config.previews_dir().resolve()


def _archive_path(conn: sqlite3.Connection, file_id: int) -> Path | None:
    row = conn.execute(queries.SELECT_ARCHIVE_FILE, (file_id,)).fetchone()
    stored = row and (row["vault_path"] or row["staging_path"])
    return Path(stored) if stored else None


def _extract(conn: sqlite3.Connection, job: sqlite3.Row, source: dict[str, Any], peeker: ArchivePeeker) -> str:
    member = conn.execute(queries.SELECT_MEMBER, (source["member"],)).fetchone()
    archive = _archive_path(conn, source["file_id"])
    if not member or not archive:
        return _finish(conn, job["id"], "skipped", "The archive or the image is no longer in the catalog")
    if not archive.is_file():
        return OFFLINE
    target = app_config.data_dir().resolve() / source["preview"]
    target.parent.mkdir(parents=True, exist_ok=True)
    found = peeker._extract_one_image(archive, member["internal_path"], target.parent, target.name)
    if not found or not found.is_file():
        return _finish(conn, job["id"], "failed", f"Couldn't extract {member['internal_path']}")
    _set(conn, job, source["preview"], source.get("source", "llm"))
    return _finish(conn, job["id"], "done")


def _copy(conn: sqlite3.Connection, job: sqlite3.Row, source: dict[str, Any]) -> str:
    origin = Path(source["origin"])
    if not origin.parent.is_dir():
        return OFFLINE
    if not origin.is_file():
        return _finish(conn, job["id"], "failed", f"{origin.name} is no longer in the library")
    name = f"{hashlib.sha256(str(origin).encode()).hexdigest()[:12]}{origin.suffix.lower()}"
    target = _previews() / "library" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.partial")
    try:
        shutil.copy2(origin, partial)
        partial.replace(target)
    except OSError as e:
        partial.unlink(missing_ok=True)
        return _finish(conn, job["id"], "failed", f"Copying {origin.name}: {e}")
    _set(conn, job, f"previews/library/{name}", source.get("source", "llm"))
    return _finish(conn, job["id"], "done")


def _mesh(conn: sqlite3.Connection, artifact: sqlite3.Row, scratch: Path, peeker: ArchivePeeker) -> Path | str:
    """The artifact as a file on disk, or a state to stop on."""
    if artifact["archive_member_id"]:
        archive = _archive_path(conn, artifact["file_id"])
        if not archive:
            return "gone"
        if not archive.is_file():
            return OFFLINE
        found = peeker._extract_one_image(archive, artifact["path"], scratch)
        return found if found and found.is_file() else "failed"
    library = conn.execute(queries.SELECT_LIBRARY_ROOT, (artifact["file_id"],)).fetchone()
    if not library:
        return "gone"
    root = Path(library["path"])
    if not root.is_dir():
        return OFFLINE
    path = root / artifact["path"]
    return path if path.is_file() else "failed"


def _render(conn: sqlite3.Connection, job: sqlite3.Row, source: dict[str, Any], renderer: MeshRenderer, peeker: ArchivePeeker) -> str:
    current = conn.execute(queries.SELECT_MODEL_PICTURE, (job["model_id"],)).fetchone()
    if not current or current["image_locked"] or current["image_source"] not in REPLACEABLE or current["image_path"]:
        return _finish(conn, job["id"], "skipped", "The model has a picture now")
    artifact = conn.execute(queries.SELECT_ARTIFACT, (source["artifact"],)).fetchone()
    if not artifact:
        return _finish(conn, job["id"], "skipped", "The artifact is no longer in the catalog")
    if not artifact["filename"].lower().endswith(RENDERABLE):
        return _finish(conn, job["id"], "failed", f"{artifact['filename']} isn't an STL, so it can't be rendered")
    if not renderer.is_available():
        return NO_BLENDER
    relative = f"previews/renders/{job['model_id']}.jpg"
    target = app_config.data_dir().resolve() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="desync-render-") as scratch:
        mesh = _mesh(conn, artifact, Path(scratch), peeker)
        if mesh == OFFLINE:
            return OFFLINE
        if not isinstance(mesh, Path):
            return _finish(conn, job["id"], "failed", f"Couldn't read {artifact['filename']}")
        log_layer("storage", logging.INFO, f"Rendering model #{job['model_id']} from {artifact['filename']}")
        if not renderer.render_primary_thumbnail(mesh, target):
            return _finish(conn, job["id"], "failed", f"Blender failed on {artifact['filename']}")
    _set(conn, job, relative, "render")
    _cover(conn, job, source, target)
    return _finish(conn, job["id"], "done")


def _cover(conn: sqlite3.Connection, job: sqlite3.Row, source: dict[str, Any], render: Path) -> None:
    """E54-9: a library collection whose cover was the size heuristic's render gets this one.

    Only where its release holds no artwork, since a cover there can only have been a render.
    """
    for collection_id in source.get("covers", []):
        cover = _previews() / str(collection_id) / "cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        partial = cover.with_name("cover.jpg.partial")
        try:
            shutil.copy2(render, partial)
            partial.replace(cover)
        except OSError as e:
            partial.unlink(missing_ok=True)
            log_layer("storage", logging.WARNING, f"Couldn't replace the cover of collection #{collection_id}: {e}")


def run_one(conn: sqlite3.Connection, renderer: MeshRenderer, peeker: ArchivePeeker | None = None) -> str:
    """Runs the oldest pending job. Returns idle, working, or why it had to wait."""
    conn.row_factory = sqlite3.Row
    job = conn.execute(queries.SELECT_PENDING).fetchone()
    if not job:
        return IDLE
    source = json.loads(job["source_json"])
    peeker = peeker or ArchivePeeker(app_config.db_path())
    try:
        if job["kind"] == "extract":
            return _extract(conn, job, source, peeker)
        if job["kind"] == "copy":
            return _copy(conn, job, source)
        return _render(conn, job, source, renderer, peeker)
    except Exception as e:  # noqa: BLE001 - one bad job fails itself, never the worker
        return _finish(conn, job["id"], "failed", str(e)[:300])


def status(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"pending": 0, "done": 0, "failed": 0, "skipped": 0}
    for state, count in conn.execute(queries.COUNT_BY_STATUS).fetchall():
        counts[state] = count
    return counts
