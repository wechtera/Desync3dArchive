"""The catalog by model and group, and tags."""

import logging
import sqlite3
from typing import Any

from desync_archival.services import (
    audit,
    catalog_browse,
    tags,
)
from desync_archival.services.logger import (
    log_layer,
)


def handle_get_models(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    """GET /api/catalog-models. Pages real models, with search, tag and creator filters."""
    query = params or {}
    try:
        page = int((query.get("page") or ["1"])[0])
    except ValueError:
        page = 1
    return catalog_browse.list_models(
        conn, (query.get("q") or [""])[0].strip(), page, (query.get("tag") or [""])[0], (query.get("creator") or [""])[0],
    ), 200


def handle_get_catalog_model(
    conn: sqlite3.Connection,
    model_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    found = catalog_browse.model_detail(conn, model_id)
    return (found, 200) if found else ({"success": False, "error": "Model not found."}, 404)


def handle_post_catalog_model_stash(
    conn: sqlite3.Connection,
    model_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    try:
        result = catalog_browse.stash_selection(conn, model_id, str(data.get("scale") or ""), str(data.get("support") or ""))
    except ValueError as e:
        return {"success": False, "error": str(e)}, 409
    log_layer("storage", logging.INFO, f"Model #{model_id}: stashed {result['added']} of {result['matched']} artifacts")
    return result, 200


def handle_get_model_group(
    conn: sqlite3.Connection,
    group_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    found = catalog_browse.group_detail(conn, group_id)
    return (found, 200) if found else ({"success": False, "error": "Group not found."}, 404)


def _tag_action(action) -> tuple[dict[str, Any], int]:
    try:
        return {"success": True, "result": action()}, 200
    except tags.TagRefused as e:
        return {"success": False, "error": str(e)}, 400


def handle_get_tags(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    query = params or {}
    limit = tags.SUGGESTION_LIMIT if (query.get("hints") or [""])[0] else tags.PAGE_LIMIT
    return tags.listing(conn, (query.get("q") or [""])[0], limit), 200


def _audit_tag_change(conn: sqlite3.Connection, outcome: tuple[dict[str, Any], int], action: str, detail: dict) -> tuple[dict[str, Any], int]:
    # A refused change altered nothing, so it doesn't belong in the history.
    if outcome[0]["success"]:
        audit.record(conn, action, False, detail)
    return outcome


def handle_post_tags(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    name, action = str(data.get("name") or ""), str(data.get("action") or "")
    if action == "create":
        return _tag_action(lambda: tags.create(conn, name))
    if action == "rename":
        new_name = str(data.get("new_name") or "")
        return _audit_tag_change(conn, _tag_action(lambda: tags.rename(conn, name, new_name)), "tag_rename", {"tag": name, "new_name": new_name})
    if action == "delete":
        return _audit_tag_change(conn, _tag_action(lambda: tags.delete(conn, name)), "tag_delete", {"tag": name})
    return {"success": False, "error": f"Unknown action '{action}'"}, 400


def handle_post_model_tags(
    conn: sqlite3.Connection,
    model_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    data = body or {}
    name = str(data.get("name") or "")
    if data.get("action") == "remove":
        return _tag_action(lambda: tags.remove_from_model(conn, model_id, name))
    return _tag_action(lambda: tags.add_to_model(conn, model_id, name))


def handle_post_group_tags(
    conn: sqlite3.Connection,
    group_id: int,
    download_manager: Any = None,
    body: dict | None = None,
    params: dict | None = None,
) -> tuple[dict[str, Any], int]:
    return _tag_action(lambda: tags.add_to_group(conn, group_id, str((body or {}).get("name") or "")))
