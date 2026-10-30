"""
api/server.py - FastAPI app: one catch-all /api route, static assets and preview streaming.
Supports both 2-tuple JSON responses and 3-tuple binary image streams.
"""

import json
import mimetypes
import re
import sqlite3
import time
import traceback
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from desync_archival import settings as app_config
from desync_archival.api.routes import dispatch_route
from desync_archival.services.db import get_db_connection, init_persistent_tables
from desync_archival.services.downloader import DownloadManager, StorageUnavailableError
from desync_archival.services.logger import log_context, logger
from desync_archival.services.task_manager import TaskManager

DB_PATH = app_config.db_path().resolve()
PREVIEWS_DIR = app_config.previews_dir().resolve()
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

HOST = "127.0.0.1"
PORT = 8080
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
PREVIEW_CACHE_CONTROL = "public, max-age=604800"
PREVIEW_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
SVG_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">'
    b'<rect width="300" height="200" fill="#1e1e24"/>'
    b'<path d="M110 130 L150 80 L190 130 Z" fill="#3a3a46"/>'
    b'<circle cx="130" cy="75" r="12" fill="#505060"/>'
    b'<text x="150" y="160" font-family="sans-serif" font-size="12" fill="#888899" text-anchor="middle">No Preview Available</text>'
    b"</svg>"
)

download_manager: DownloadManager | None = None
task_manager: TaskManager | None = None


def get_db() -> sqlite3.Connection:
    return get_db_connection(DB_PATH)


class RequestLoggingMiddleware:
    """Gives every request a request_id and logs its outcome.

    Query strings are left out of the log line because they can carry credentials.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status = {"code": 500}

        async def capture_status(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        started = time.perf_counter()
        with log_context(request_id=uuid.uuid4().hex):
            try:
                await self.app(scope, receive, capture_status)
            finally:
                duration_ms = (time.perf_counter() - started) * 1000
                logger.info(f"{scope['method']} {scope['path']} {status['code']} {duration_ms:.1f}ms")


def redirect_location(data: Any) -> str | None:
    if isinstance(data, str):
        match = re.search(r"url=([^'>\s]+)", data) or re.search(r"href=['\"]([^'\"]+)['\"]", data)
        return match.group(1) if match else None
    if isinstance(data, dict):
        return data.get("location") or data.get("auth_url") or data.get("url")
    return None


def build_response(data: Any, status: int = 200, extra_headers: dict | None = None) -> Response:
    headers = {**CORS_HEADERS, **(extra_headers or {})}

    if status in REDIRECT_STATUSES:
        location = redirect_location(data)
        if location:
            headers["Location"] = location

    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
        headers.setdefault("Content-Type", "application/octet-stream")
    elif isinstance(data, str):
        payload = data.encode("utf-8")
        headers.setdefault("Content-Type", "text/html; charset=utf-8")
    else:
        payload = json.dumps(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    return Response(content=payload, status_code=status, headers=headers)


def parse_body(body_bytes: bytes) -> Any:
    if not body_bytes:
        return {}
    try:
        return json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return {}


def run_route(method: str, path: str, body_data: Any, params: dict) -> Response:
    """Blocking route work, run in the threadpool."""
    conn = get_db()
    try:
        route_res = dispatch_route(
            method=method,
            path=path,
            conn=conn,
            download_manager=download_manager,
            body_data=body_data,
            query_params=params,
            task_manager=task_manager,
        )
    except Exception as e:
        traceback.print_exc()
        route_res = ({"error": f"Internal Server Error: {e}"}, 500)
    finally:
        conn.close()

    if isinstance(route_res, tuple) and len(route_res) == 3:
        data, status, extra_headers = route_res
        return build_response(data, status, extra_headers=extra_headers)
    if isinstance(route_res, tuple) and len(route_res) == 2:
        data, status = route_res
        return build_response(data, status)
    return build_response(route_res, 200)


def serve_preview(rel_path: str) -> Response:
    file_path = (app_config.data_dir() / "previews" / rel_path).resolve()

    if not file_path.is_relative_to(PREVIEWS_DIR) or not file_path.is_file():
        # Graceful SVG placeholder fallback instead of breaking 404 JSON
        return Response(
            content=SVG_PLACEHOLDER,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )

    content_type, _ = mimetypes.guess_type(str(file_path))
    if not content_type:
        content_type = PREVIEW_CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")

    return FileResponse(file_path, media_type=content_type, headers={"Cache-Control": PREVIEW_CACHE_CONTROL})


def start_managers() -> None:
    global download_manager, task_manager
    logger.info("[Server] Initializing database schema")
    conn = get_db()
    try:
        init_persistent_tables(conn)
        app_config.check_stored_paths(conn)
    finally:
        conn.close()

    logger.info("[Server] Initializing background TaskManager")
    task_manager = TaskManager(DB_PATH, app_config.data_dir())

    logger.info("[Server] Initializing background DownloadManager")
    download_manager = DownloadManager(DB_PATH)
    try:
        download_manager.start()
    except StorageUnavailableError as e:
        logger.warning(f"[Server] Storage notice: {e} (Workers will start once storage is configured via UI)")


def stop_managers() -> None:
    logger.info("[Server] Shutting down")
    if download_manager:
        download_manager.stop()


def create_app(manage_workers: bool = True) -> FastAPI:
    """Builds the app. With manage_workers off, the caller owns the background managers."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_workers:
            await run_in_threadpool(start_managers)
        yield
        if manage_workers:
            await run_in_threadpool(stop_managers)

    app = FastAPI(title="DESYNC 3D Vault", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestLoggingMiddleware)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "DELETE"])
    async def api_catch_all(request: Request) -> Response:
        # raw_path keeps the percent-encoding that dispatch_route has always received.
        path = request.scope["raw_path"].decode("latin-1").split("?", 1)[0]
        params = urllib.parse.parse_qs(request.url.query)
        body_data = parse_body(await request.body()) if request.method != "GET" else {}
        return await run_in_threadpool(run_route, request.method, path, body_data, params)

    @app.get("/previews/{rel_path:path}")
    def previews(rel_path: str) -> Response:
        return serve_preview(rel_path)

    @app.get("/")
    @app.get("/index.html")
    def index() -> Response:
        return FileResponse(app_config.web_dir() / "index.html")

    app.mount("/static", StaticFiles(directory=app_config.web_dir() / "static"), name="static")

    @app.api_route("/{rest:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
    def fallback(request: Request) -> Response:
        if request.method == "GET":
            return JSONResponse({"error": "Not found"}, 404)
        if request.method == "OPTIONS":
            return Response(
                status_code=204,
                headers={
                    **CORS_HEADERS,
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                },
            )
        return JSONResponse({"error": "Method not allowed for static paths"}, 405, headers=CORS_HEADERS)

    return app


app = create_app()


def main() -> None:
    logger.info(f"[Server] Starting Web UI and API at http://localhost:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_config=None, access_log=False, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
