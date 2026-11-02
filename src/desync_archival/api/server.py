"""
api/server.py - FastAPI app: one catch-all /api route, static assets and preview streaming.
Supports both 2-tuple JSON responses and 3-tuple binary image streams.
"""

import fcntl
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TextIO

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders

from desync_archival import external, migrations
from desync_archival import settings as app_config
from desync_archival.api import schemas
from desync_archival.api.routes import catalog as catalog_routes
from desync_archival.services import documents, image_worker, jobs, login, scheduler
from desync_archival.services import task_manager as task_service
from desync_archival.services.db import get_db_connection, seed_default_settings
from desync_archival.services.downloader import DownloadManager, StorageUnavailableError
from desync_archival.services.logger import init_logging_from_db, log_context, logger
from desync_archival.services.mesh_renderer import MeshRenderer
from desync_archival.services.task_manager import TASK_LOGS, TaskManager

DB_PATH = app_config.db_path().resolve()
PREVIEWS_DIR = app_config.previews_dir().resolve()
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# A re-peek rewrites these in place, so a browser has to ask before reusing one.
PREVIEW_CACHE_CONTROL = "no-cache"
PREVIEW_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
SVG_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">'
    b'<rect width="300" height="200" fill="#1a221c"/>'
    b'<path d="M110 130 L150 80 L190 130 Z" fill="#2c3830"/>'
    b'<circle cx="130" cy="75" r="12" fill="#3a4a3f"/>'
    b'<text x="150" y="160" font-family="sans-serif" font-size="12" fill="#96a48f" text-anchor="middle">No Preview Available</text>'
    b"</svg>"
)

download_manager: DownloadManager | None = None
task_manager: TaskManager | None = None
schedule_stop = threading.Event()
_instance_lock: TextIO | None = None


def get_db() -> sqlite3.Connection:
    return get_db_connection(DB_PATH)


class RequestLoggingMiddleware:
    """Gives every request a request_id and logs its outcome.

    It's the outermost middleware, so refused requests are logged too. Query strings are left out
    of the log line because they can carry credentials.
    """

    POLLED_PATHS = frozenset({
        "/api/tasks/status", "/api/downloads/status", "/api/storage/status", "/api/stashes/active",
        "/api/auth/status", "/api/health", "/healthz",
    })

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
                # The UI polls these every few seconds, which buried everything else in vault.log.
                quiet = scope["path"] in self.POLLED_PATHS and status["code"] < 400
                logger.log(logging.DEBUG if quiet else logging.INFO, f"{scope['method']} {scope['path']} {status['code']} {duration_ms:.1f}ms")


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


# A day, because the LAN certificate is the owner's own. A long policy would lock the browser out
# for its whole length if that certificate ever lapsed, and this app has no plain port to protect.
HSTS_POLICY = "max-age=86400"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


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
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["X-Content-Type-Options"] = "nosniff"
                if scope.get("scheme") == "https":
                    headers["Strict-Transport-Security"] = HSTS_POLICY
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
            # Safety: compares host and port with Host. Together with the allowed Host list this stops DNS rebinding.
            if parts.scheme not in ("http", "https") or origin_host != host or origin_port != port:
                return 403, "Origin not allowed"

        if scope["method"] in ("POST", "DELETE") and scope["path"].startswith("/api/"):
            length = request.headers.get("content-length", "")
            has_body = "transfer-encoding" in request.headers or (length not in ("", "0"))
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            # Safety: a cross-site form can't send JSON without a preflight, and no CORS headers are ever returned.
            if has_body and content_type != "application/json":
                return 415, "Content-Type must be application/json"
            if length.isdigit() and int(length) > app_config.MAX_BODY_BYTES:
                return 413, "Request body too large"
        return None


class LoginMiddleware:
    """Blocks anonymous requests when login is required. Only these paths stay open."""

    # The callback page must stay open: SameSite=Strict cookies are not sent on the redirect back
    # from MMF. It holds no data, and storing the token still needs a session.
    PUBLIC_PATHS = frozenset({
        "/login", "/api/login", "/api/session", "/api/auth/callback", "/healthz",
        "/static/css/vault.css", "/static/js/login.js", "/static/js/auth_callback.js",
    })

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        # Safety: exact match on purpose, so a new route is protected by default.
        if scope["type"] != "http" or not app_config.login_required() or scope["path"] in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if login.session_valid(request.cookies.get(login.COOKIE_NAME)):
            await self.app(scope, receive, send)
            return

        if scope["path"].startswith("/api/"):
            response: Response = JSONResponse({"error": "Login required"}, 401, {"X-Login-Required": "1"})
        else:
            response = RedirectResponse("/login", 303)
        await response(scope, receive, send)


