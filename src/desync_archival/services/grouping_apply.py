"""Applies a checked answer, and undoes it.

Grouping goes through the same saves Archive Review and the catalog review use, which refuse
anything that doesn't place every file once. Before a unit changes, its whole state is
snapshotted: which model holds each file, every model's name, state and picture, its groups,
links, tags and review flag. Undo puts exactly that back, unless a person has edited the unit
since, in which case it leaves the unit alone and says so.

No file record is ever deleted. Files only move between models of the same unit, and a model
left empty keeps its row and redirects, so no ID dangles.
"""

import hashlib
import json
import sqlite3
from typing import Any

from desync_archival.db.queries import grouping as queries
from desync_archival.services import (
    archive_review,
    audit,
    catalog_review,
    grouping_units,
)
from desync_archival.services.grouping_answer import APPLY, SUGGEST
from desync_archival.services.grouping_units import ARCHIVE, COVER
from desync_archival.services.library_review import ReviewError

UNPLACED_CARD = "Unplaced files"
# Pictures that only stand in for one: a better match may replace these, and nothing else.
FALLBACK_SOURCES = (None, "group", "cover", "render")
OWNER = "owner"


class ApplyRefused(ValueError):
    """Nothing changed. The message is shown to the user."""


# A unit's state ------------------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    return [dict(r) for r in conn.execute(sql.format(marks=",".join("?" * len(ids))), ids)]


def state(conn: sqlite3.Connection, unit_key: str) -> dict[str, Any]:
    """Everything an apply can change in a unit, as plain data."""
    conn.row_factory = sqlite3.Row
    kind, number, release = grouping_units.parse_key(unit_key)
    if kind == ARCHIVE:
        artifacts = conn.execute(queries.SELECT_UNIT_ARCHIVE_ARTIFACTS, (number,)).fetchall()
        groups = conn.execute(queries.SELECT_ARCHIVE_GROUP_ROWS, (number,)).fetchall()
        review = conn.execute(queries.SELECT_REVIEW_ROW, (number,)).fetchone()
    else:
        prefix = f"{number}/{release}/"
        artifacts = conn.execute(queries.SELECT_UNIT_LIBRARY_ARTIFACTS, (number, prefix, prefix)).fetchall()
        groups = conn.execute(queries.SELECT_LIBRARY_GROUP_ROWS, (number, release)).fetchall()
        review = None
    model_ids = sorted({a["model_id"] for a in artifacts})
    return {
        "artifacts": {str(a["id"]): a["model_id"] for a in artifacts},
        "models": _rows(conn, queries.SELECT_MODEL_ROWS, model_ids),
        "links": [[r["model_id"], r["collection_id"]] for r in _rows(conn, queries.SELECT_LINKS, model_ids)],
        "groups": [
            {"name": g["name"], "image_path": g["image_path"], "image_source": g["image_source"],
             "members": [r[0] for r in conn.execute(queries.SELECT_GROUP_MEMBERS, (g["id"],))]}
            for g in groups
        ],
        "tags": [[r["model_id"], r["tag"]] for r in _rows(conn, queries.SELECT_TAG_ROWS, model_ids)],
        "reviewed": None if review is None else review["reviewed"],
    }


def state_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


# Building cards ------------------------------------------------------------------------------

def _shown_paths(conn: sqlite3.Connection, unit_key: str) -> dict[int, str]:
    """Each artifact's path as its review save names it."""
    conn.row_factory = sqlite3.Row
    kind, number, release = grouping_units.parse_key(unit_key)
    if kind == ARCHIVE:
        rows = conn.execute(archive_review.queries.SELECT_MODEL_ARTIFACTS_BY_MODEL_ID, (number,)).fetchall()
        return {r["id"]: archive_review._shown_path(number, r) for r in rows}
    rows = catalog_review._artifacts(conn, number, release)
    return {r["id"]: r["path"] for r in rows}


def _reuse(plan_models: list[dict], held_by: dict[str, int]) -> list[int | None]:
    """Each answer model keeps the ID of the model it takes most files from, so stash items and
    links stay with the same row. Each old ID goes to one new model at most."""
    overlaps = []
    for index, model in enumerate(plan_models):
        counts: dict[int, int] = {}
        for artifact in model["artifacts"]:
            old = held_by.get(str(artifact))
            if old is not None:
                counts[old] = counts.get(old, 0) + 1
        overlaps += [(n, index, old) for old, n in counts.items()]
    keys: list[int | None] = [None] * len(plan_models)
    used: set[int] = set()
    for _, index, old in sorted(overlaps, key=lambda o: (-o[0], o[1], o[2])):
        if keys[index] is None and old not in used:
            keys[index] = old
            used.add(old)
    return keys


