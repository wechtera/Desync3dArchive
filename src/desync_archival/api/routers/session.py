"""App login: separate from MMF sign-in, and only used in LAN mode."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from desync_archival import settings as app_config
from desync_archival.api import schemas
from desync_archival.services import login, secret_store
from desync_archival.services.logger import logger

router = APIRouter()


def client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_page() -> Response:
    return FileResponse(app_config.web_dir() / "login.html")


@router.get("/api/session")
def session_status(request: Request) -> Response:
    """GET /api/session. Public, and reveals only whether this browser is signed in."""
    token = request.cookies.get(login.COOKIE_NAME)
    return JSONResponse({"login_required": app_config.login_required(), "authenticated": login.session_valid(token)})


@router.post("/api/login")
async def log_in(request: Request) -> Response:
    """POST /api/login. Checks the lockout before hashing, so a locked client can't burn CPU."""
    client = client_id(request)
    if login.is_locked_out(client):
        return JSONResponse({"error": "Too many attempts. Try again later."}, 429)

    try:
        body = schemas.LoginBody.model_validate_json(await request.body())
    except ValidationError:
        return JSONResponse({"error": "A password is required."}, 400)

    # Counted before the result, because scrypt takes long enough for parallel tries to overlap.
    if not login.begin_attempt(client):
        return JSONResponse({"error": "Too many attempts. Try again later."}, 429)

    # scrypt takes real time, so it stays off the event loop.
    try:
        matched = await run_in_threadpool(login.verify_password, body.password)
    except secret_store.SecretsError as e:
        # Not a wrong password, so it doesn't count toward the lockout.
        login.forget_attempt(client)
        logger.error(f"Login unavailable: {e}")
        return JSONResponse({"error": "The password can't be read right now. See the server log."}, 503)
    if not matched:
        logger.warning(f"Failed login from {client}")
        return JSONResponse({"error": "Wrong password."}, 401)

    login.clear_failures(client)
    logger.info(f"Login from {client}")
    response = JSONResponse({"success": True})
    response.set_cookie(
        login.COOKIE_NAME, login.create_session(), max_age=login.SESSION_SECONDS,
        path="/", httponly=True, secure=True, samesite="strict",
    )
    return response


@router.post("/api/logout")
def log_out(request: Request) -> Response:
    """POST /api/logout. Ends the session on the server as well as clearing the cookie."""
    login.end_session(request.cookies.get(login.COOKIE_NAME))
    response = JSONResponse({"success": True})
    response.delete_cookie(login.COOKIE_NAME, path="/", httponly=True, secure=True, samesite="strict")
    return response
