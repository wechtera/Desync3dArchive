"""How a handler becomes a route. A handler lists only what it needs, and these dependencies supply it."""

import json
import re
import sqlite3
import urllib.parse
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from functools import wraps
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from desync_archival.api import schemas
from desync_archival.services.logger import logger

GENERIC_ERROR = "Internal server error"
# A worker thread inherits this, and a handler's error names the request that caused it.
_current_route: ContextVar[str] = ContextVar("current_route", default="a route")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class RefusedRequest(Exception):
    """The request can't reach its handler. The message goes back with a 400."""


class BadParameter(ValueError):
    """A query value a route can't use. It answers 400, so a typo isn't logged as a server fault."""


def paging(params: dict, default: int, maximum: int) -> tuple[int, int]:
    """Reads limit and offset, held to the ranges a query can use.

    Raises:
        BadParameter: Either one isn't a whole number.
    """
    try:
        limit = int(params.get("limit", [default])[0])
        offset = int(params.get("offset", [0])[0])
    except ValueError:
        raise BadParameter("limit and offset must be whole numbers") from None
    return max(1, min(maximum, limit)), max(0, offset)


def _connection(request: Request) -> Iterator[sqlite3.Connection]:
    conn = request.app.state.open_database()
    try:
        yield conn
    finally:
        conn.close()


def _download_manager(request: Request) -> Any:
    return request.app.state.download_manager()


def _task_manager(request: Request) -> Any:
    return request.app.state.task_manager()


def _params(request: Request) -> dict[str, list[str]]:
    params = urllib.parse.parse_qs(request.url.query)
    # A value in the path answers as the query parameter a shared handler reads.
    for name, value in request.path_params.items():
        params.setdefault(name, [str(value)])
    return params


def _body(request: Request) -> dict:
    return getattr(request.state, "body", {})


Conn = Annotated[sqlite3.Connection, Depends(_connection)]
Downloads = Annotated[Any, Depends(_download_manager)]
Tasks = Annotated[Any, Depends(_task_manager)]
Params = Annotated[dict, Depends(_params)]
Body = Annotated[dict, Depends(_body)]


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


def validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "body"
    return f"Invalid value for {field}: {first['msg']}"


def name_route(request: Request) -> None:
    """Names the request in any error a handler logs, including from its worker threads."""
    _current_route.set(f"{request.method} {request.url.path}")


def current_route() -> str:
    return _current_route.get()


def _checked_body(model: type[BaseModel] | None) -> Callable[[Request], Awaitable[None]]:
    """Reads the body before anything else runs, so a bad one never opens a connection."""
    async def check(request: Request) -> None:
        name_route(request)
        request.state.body = {}
        if request.method == "GET":
            return
        try:
            body = parse_body(await request.body())
        except ValueError:
            raise RefusedRequest("Request body isn't valid JSON") from None
        if not isinstance(body, dict):
            raise RefusedRequest("Request body must be a JSON object")
        if model is not None:
            try:
                body = model.model_validate(body).model_dump(exclude_unset=True)
            except ValidationError as e:
                raise RefusedRequest(validation_message(e)) from None
        request.state.body = body
    return check


def _respond(handler: Callable) -> Callable:
    """Turns a handler's result into a response, and an unhandled error into a plain 500."""
    @wraps(handler)
    def route(*args: Any, **kwargs: Any) -> Response:
        try:
            result = handler(*args, **kwargs)
        except BadParameter as e:
            result = ({"error": str(e)}, 400)
        except Exception:  # noqa: BLE001
            logger.exception(f"Unhandled error in {_current_route.get()}")
            result = ({"error": GENERIC_ERROR}, 500)
        if isinstance(result, tuple) and len(result) == 3:
            return build_response(result[0], result[1], extra_headers=result[2])
        if isinstance(result, tuple) and len(result) == 2:
            return build_response(result[0], result[1])
        return build_response(result, 200)
    return route


def add_route(
    router: APIRouter,
    methods: str | tuple[str, ...],
    paths: str | tuple[str, ...],
    handler: Callable,
    model: type[BaseModel] | None = None,
) -> None:
    method_list = [methods] if isinstance(methods, str) else list(methods)
    endpoint = _respond(handler)
    for path in (paths,) if isinstance(paths, str) else paths:
        router.add_api_route(
            path, endpoint, methods=method_list, response_model=None, dependencies=[Depends(_checked_body(model))],
        )