def redirect_location(data: Any) -> str | None:
    """Finds a redirect target in the old handlers' HTML, or dict results."""
    if isinstance(data, str):
        match = re.search(r"url=([^'>\s]+)", data) or re.search(r"href=['\"]([^'\"]+)['\"]", data)
        return match.group(1) if match else None
    if isinstance(data, dict):
        return data.get("location") or data.get("auth_url") or data.get("url")
    return None


def build_response(data: Any, status: int = 200, extra_headers: dict | None = None) -> Response:
    """Turns a handler's result into a Response. Bytes pass through, a str is sent as text, and anything else as JSON."""
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
        # HTML has to be asked for, because a scrape file holds third-party names.
        kind = "text/html" if isinstance(data, schemas.HtmlPage) else "text/plain"
        headers.setdefault("Content-Type", f"{kind}; charset=utf-8")
    else:
        payload = json.dumps(data).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    return Response(content=payload, status_code=status, headers=headers)


def parse_body(body_bytes: bytes) -> Any:
    """Decodes a JSON body. An empty body means no options.

    Raises:
        ValueError: The body isn't JSON. A broken request must not run with the defaults.
    """
    if not body_bytes:
        return {}
    return json.loads(body_bytes.decode("utf-8"))


def serve_preview(rel_path: str) -> Response:
    """Serves a cached preview, or a placeholder for anything missing or outside the previews folder."""
    file_path = (app_config.data_dir() / "previews" / rel_path).resolve()

    # An empty file is a failed extraction, and a blank tab tells the user nothing.
    # Safety: checks the resolved path, so ../ and symlinks can't leave the previews folder.
    if not file_path.is_relative_to(PREVIEWS_DIR) or not file_path.is_file() or file_path.stat().st_size == 0:
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


def serve_document(key: str) -> Response:
    """Serves a PDF inline, using the response status DocumentUnavailable carries."""
    conn = get_db_connection(DB_PATH)
    try:
        path = documents.locate(conn, key)
    except documents.DocumentUnavailable as e:
        return JSONResponse({"error": str(e)}, e.code)
    finally:
        conn.close()
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline", filename=path.name)


def serve_stl_preview(source_type: str, source_id: int) -> Response:
    """Sends one STL for the browser to draw. An archive member is unpacked first, so it shares the render slots."""
    if not catalog_routes.RENDER_SLOTS.acquire(timeout=catalog_routes.RENDER_WAIT_SECONDS):
        return JSONResponse(
            {"error": "Blender is busy with other renders. Try again shortly."}, 429,
            {"Retry-After": catalog_routes.RENDER_RETRY_SECONDS},
        )
    scratch = Path(tempfile.mkdtemp(prefix="desync-preview-"))
    conn = get_db_connection(DB_PATH)
    try:
        path = MeshRenderer().locate_stl(conn, source_type, source_id, scratch)
    except FileNotFoundError as e:
        shutil.rmtree(scratch, ignore_errors=True)
        return JSONResponse({"error": str(e)}, 404)
    except ValueError as e:
        shutil.rmtree(scratch, ignore_errors=True)
        return JSONResponse({"error": str(e)}, 415)
    except (external.ExtractionRefused, OSError) as e:
        shutil.rmtree(scratch, ignore_errors=True)
        logger.warning(f"STL preview failed for {source_type} #{source_id}: {e}")
        return JSONResponse({"error": "That file couldn't be read from its archive."}, 500)
    finally:
        conn.close()
        catalog_routes.RENDER_SLOTS.release()
    return FileResponse(
        path, media_type="model/stl", headers={"Cache-Control": "no-store"},
        background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True),
    )


def start_managers() -> None:
    global download_manager, task_manager
    logger.info("[Server] Initializing database schema")
    conn = get_db()
    try:
        migrations.require_current(conn)
        seed_default_settings(conn)
        app_config.check_stored_paths(conn)
        jobs.recover_interrupted(conn)
    finally:
        conn.close()
    init_logging_from_db(DB_PATH)

    logger.info("[Server] Initializing background TaskManager")
    task_manager = task_service.current = TaskManager(DB_PATH, app_config.data_dir())
    task_manager.remove_scrape_cookies()

    logger.info("[Server] Initializing background DownloadManager")
    download_manager = DownloadManager(DB_PATH)
    try:
        download_manager.start()
    except StorageUnavailableError as e:
        logger.warning(f"[Server] Storage notice: {e} (Workers will start once storage is configured via UI)")

    image_worker.current = image_worker.ImageWorker(DB_PATH)
    image_worker.current.start()

    schedule_stop.clear()
    threading.Thread(target=backup_schedule, name="backup-schedule", daemon=True).start()