def cards_for(conn: sqlite3.Connection, plan: dict[str, Any], before: dict[str, Any]) -> list[dict[str, Any]]:
    """The answer as review cards. Unplaced files go on a skipped card, so they stay listed."""
    paths = _shown_paths(conn, plan["unit_key"])
    categories = {m["id"]: m["category"] or "" for m in before["models"]}
    keys = _reuse(plan["models"], before["artifacts"])
    cards = [
        {
            "key": str(key) if key is not None else "", "name": model["name"], "category": categories.get(key, "") if key else "",
            "paths": [paths[a] for a in model["artifacts"]], "skip": False, "group": model["group"], "tags": [], "flags": [],
        }
        for model, key in zip(plan["models"], keys, strict=True)
    ]
    if plan["unplaced"]:
        cards.append({
            "key": "", "name": UNPLACED_CARD, "category": "", "paths": [paths[u["artifact"]] for u in plan["unplaced"]],
            "skip": True, "group": "", "tags": [], "flags": [],
        })
    return cards


def save_cards(conn: sqlite3.Connection, unit_key: str, cards: list[dict], reviewed: bool) -> None:
    kind, number, release = grouping_units.parse_key(unit_key)
    try:
        if kind == ARCHIVE:
            archive_review.save(conn, number, cards, reviewed)
        else:
            catalog_review.save(conn, number, release, cards)
    except ReviewError as e:
        raise ApplyRefused(str(e)) from e


# Pictures and tags ---------------------------------------------------------------------------

def _model_of(conn: sqlite3.Connection, artifact_id: int) -> int:
    return conn.execute(queries.SELECT_ARTIFACT_MODEL, (artifact_id,)).fetchone()[0]


def _picture(conn: sqlite3.Connection, plan: dict, unit_key: str, model_id: int, model: dict,
             before: dict[int, dict], held_back: list[dict]) -> None:
    """Applies a model's picture by 9A.4, or holds it back as a suggestion."""
    picture = model["image"]
    had = before.get(model_id, {})
    if had.get("image_locked") or had.get("image_source") == OWNER:
        return
    collection = None if not unit_key.startswith(f"{ARCHIVE}:") else grouping_units.parse_key(unit_key)[1]
    if picture is None:
        if model.get("render_artifact") and not had.get("image_path") and had.get("image_source") in (None, "render"):
            conn.execute(queries.INSERT_PICTURE_JOB, ("render", model_id, None, collection, json.dumps({"artifact": model["render_artifact"]})))
        return
    fallback = had.get("image_source") in FALLBACK_SOURCES and (had.get("image_source") is not None or not had.get("image_path"))
    if model["image_confidence"] != "high" or not fallback:
        if picture.get("preview") != had.get("image_path"):
            held_back.append({"model_id": model_id, "name": model["name"], "image": picture,
                              "alternates": model.get("alternates", []), "confidence": model["image_confidence"],
                              "reason": model.get("image_reason", "")})
        return
    if picture.get("cover"):
        conn.execute(queries.SET_MODEL_PICTURE, (None, COVER, model_id))
        return
    source = "group" if model["image_shared"] else "llm"
    if picture.get("available") and picture.get("preview"):
        conn.execute(queries.SET_MODEL_PICTURE, (picture["preview"], source, model_id))
    else:
        conn.execute(queries.INSERT_PICTURE_JOB, (
            "extract" if picture.get("member") else "copy", model_id, None, collection, json.dumps({**picture, "source": source}),
        ))


def _group_pictures(conn: sqlite3.Connection, plan: dict, unit_key: str) -> None:
    kind, number, release = grouping_units.parse_key(unit_key)
    for group in plan["groups"]:
        picture = group.get("image")
        if not picture or picture.get("cover") or group.get("image_confidence") != "high":
            continue
        row = (conn.execute(queries.SELECT_GROUP_ID, (number, group["name"])) if kind == ARCHIVE
               else conn.execute(queries.SELECT_LIBRARY_GROUP_ID, (number, release, group["name"]))).fetchone()
        if not row:
            continue
        if picture.get("available") and picture.get("preview"):
            conn.execute(queries.SET_GROUP_PICTURE, (picture["preview"], "llm", row[0]))
        else:
            conn.execute(queries.INSERT_PICTURE_JOB, (
                "extract" if picture.get("member") else "copy", None, row[0], number if kind == ARCHIVE else None,
                json.dumps({**picture, "source": "llm"}),
            ))


# Apply ---------------------------------------------------------------------------------------

