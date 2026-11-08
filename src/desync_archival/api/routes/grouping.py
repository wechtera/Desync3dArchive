"""LLM Grouping (E54): export units, check an answer, apply or undo it, and decide suggestions."""

import logging
from typing import Any

from desync_archival.api.routing import Body, Conn, Params
from desync_archival.services import (
    grouping_apply,
    grouping_export,
    grouping_screen,
    grouping_suggestions,
)
from desync_archival.services.grouping_apply import ApplyRefused
from desync_archival.services.grouping_export import ExportRefused
from desync_archival.services.grouping_units import UnitError
from desync_archival.services.logger import log_layer

Result = tuple[Any, int] | tuple[Any, int, dict[str, str]]


def _refused(e: Exception, status: int = 409) -> tuple[dict[str, Any], int]:
    return {"success": False, "error": str(e)}, status


def handle_get_grouping(conn: Conn) -> Result:
    return grouping_screen.overview(conn), 200


def handle_get_grouping_preview(conn: Conn, params: Params) -> Result:
    try:
        lines = int(params.get("batch_lines", [grouping_export.DEFAULT_BATCH_LINES])[0])
    except ValueError:
        return _refused(ValueError("Batch size must be a whole number"), 400)
    try:
        return grouping_export.preview(
            conn, params.get("mode", ["delta"])[0], params.get("scope", ["both"])[0],
            params.get("include_reviewed", ["0"])[0] == "1", lines, params.get("unit_key", [""])[0],
        ), 200
    except ExportRefused as e:
        return _refused(e, 400)


def handle_post_grouping_export(conn: Conn, body: Body) -> Result:
    try:
        export_key, blob, summary = grouping_export.build(
            conn, body.get("mode", "delta"), body.get("scope", "both"), bool(body.get("include_reviewed")),
            int(body.get("batch_lines", grouping_export.DEFAULT_BATCH_LINES)), body.get("unit_key", ""),
        )
    except ExportRefused as e:
        return _refused(e, 400)
    log_layer("storage", logging.INFO, f"LLM grouping export {export_key}: {summary['units']} units in {summary['batches']} batches")
    return blob, 200, {
        "Content-Type": "application/zip",
        "Content-Disposition": f'attachment; filename="{export_key}.zip"',
        "X-Export-Id": export_key,
    }


def handle_post_grouping_check(conn: Conn, body: Body) -> Result:
    report = grouping_screen.check_upload(conn, body.get("text", ""), str(body.get("name", ""))[:200])
    log_layer("storage", logging.INFO, f"LLM grouping answer checked: {report['counts']}, import {report.get('import_id')}")
    return report, 200


def handle_post_grouping_apply(conn: Conn, import_id: int, body: Body) -> Result:
    choices = {str(unit): str(choice) for unit, choice in (body.get("choices") or {}).items()}
    try:
        result = grouping_apply.apply(conn, import_id, choices)
    except ApplyRefused as e:
        return _refused(e)
    log_layer("storage", logging.INFO, f"LLM grouping import {import_id} applied: {len(result['applied'])} units, "
              f"{len(result['suggested'])} waiting, {len(result['skipped'])} skipped, {len(result['refused'])} refused")
    return result, 200


def handle_post_grouping_review(conn: Conn, import_id: int, body: Body) -> Result:
    try:
        result = grouping_apply.review_now(conn, import_id, str(body.get("unit", "")))
    except ApplyRefused as e:
        return _refused(e)
    log_layer("storage", logging.INFO, f"LLM grouping import {import_id}: {result['unit']} sent to review before confirming")
    return result, 200


def handle_post_grouping_discard(conn: Conn, import_id: int) -> Result:
    try:
        return grouping_screen.discard(conn, import_id), 200
    except ApplyRefused as e:
        return _refused(e)


def handle_post_grouping_undo(conn: Conn, import_id: int) -> Result:
    try:
        result = grouping_apply.undo(conn, import_id)
    except ApplyRefused as e:
        return _refused(e)
    log_layer("storage", logging.INFO, f"LLM grouping import {import_id} undone: {len(result['restored'])} restored, {len(result['left'])} left")
    return result, 200


def handle_post_grouping_tags(conn: Conn, import_id: int, body: Body) -> Result:
    try:
        return grouping_suggestions.approve_tags(conn, import_id, [str(t) for t in body.get("approved", [])]), 200
    except (ApplyRefused, ValueError) as e:
        return _refused(e, 400)


def handle_get_grouping_suggestion(conn: Conn, suggestion_id: int) -> Result:
    try:
        return grouping_suggestions.view(conn, suggestion_id), 200
    except (ApplyRefused, UnitError) as e:
        return _refused(e)


def handle_post_grouping_accept(conn: Conn, suggestion_id: int, body: Body) -> Result:
    try:
        return grouping_suggestions.accept(conn, suggestion_id, body.get("cards"), body.get("pictures") or {}), 200
    except (ApplyRefused, UnitError) as e:
        return _refused(e)


def handle_post_grouping_reject(conn: Conn, suggestion_id: int) -> Result:
    try:
        return grouping_suggestions.reject(conn, suggestion_id), 200
    except ApplyRefused as e:
        return _refused(e)
