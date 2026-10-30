"""
api/server.py - FastAPI app: one catch-all /api route, static assets and preview streaming.
Supports both 2-tuple JSON responses and 3-tuple binary image streams.
"""

import ipaddress
import json
import mimetypes
import re
import sqlite3
import time
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

REDIRECT_STATUSES = (301, 302, 303, 307, 308)
GENERIC_ERROR = "Internal server error"
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


class BodyTooLargeError(Exception):
    pass


def split_host_header(value: str) -> tuple[str, str]:
    """Splits a Host header into a lowercase hostname and port, including IPv6 forms."""
    parts = urllib.parse.urlsplit(f"//{value}")
    try:
        port = str(parts.port or "")
    except ValueError:
        port = ""
    return (parts.hostname or "").lower(), port


class SecurityMiddleware:
    """Validates Host, Origin, content type and body size before a request reaches a route.

    Host blocks DNS rebinding, and Origin blocks cross-site requests. A POST or DELETE with a body
    must be JSON, so a cross-site form can't reach a route. An empty body has nothing to parse.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        refusal = self.check(request, scope)
        if refusal:
            status, message = refusal
            logger.warning(f"Refused {scope['method']} {scope['path']}: {message}")
            await JSONResponse({"error": message}, status)(scope, receive, send)
            return

        total = 0
        started = False

        async def limited_receive() -> dict:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > app_config.MAX_BODY_BYTES:
                    raise BodyTooLargeError
            return message

        async def tracked_send(message: dict) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyTooLargeError:
            if not started:
                await JSONResponse({"error": "Request body too large"}, 413)(scope, receive, send)

    @staticmethod
    def check(request: Request, scope: dict) -> tuple[int, str] | None:
        allowed = app_config.allowed_hosts()
        host, port = split_host_header(request.headers.get("host", ""))
        if host not in allowed:
            return 400, "Host not allowed"

        origin = request.headers.get("origin")
        if origin is not None:
            parts = urllib.parse.urlsplit(origin)
            origin_host = (parts.hostname or "").lower()
            try:
                origin_port = str(parts.port or "")
            except ValueError:
                origin_port = "invalid"
            if parts.scheme not in ("http", "https") or origin_host != host or origin_port != port:
                return 403, "Origin not allowed"

        if scope["method"] in ("POST", "DELETE") and scope["path"].startswith("/api/"):
            length = request.headers.get("content-length", "")
            has_body = "transfer-encoding" in request.headers or (length not in ("", "0"))
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if has_body and content_type != "application/json":
                return 415, "Content-Type must be application/json"
            if length.isdigit() and int(length) > app_config.MAX_BODY_BYTES:
                return 413, "Request body too large"
        return None


def redirect_location(data: Any) -> str | None:
    if isinstance(data, str):
        match = re.search(r"url=([^'>\s]+)", data) or re.search(r"href=['\"]([^'\"]+)['\"]", data)
        return match.group(1) if match else None
    if isinstance(data, dict):
        return data.get("location") or data.get("auth_url") or data.get("url")
    return None


def build_response(data: Any, status: int = 200, extra_headers: dict | None = None) -> Response:
    headers = dict(extra_headers or {})

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
    except Exception:
        logger.exception(f"Unhandled error in {method} {path}")
        route_res = ({"error": GENERIC_ERROR}, 500)
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
    app.add_middleware(SecurityMiddleware)
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
        return JSONResponse({"error": "Method not allowed for static paths"}, 405)

    return app


app = create_app()


def check_bind_host(host: str) -> None:
    """Binding beyond loopback needs the login and TLS that LAN mode adds."""
    if host == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise app_config.SettingsError(f"Refusing to bind to {host}: only loopback is supported until LAN mode.")


def main() -> None:
    host, port = app_config.host(), app_config.port()
    check_bind_host(host)
    logger.info(f"[Server] Starting Web UI and API at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
