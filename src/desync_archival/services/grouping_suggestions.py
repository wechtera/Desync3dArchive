"""Answers waiting for a person, shown in the review editor they already use.

A suggestion opens with the LLM's cards filled in and its reasons on them, and a picture choice on
each card. Accepting saves the person's cards, edited or not, as reviewed by a person, and any
picture they chose is locked so nothing automatic replaces it. Rejecting keeps today's grouping,
and the unit stays out of later deltas until its files change.
"""

import json
import sqlite3
from typing import Any

from desync_archival.db.queries import grouping as queries
from desync_archival.services import (
    archive_review,
    audit,
    catalog_review,
    grouping_units,
    tags,
)
from desync_archival.services.grouping_apply import (
    ApplyRefused,
    _shown_paths,
    save_cards,
)
from desync_archival.services.grouping_units import ARCHIVE, COVER
from desync_archival.services.library_review import ReviewError


def _load(conn: sqlite3.Connection, suggestion_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_SUGGESTION, (suggestion_id,)).fetchone()
    if not row:
        raise ApplyRefused("That suggestion doesn't exist.")
    if row["status"] != "waiting":
        raise ApplyRefused(f"That suggestion was already {row['status']}.")
    return row


def waiting(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every suggestion waiting, low ones last, with what a person needs to pick one up."""
    conn.row_factory = sqlite3.Row
    items = []
    for row in conn.execute(queries.SELECT_WAITING_SUGGESTIONS).fetchall():
        payload = json.loads(row["cards_json"])
        try:
            unit = grouping_units.build(conn, row["unit_key"]).payload
            title, creator = unit["collection"], unit["creator"]
        except grouping_units.UnitError:
            title, creator = row["unit_key"], ""
        items.append({
            "id": row["id"], "import_id": row["import_id"], "unit": row["unit_key"], "title": title, "creator": creator,
            "bucket": row["bucket"], "reasons": json.loads(row["reasons_json"]), "created_at": row["created_at"],
            "kind": "pictures" if not payload.get("cards") else "grouping",
            "models": len(payload.get("cards") or []), "pictures": len(payload.get("pictures") or []),
        })
    return items


def _images(unit: grouping_units.Unit) -> list[dict[str, Any]]:
    """Every picture in the unit, for the picker. Only ones held here can be shown."""
    return [
        {"id": short, "name": meta["path"].rpartition("/")[2], "path": meta["path"],
         "url": f"/{meta['preview']}" if meta.get("available") and meta.get("preview") else ""}
        for short, meta in unit.ids.items() if meta["kind"] == "image"
    ]


def view(conn: sqlite3.Connection, suggestion_id: int) -> dict[str, Any]:
    """The unit's review screen, with the LLM's cards in place of today's."""
    row = _load(conn, suggestion_id)
    payload = json.loads(row["cards_json"])
    unit_key = row["unit_key"]
    kind, number, release = grouping_units.parse_key(unit_key)
    try:
        base = archive_review.view(conn, number) if kind == ARCHIVE else catalog_review.view(conn, number, release)
        unit = grouping_units.build(conn, unit_key)
    except (ReviewError, grouping_units.UnitError) as e:
        raise ApplyRefused(str(e)) from e
    plan = payload.get("plan") or {}
    if plan and plan.get("fingerprint") != unit.fingerprint:
        raise ApplyRefused("Its files changed after the LLM answered, so this suggestion no longer fits. Export it again.")

    by_short = {meta.get("preview"): short for short, meta in unit.ids.items() if meta["kind"] == "image"}
    cards = payload.get("cards") or []
    for card, model in zip(cards, plan.get("models", []), strict=False):
        card["flags"] = [f"Suggested: {model['reason']}"] if model.get("reason") else ["Suggested"]
        image = model.get("image") or {}
        card["picture"] = COVER if image.get("cover") else image.get("short", "")
        card["picture_alternates"] = [a.get("short") for a in model.get("alternates", []) if a and a.get("short")]
        card["picture_confidence"] = model.get("image_confidence", "low")
    pictures = [
        {**p, "image": p["image"].get("short") if isinstance(p.get("image"), dict) else p.get("image")}
        for p in payload.get("pictures") or []
    ]
    for item in pictures:
        current = conn.execute(queries.SELECT_MODEL_IMAGE, (item["model_id"],)).fetchone()
        item["current"] = by_short.get(current[0] if current else None, "")
    return {
        **base, "cards": cards or base["cards"], "changed": True,
        "suggestion": {
            "id": suggestion_id, "bucket": row["bucket"], "reasons": json.loads(row["reasons_json"]),
            "kind": "pictures" if not cards else "grouping", "images": _images(unit), "pictures": pictures,
            "unplaced_note": "Unplaced files sit on a skipped card. Move them to a model, or leave them skipped.",
        },
    }


def _set_pictures(conn: sqlite3.Connection, unit: grouping_units.Unit, collection: int | None,
                  choices: list[tuple[int, str]]) -> tuple[int, int]:
    """A person's choices, locked. Returns (set now, waiting for the NAS).

    A picture not held here yet, like any library image, is fetched by a picture job that locks it
    when it lands. An empty choice leaves the model as it is.
    """
    set_now = waiting_for_nas = 0
    for model_id, short in choices:
        if not short:
            continue
        if short == COVER:
            conn.execute(queries.SKIP_MODEL_PICTURE_JOBS, (model_id,))
            conn.execute(queries.LOCK_MODEL_PICTURE, (None, model_id))
            set_now += 1
            continue
        meta = unit.ids.get(short)
        if not meta or meta["kind"] != "image":
            raise ApplyRefused(f"{short} isn't a picture of this unit.")
        conn.execute(queries.SKIP_MODEL_PICTURE_JOBS, (model_id,))
        if meta.get("available") and meta.get("preview"):
            conn.execute(queries.LOCK_MODEL_PICTURE, (meta["preview"], model_id))
            set_now += 1
        else:
            kind = "extract" if meta.get("member") else "copy"
            conn.execute(queries.INSERT_PICTURE_JOB, (kind, model_id, None, collection, json.dumps({**meta, "source": "owner"})))
            waiting_for_nas += 1
    return set_now, waiting_for_nas


def accept(conn: sqlite3.Connection, suggestion_id: int, cards: list[dict] | None, pictures: dict[str, str] | None = None) -> dict[str, Any]:
    """Saves the person's cards as reviewed, and their picture choices as theirs.

    `cards` are the editor's cards, edited or not; None keeps the grouping as it is, for a pictures
    suggestion. `pictures` maps a card's first path, or a model ID for a pictures suggestion, to an
    i-ID or "cover".

    Raises:
        ApplyRefused: The suggestion is gone, the unit changed, or the save refused the cards.
    """
    row = _load(conn, suggestion_id)
    unit_key = row["unit_key"]
    unit = grouping_units.build(conn, unit_key)
    payload = json.loads(row["cards_json"])
    plan = payload.get("plan") or {}
    if plan and plan.get("fingerprint") != unit.fingerprint:
        raise ApplyRefused("Its files changed after the LLM answered, so this suggestion no longer fits. Export it again.")
    pictures = pictures or {}
    # Checked before anything is saved, so a bad choice can't leave the cards half accepted.
    for short in pictures.values():
        if short and short != COVER and (unit.ids.get(short) or {}).get("kind") != "image":
            raise ApplyRefused(f"{short} isn't a picture of this unit.")

    if cards is not None:
        save_cards(conn, unit_key, cards, True)
        paths = {v: k for k, v in _shown_paths(conn, unit_key).items()}
        choices = []
        for card in cards:
            first = (card.get("paths") or [""])[0]
            artifact = paths.get(first)
            if artifact is not None and first in pictures:
                choices.append((conn.execute(queries.SELECT_ARTIFACT_BY_ID, (artifact,)).fetchone()[0], pictures[first]))
    else:
        choices = [(int(model_id), short) for model_id, short in pictures.items() if str(model_id).isdigit()]

    kind, number, _release = grouping_units.parse_key(unit_key)
    with conn:
        chosen, queued = _set_pictures(conn, unit, number if kind == ARCHIVE else None, choices)
        conn.execute(queries.DECIDE_SUGGESTION, ("accepted", suggestion_id))
        if cards is not None:
            conn.execute(queries.UPDATE_UNIT_DECIDED, ("applied", unit.fingerprint, unit_key))
    result = {"success": True, "suggestion": suggestion_id, "unit": unit_key, "pictures": chosen, "pictures_waiting": queued}
    audit.record(conn, "grouping_accept", False, result)
    return result


def reject(conn: sqlite3.Connection, suggestion_id: int) -> dict[str, Any]:
    """Keeps today's grouping. The unit stays out of deltas until its files change."""
    row = _load(conn, suggestion_id)
    payload = json.loads(row["cards_json"])
    with conn:
        conn.execute(queries.DECIDE_SUGGESTION, ("rejected", suggestion_id))
        if payload.get("cards"):
            fingerprint = (payload.get("plan") or {}).get("fingerprint")
            conn.execute(queries.UPDATE_UNIT_DECIDED, ("rejected", fingerprint, row["unit_key"]))
    result = {"success": True, "suggestion": suggestion_id, "unit": row["unit_key"]}
    audit.record(conn, "grouping_reject", False, result)
    return result


def approve_tags(conn: sqlite3.Connection, import_id: int, approved: list[str]) -> dict[str, Any]:
    """Creates the suggested tags a person approved, then adds each to the models that asked for it."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_IMPORT, (import_id,)).fetchone()
    if not row:
        raise ApplyRefused("That import doesn't exist.")
    plans = json.loads(row["answer_json"])
    # Only units this import applied: one still waiting may be grouped differently once reviewed.
    applied = {s["unit_key"] for s in conn.execute(queries.SELECT_SNAPSHOTS, (import_id,)) if not s["restored_at"]}
    created = [tags.create(conn, name) for name in approved]
    added = 0
    for key, plan in plans.items():
        if key not in applied:
            continue
        for model in plan.get("models", []):
            wanted = set(model.get("wanted_tags", [])) & set(created)
            if not wanted or not model.get("artifacts"):
                continue
            found = conn.execute(queries.SELECT_ARTIFACT_BY_ID, (model["artifacts"][0],)).fetchone()
            if not found:
                continue
            for tag in sorted(wanted):
                tags.add_to_model(conn, found[0], tag)
                added += 1
    result = {"success": True, "created": created, "added": added}
    audit.record(conn, "grouping_tags", False, result)
    return result