def apply_plan(conn: sqlite3.Connection, import_id: int, plan: dict[str, Any], reviewed: bool = False) -> dict[str, Any]:
    """Applies one unit's plan, snapshotted first. Returns what changed and what was held back.

    Raises:
        ApplyRefused: The unit changed since it was checked, or its save refused the cards.
    """
    unit_key = plan["unit_key"]
    current = grouping_units.build(conn, unit_key)
    if current.fingerprint != plan["fingerprint"]:
        raise ApplyRefused("Its files changed after the answer was checked. Export it again.")
    before = state(conn, unit_key)
    cards = cards_for(conn, plan, before)
    with conn:
        snapshot_id = int(conn.execute(queries.INSERT_SNAPSHOT, (import_id, unit_key, json.dumps(before))).lastrowid or 0)
    try:
        save_cards(conn, unit_key, cards, reviewed)
    except ApplyRefused:
        with conn:
            conn.execute(queries.DELETE_SNAPSHOT, (snapshot_id,))
        raise

    held_back: list[dict] = []
    models_before = {m["id"]: m for m in before["models"]}
    with conn:
        for model in plan["models"]:
            model_id = _model_of(conn, model["artifacts"][0])
            _picture(conn, plan, unit_key, model_id, model, models_before, held_back)
            for tag in model.get("tags", []):
                # A tag a person took off this collection stays off. Only a person can put it back.
                if not conn.execute(queries.SELECT_REMOVED_TAG, (tag, model_id)).fetchone():
                    conn.execute(queries.INSERT_TAG, (model_id, tag))
        _group_pictures(conn, plan, unit_key)
        conn.execute(queries.UPDATE_SNAPSHOT_HASH, (state_hash(state(conn, unit_key)), snapshot_id))
    return {"unit": unit_key, "models_before": len([m for m in before["models"] if m["state"] != "skipped"]),
            "models_after": len(plan["models"]), "pictures_held_back": held_back}


def _suggest(conn: sqlite3.Connection, import_id: int, plan: dict, verdict: dict) -> None:
    before = state(conn, plan["unit_key"])
    payload = {"cards": cards_for(conn, plan, before), "plan": plan}
    with conn:
        conn.execute(queries.REPLACE_WAITING_SUGGESTIONS, (plan["unit_key"],))
        conn.execute(queries.INSERT_SUGGESTION, (
            import_id, plan["unit_key"], verdict.get("bucket") or "medium", json.dumps(payload), json.dumps(verdict.get("reasons", [])),
        ))
        conn.execute(queries.UPDATE_UNIT_DECIDED, ("suggested", plan["fingerprint"], plan["unit_key"]))