def backup_schedule() -> None:
    """Backups check hourly, so one missed while the app was busy or off still happens."""
    wait = jobs.FIRST_BACKUP_CHECK_SECONDS
    since_backup_check = jobs.BACKUP_CHECK_SECONDS
    while not schedule_stop.wait(wait):
        wait = jobs.SCHEDULE_CHECK_SECONDS
        if not task_manager:
            continue
        # This thread is the only thing running the schedules, so one bad tick must not end it.
        try:
            if since_backup_check >= jobs.BACKUP_CHECK_SECONDS:
                since_backup_check = 0
                task_manager.run_scheduled_backup()
            since_backup_check += wait
            conn = get_db_connection(DB_PATH)
            try:
                scheduler.tick(conn, task_manager, download_manager)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.exception("[Server] A scheduled check failed")


def stop_managers() -> None:
    """Signals workers to stop. Daemon threads may still be mid-write when the process exits."""
    logger.info("[Server] Shutting down")
    schedule_stop.set()
    if image_worker.current:
        image_worker.current.stop()
    if download_manager:
        download_manager.stop()
    task_service.current = None
    TASK_LOGS.flush()


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
    app.add_middleware(LoginMiddleware)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    from desync_archival.api import routers

    for router in routers.ALL:
        app.include_router(router)

    @app.get("/api/documents/{key}/file")
    def open_document(key: str) -> Response:
        return serve_document(key)

    @app.post("/api/preview/stl")
    async def preview_stl(request: Request) -> Response:
        try:
            body = schemas.StlPreviewBody.model_validate_json(await request.body())
        except ValidationError:
            return JSONResponse({"error": "A source_type of file or archive_member and a numeric id are required."}, 400)
        return await run_in_threadpool(serve_stl_preview, body.source_type, body.id)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "DELETE"])
    def api_not_found(request: Request) -> Response:
        return JSONResponse({"error": f"Route not found: {request.method} {request.url.path}"}, 404)

    # Says only that the process answers, so it's safe to leave open for a container or monitor.
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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


def check_network_ready(host: str) -> None:
    """Reaching beyond loopback needs a password and TLS, or the app refuses to start."""
    if app_config.login_required() and not login.has_password():
        raise app_config.SettingsError(
            "Login is required, and no password is set. Run: uv run python -m desync_archival.dbtool set-password"
        )
    if app_config.is_loopback(host):
        return
    tls = app_config.tls_files()
    if not tls or not all(path.is_file() for path in tls):
        raise app_config.SettingsError(f"Refusing to bind to {host} without DESYNC_TLS_CERT and DESYNC_TLS_KEY files.")


def take_instance_lock() -> None:
    """Refuses a second copy on one data folder.

    Startup runs before uvicorn binds the port, so without this a second copy marks live jobs as
    interrupted and starts its own workers, and only then fails on the port. It lives here and not
    in startup because tests start the app many times in one process.

    Raises:
        SettingsError: Another copy holds the lock.
    """
    global _instance_lock
    folder = app_config.data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / "app.lock", "w", encoding="utf-8")  # noqa: SIM115 - held until the process ends
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise app_config.SettingsError(f"The app is already running on {folder}.") from None
    _instance_lock = handle


def release_instance_lock() -> None:
    global _instance_lock
    if _instance_lock:
        _instance_lock.close()
        _instance_lock = None


def main() -> None:
    take_instance_lock()
    host, port = app_config.host(), app_config.port()
    check_network_ready(host)
    tls = app_config.tls_files()
    scheme = "https" if tls else "http"
    logger.info(f"[Server] Starting Web UI and API at {scheme}://{host}:{port}")
    uvicorn.run(
        app, host=host, port=port, log_config=None, access_log=False, timeout_graceful_shutdown=5,
        ssl_certfile=str(tls[0]) if tls else None, ssl_keyfile=str(tls[1]) if tls else None,
    )


if __name__ == "__main__":
    # Run this as the imported module, so the routers and main() share one copy of the managers.
    from desync_archival.api import server as imported_server  # noqa: PLW0406

    imported_server.main()
