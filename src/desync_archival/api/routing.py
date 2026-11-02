"""Shared plumbing for domain routers: request parsing, validation and response building."""

import sqlite3
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from desync_archival.services.logger import logger

GENERIC_ERROR = "Internal server error"


@dataclass
class RouteContext:
    """Everything one request hands its handler, so handlers keep the old four-argument shape."""
    conn: sqlite3.Connection
    download_manager: Any
    task_manager: Any
    body: dict
    params: dict
    path: dict


def plain(handler: Callable) -> Callable:
    return lambda ctx: handler(ctx.conn, ctx.download_manager, ctx.body, ctx.params)


def with_tasks(handler: Callable) -> Callable:
    return lambda ctx: handler(ctx.conn, ctx.download_manager, ctx.body, ctx.params, task_manager=ctx.task_manager)


def with_path_id(handler: Callable, name: str, tasks: bool = False) -> Callable:
    """Passes a path value as the handler's second argument."""
    def call(ctx: RouteContext) -> Any:
        kwargs = {"task_manager": ctx.task_manager} if tasks else {}
        return handler(ctx.conn, ctx.path[name], ctx.download_manager, ctx.body, ctx.params, **kwargs)
    return call


def path_to_param(handler: Callable, name: str, param: str, tasks: bool = False) -> Callable:
    """Presents a path value as the query parameter a shared handler reads."""
    def call(ctx: RouteContext) -> Any:
        ctx.params.setdefault(param, [str(ctx.path[name])])
        return (with_tasks if tasks else plain)(handler)(ctx)
    return call


def validation_message(error: ValidationError) -> str:
    first = error.errors()[0]
    field = ".".join(str(part) for part in first["loc"]) or "body"
    return f"Invalid value for {field}: {first['msg']}"


def add_route(
    router: APIRouter,
    methods: str | tuple[str, ...],
    paths: str | tuple[str, ...],
    call: Callable[[RouteContext], Any],
    model: type[BaseModel] | None = None,
) -> None:
    method_list = [methods] if isinstance(methods, str) else list(methods)

    async def endpoint(request: Request) -> Response:
        from desync_archival.api import server

        try:
            body = server.parse_body(await request.body()) if request.method != "GET" else {}
        except ValueError:
            return JSONResponse({"error": "Request body isn't valid JSON"}, 400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, 400)
        if model is not None and request.method != "GET":
            try:
                body = model.model_validate(body).model_dump(exclude_unset=True)
            except ValidationError as e:
                return JSONResponse({"error": validation_message(e)}, 400)
        params = urllib.parse.parse_qs(request.url.query)
        path = dict(request.path_params)
        return await run_in_threadpool(run_call, call, body, params, path, request)

    for path in (paths,) if isinstance(paths, str) else paths:
        router.add_api_route(path, endpoint, methods=method_list)


def run_call(call: Callable, body: dict, params: dict, path: dict, request: Request) -> Response:
    """Blocking route work, run in the threadpool."""
    from desync_archival.api import server

    conn = server.get_db()
    try:
        context = RouteContext(conn, server.download_manager, server.task_manager, body, params, path)
        result = call(context)
    except Exception:  # noqa: BLE001
        logger.exception(f"Unhandled error in {request.method} {request.url.path}")
        result = ({"error": GENERIC_ERROR}, 500)
    finally:
        conn.close()

    if isinstance(result, tuple) and len(result) == 3:
        return server.build_response(result[0], result[1], extra_headers=result[2])
    if isinstance(result, tuple) and len(result) == 2:
        return server.build_response(result[0], result[1])
    return server.build_response(result, 200)
