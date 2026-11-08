"""Builds the zip an owner runs through an LLM: the prompt, the answer format and the batches.

Delta leaves out units whose files haven't changed since an answer was applied, suggested or
rejected for them. A unit exported but never answered goes out again, so a lost zip costs nothing.
Units a person reviewed stay out unless asked for, because an answer would only suggest for them.
"""

import io
import json
import secrets
import sqlite3
import zipfile
from datetime import UTC, datetime
from typing import Any

from desync_archival.db.queries import grouping as queries
from desync_archival.services import grouping_contract, grouping_units
from desync_archival.services.grouping_units import Unit, UnitError

MODES = ("delta", "full", "single")
SCOPES = ("archives", "libraries", "both")
DEFAULT_BATCH_LINES = 2000
MIN_BATCH_LINES = 200
MAX_BATCH_LINES = 20000


class ExportRefused(ValueError):
    """Nothing was exported. The message is shown to the user."""


def _decided(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {r[0]: r[2] for r in conn.execute(queries.SELECT_UNIT_STATE)}


def choose(
    conn: sqlite3.Connection, mode: str, scope: str, include_reviewed: bool = False, unit_key: str = "",
    unit_keys: list[str] | None = None,
) -> tuple[list[Unit], list[dict[str, str]]]:
    """The units an export would hold, and the ones left out with why. Writes nothing.

    A single export takes one unit_key, or a list in unit_keys for a chosen set such as a test sample.
    """
    if mode not in MODES:
        raise ExportRefused(f"Mode must be one of {', '.join(MODES)}")
    if scope not in SCOPES:
        raise ExportRefused(f"Scope must be one of {', '.join(SCOPES)}")
    chosen_keys = list(unit_keys or ([unit_key] if unit_key else []))
    keys = chosen_keys if mode == "single" else grouping_units.all_unit_keys(conn, scope)
    if mode == "single" and not chosen_keys:
        raise ExportRefused("A single export needs a unit")
    decided = _decided(conn)
    chosen, left_out = [], []
    for key in keys:
        try:
            unit = grouping_units.build(conn, key)
        except UnitError as e:
            if mode == "single":
                raise ExportRefused(str(e)) from None
            left_out.append({"unit": key, "why": str(e)})
            continue
        if unit.reviewed and not include_reviewed:
            left_out.append({"unit": key, "why": "Reviewed by a person"})
            continue
        if mode == "delta" and decided.get(key) == unit.fingerprint:
            left_out.append({"unit": key, "why": "No change since its last answer"})
            continue
        chosen.append(unit)
    return chosen, left_out


def batches(units: list[Unit], batch_lines: int) -> list[list[Unit]]:
    """Whole units only. A unit bigger than the limit goes out alone."""
    packed: list[list[Unit]] = []
    current: list[Unit] = []
    used = 0
    for unit in units:
        if current and used + unit.lines > batch_lines:
            packed.append(current)
            current, used = [], 0
        current.append(unit)
        used += unit.lines
    if current:
        packed.append(current)
    return packed


def preview(conn: sqlite3.Connection, mode: str, scope: str, include_reviewed: bool, batch_lines: int, unit_key: str = "") -> dict[str, Any]:
    """What an export would hold, for the screen. Writes nothing."""
    batch_lines = _checked_lines(batch_lines)
    units, left_out = choose(conn, mode, scope, include_reviewed, unit_key)
    packed = batches(units, batch_lines)
    return {
        "units": len(units), "batches": len(packed),
        "oversized": [u.key for u in units if u.lines > batch_lines],
        "left_out": left_out,
        "files": sum(len(u.payload["printables"]) for u in units),
    }


def _checked_lines(batch_lines: int) -> int:
    if not MIN_BATCH_LINES <= int(batch_lines) <= MAX_BATCH_LINES:
        raise ExportRefused(f"Batch size must be between {MIN_BATCH_LINES} and {MAX_BATCH_LINES} lines")
    return int(batch_lines)


def _export_key() -> str:
    return f"e-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _batch_file(export_key: str, number: int, total: int, units: list[Unit]) -> str:
    body = {
        "export_id": export_key,
        "batch": number,
        "of": total,
        "instructions": "Follow prompt.md. Answer with one JSON object matching answer-format.json.",
        "units": [u.payload for u in units],
    }
    return json.dumps(body, indent=1, ensure_ascii=False)


def build(
    conn: sqlite3.Connection, mode: str = "delta", scope: str = "both", include_reviewed: bool = False,
    batch_lines: int = DEFAULT_BATCH_LINES, unit_key: str = "", unit_keys: list[str] | None = None,
) -> tuple[str, bytes, dict[str, Any]]:
    """Records the export and returns its key, the zip, and a summary.

    Raises:
        ExportRefused: Nothing to export, or a setting is out of range.
    """
    batch_lines = _checked_lines(batch_lines)
    units, left_out = choose(conn, mode, scope, include_reviewed, unit_key, unit_keys)
    if not units:
        raise ExportRefused("Nothing to export. Every unit in scope is unchanged since its last answer, or reviewed.")
    packed = batches(units, batch_lines)
    export_key = _export_key()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", grouping_contract.README)
        archive.writestr("prompt.md", grouping_contract.PROMPT)
        archive.writestr("answer-format.json", json.dumps(grouping_contract.answer_schema(), indent=1))
        if any(u.payload["tags_allowed"] for u in units):
            archive.writestr("tags.txt", "\n".join(r[0] for r in conn.execute(queries.SELECT_TAGS)) + "\n")
        for number, batch in enumerate(packed, 1):
            archive.writestr(f"batch-{number:02d}.json", _batch_file(export_key, number, len(packed), batch))

    known = {r[0] for r in conn.execute(queries.SELECT_UNIT_STATE)}
    with conn:
        export_id = conn.execute(queries.INSERT_EXPORT, (
            export_key, mode, scope, int(include_reviewed), batch_lines, len(units), len(packed),
        )).lastrowid
        for number, batch in enumerate(packed, 1):
            for unit in batch:
                conn.execute(queries.INSERT_EXPORT_UNIT, (export_id, unit.key, number, unit.fingerprint, grouping_units.ids_json(unit)))
                if unit.key in known:
                    conn.execute(queries.UPDATE_UNIT_EXPORTED, (export_id, unit.fingerprint, unit.key))
                else:
                    conn.execute(queries.INSERT_UNIT, (unit.key, unit.kind, export_id, unit.fingerprint))
    summary = {
        "export_id": export_key, "units": len(units), "batches": len(packed),
        "oversized": [u.key for u in units if u.lines > batch_lines], "left_out": len(left_out),
    }
    return export_key, buffer.getvalue(), summary
