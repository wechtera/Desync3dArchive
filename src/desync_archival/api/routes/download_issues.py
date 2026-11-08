"""Download Issues: the flagged files, Keep, and the upload that replaces one."""

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

import anyio
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from desync_archival.api.routing import Conn, Params, current_route, name_route
from desync_archival.services import download_issues, issue_upload
from desync_archival.services.download_issues import IssueRefused
from desync_archival.services.downloader import StorageUnavailableError
from desync_archival.services.logger import logger

WRITE_BATCH = 4 * 1024 * 1024
# Room left in the work folder after an upload, so it can't starve a running download.
FREE_SPACE_MARGIN = 2 * 1024 ** 3


def handle_get_download_issues(conn: Conn, params: Params) -> tuple[dict[str, Any], int]:
    return download_issues.listing(conn, resolved=params.get("state", ["open"])[0] == "resolved"), 200


def handle_post_download_issue_keep(conn: Conn, issue_id: int) -> tuple[dict[str, Any], int]:
    try:
        return download_issues.keep(conn, issue_id), 200
    except IssueRefused as e:
        return {"error": str(e)}, e.code


def _with_connection(request: Request, work: Any, *args: Any, **kwargs: Any) -> Any:
    conn = request.app.state.open_database()
    try:
        return work(conn, *args, **kwargs)
    finally:
        conn.close()


def _work_folder(request: Request, length: int) -> Path:
    try:
        folder = request.app.state.download_manager().storage.get_work_path() / "uploads"
    except StorageUnavailableError as e:
        raise IssueRefused(f"The work folder isn't available: {e}", 503) from e
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < length + FREE_SPACE_MARGIN:
        raise IssueRefused("The work folder doesn't have room for this file. Free some space and try again.", 507)
    return folder


async def _receive(request: Request, path: Path) -> str:
    """Writes the body to disk as it arrives, and returns its SHA-256."""
    hasher = hashlib.sha256()
    batch = bytearray()
    # Batched, so a 400 MB upload is about a hundred trips to a worker thread, not thousands.
    async with await anyio.open_file(path, "wb") as out:
        async for chunk in request.stream():
            hasher.update(chunk)
            batch += chunk
            if len(batch) >= WRITE_BATCH:
                await out.write(bytes(batch))
                batch.clear()
        await out.write(bytes(batch))
    return hasher.hexdigest()


async def put_download_issue_file(request: Request, issue_id: int) -> Response:
    """Takes a file the owner downloaded by hand. The middleware has already checked its type and length."""
    name_route(request)
    name = request.query_params.get("name", "").strip()
    confirm = request.query_params.get("confirm") == "1"
    upload: Path | None = None
    try:
        if not name:
            raise IssueRefused("The upload needs its file name.")
        await run_in_threadpool(_with_connection, request, issue_upload.check, issue_id, name, confirm)
        folder = await run_in_threadpool(_work_folder, request, int(request.headers["content-length"]))
        upload = folder / f"issue-{issue_id}-{uuid.uuid4().hex}.part"
        sha256 = await _receive(request, upload)
        result = await run_in_threadpool(
            _with_connection, request, issue_upload.place, issue_id, upload, name=name, sha256=sha256, confirm_name=confirm,
        )
        return JSONResponse(result)
    except IssueRefused as e:
        return JSONResponse({"error": str(e)}, e.code)
    except ClientDisconnect:
        return JSONResponse({"error": "The upload stopped before it finished."}, 400)
    except Exception:  # noqa: BLE001
        logger.exception(f"Unhandled error in {current_route()}")
        return JSONResponse({"error": "Internal server error"}, 500)
    finally:
        if upload:
            upload.unlink(missing_ok=True)