def apply(conn: sqlite3.Connection, import_id: int) -> dict[str, Any]:
    """Applies every unit an import may apply, and files the rest as suggestions.

    Raises:
        ApplyRefused: The import doesn't exist, or was already applied or undone.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_IMPORT, (import_id,)).fetchone()
    if not row:
        raise ApplyRefused("That import doesn't exist.")
    if row["status"] != "checked":
        raise ApplyRefused(f"That import was already {row['status']}.")
    report, plans = json.loads(row["report_json"]), json.loads(row["answer_json"])
    result: dict[str, Any] = {"import_id": import_id, "applied": [], "suggested": [], "refused": [], "pictures_held_back": []}
    for verdict in report["units"]:
        plan = plans.get(verdict["unit"])
        if not plan or verdict["outcome"] not in (APPLY, SUGGEST):
            continue
        try:
            if verdict["outcome"] == APPLY:
                applied = apply_plan(conn, import_id, plan)
                with conn:
                    conn.execute(queries.UPDATE_UNIT_DECIDED, ("applied", plan["fingerprint"], plan["unit_key"]))
                result["applied"].append(applied)
                if applied["pictures_held_back"]:
                    # A grouping can apply while a picture waits: 9A.4 holds a picture back on its own.
                    _suggest_pictures(conn, import_id, plan, applied["pictures_held_back"])
                    result["pictures_held_back"] += applied["pictures_held_back"]
            else:
                _suggest(conn, import_id, plan, verdict)
                result["suggested"].append(plan["unit_key"])
        except (ApplyRefused, grouping_units.UnitError) as e:
            result["refused"].append({"unit": plan["unit_key"], "why": str(e)})
    with conn:
        conn.execute(queries.UPDATE_IMPORT_STATUS, ("applied", "applied", "applied", import_id))
    audit.record(conn, "grouping_apply", False, {
        "import_id": import_id, "applied": [a["unit"] for a in result["applied"]],
        "suggested": result["suggested"], "refused": result["refused"],
    })
    return result


def _suggest_pictures(conn: sqlite3.Connection, import_id: int, plan: dict, held_back: list[dict]) -> None:
    with conn:
        conn.execute(queries.INSERT_SUGGESTION, (
            import_id, plan["unit_key"], "medium", json.dumps({"cards": [], "pictures": held_back, "plan": None}),
            json.dumps(["Pictures the LLM wasn't sure of, or that would replace a picture found in the archive"]),
        ))


# Undo ----------------------------------------------------------------------------------------

def restore(conn: sqlite3.Connection, unit_key: str, before: dict[str, Any]) -> None:
    """Puts a unit back exactly as a snapshot holds it. The caller has checked nobody edited it since."""
    kind, number, release = grouping_units.parse_key(unit_key)
    now = state(conn, unit_key)
    kept = {m["id"] for m in before["models"]}
    added = sorted({m["id"] for m in now["models"]} - kept)
    fallback = before["models"][0]["id"] if before["models"] else None
    collections = {c for _, c in before["links"]} | ({number} if kind == ARCHIVE else set())

    for artifact, model_id in before["artifacts"].items():
        conn.execute(queries.MOVE_ARTIFACT, (model_id, int(artifact)))
    for m in before["models"]:
        conn.execute(queries.RESTORE_MODEL, (
            m["name"], m["category"], m["state"], m["merged_into"], m["image_path"], m["image_source"], m["image_locked"], m["id"],
        ))
    for model_id in added:
        # Made by the apply, and empty now. The row stays and redirects, as any emptied model does.
        if fallback is not None:
            conn.execute(queries.REDIRECT_MODEL, (fallback, model_id))
    for model_id in sorted(kept | set(added)):
        for collection in collections:
            conn.execute(queries.DELETE_LINKS_TO, (model_id, collection))
    for model_id, collection in before["links"]:
        conn.execute(queries.INSERT_LINK, (model_id, collection))

    if kind == ARCHIVE:
        conn.execute(queries.DELETE_ARCHIVE_GROUPS, (number,))
    else:
        conn.execute(queries.DELETE_LIBRARY_GROUPS, (number, release))
    for group in before["groups"]:
        if kind == ARCHIVE:
            group_id = conn.execute(queries.INSERT_ARCHIVE_GROUP, (number, group["name"], group["image_path"], group["image_source"])).lastrowid
        else:
            group_id = conn.execute(queries.INSERT_LIBRARY_GROUP, (number, release, group["name"], group["image_path"], group["image_source"])).lastrowid
        for member in group["members"]:
            conn.execute(queries.INSERT_GROUP_MEMBER, (group_id, member))

    wanted = {(m, t) for m, t in before["tags"]}
    for model_id, tag in [(m, t) for m, t in now["tags"]]:
        if (model_id, tag) not in wanted:
            conn.execute(queries.DELETE_TAG, (model_id, tag))
    for model_id, tag in wanted:
        conn.execute(queries.INSERT_TAG, (model_id, tag))

    if kind == ARCHIVE:
        if before["reviewed"] is None:
            conn.execute(queries.DELETE_REVIEW, (number,))
        else:
            conn.execute(queries.UPSERT_REVIEW, (before["reviewed"], number))
    touched = sorted(kept | set(added)) or [0]
    conn.execute(queries.SKIP_PICTURE_JOBS.format(marks=",".join("?" * len(touched))), [*touched, number if kind == ARCHIVE else -1])


def undo(conn: sqlite3.Connection, import_id: int) -> dict[str, Any]:
    """Restores every unit the import applied, except any a person has edited since.

    Raises:
        ApplyRefused: The import doesn't exist or wasn't applied.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_IMPORT, (import_id,)).fetchone()
    if not row:
        raise ApplyRefused("That import doesn't exist.")
    if row["status"] != "applied":
        raise ApplyRefused("Only an applied import can be undone.")
    restored, left = [], []
    for snapshot in conn.execute(queries.SELECT_SNAPSHOTS, (import_id,)).fetchall():
        if snapshot["restored_at"]:
            continue
        unit_key = snapshot["unit_key"]
        if state_hash(state(conn, unit_key)) != snapshot["after_hash"]:
            left.append({"unit": unit_key, "why": "Changed since the import applied, so it was left as it is."})
            continue
        with conn:
            restore(conn, unit_key, json.loads(snapshot["before_json"]))
            conn.execute(queries.UPDATE_SNAPSHOT_RESTORED, (snapshot["id"],))
            conn.execute(queries.UPDATE_UNIT_DECIDED, ("undone", None, unit_key))
        restored.append(unit_key)
    with conn:
        conn.execute(queries.UPDATE_IMPORT_STATUS, ("undone", "undone", "undone", import_id))
    result = {"import_id": import_id, "restored": restored, "left": left}
    audit.record(conn, "grouping_undo", False, result)
    return result
