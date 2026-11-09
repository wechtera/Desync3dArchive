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
        # A person's save since an apply changes this, so undo leaves their release alone.
        review = conn.execute(queries.SELECT_LIBRARY_CATALOG_REVIEW, (number, release)).fetchone()
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
            catalog_review.save(conn, number, release, cards, by_person=reviewed)
    except ReviewError as e:
        raise ApplyRefused(str(e)) from e


# Pictures and tags ---------------------------------------------------------------------------

def _model_of(conn: sqlite3.Connection, artifact_id: int) -> int:
    return conn.execute(queries.SELECT_ARTIFACT_MODEL, (artifact_id,)).fetchone()[0]


def _covers(conn: sqlite3.Connection, unit_key: str, artifacts: list[int]) -> list[int]:
    """E54-9: library collections whose cover this model's render should replace. Those it holds
    most of the files of, in a release with no artwork, where every cover is a size-picked render."""
    kind, number, release = grouping_units.parse_key(unit_key)
    if kind == ARCHIVE or conn.execute(queries.SELECT_LIBRARY_IMAGES, (number, release)).fetchone():
        return []
    held: dict[int, int] = {}
    for artifact in artifacts:
        row = conn.execute(queries.SELECT_ARTIFACT_COLLECTION, (artifact,)).fetchone()
        if row:
            held[row[0]] = held.get(row[0], 0) + 1
    return sorted(c for c, n in held.items() if n * 2 > conn.execute(queries.COUNT_COLLECTION_ARTIFACTS, (c,)).fetchone()[0])


def _picture(conn: sqlite3.Connection, plan: dict, unit_key: str, model_id: int, model: dict,
             before: dict[int, dict], held_back: list[dict], snapshot_id: int) -> None:
    """Applies a model's picture by 9A.4, or holds it back as a suggestion."""
    picture = model["image"]
    had = before.get(model_id, {})
    if had.get("image_locked") or had.get("image_source") == OWNER:
        return
    collection = None if not unit_key.startswith(f"{ARCHIVE}:") else grouping_units.parse_key(unit_key)[1]
    if picture is None:
        if model.get("render_artifact") and not had.get("image_path") and had.get("image_source") in (None, "render"):
            source = {"artifact": model["render_artifact"], "covers": _covers(conn, unit_key, model["artifacts"]),
                      "snapshot": snapshot_id}
            conn.execute(queries.INSERT_PICTURE_JOB, ("render", model_id, None, collection, json.dumps(source)))
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
            "extract" if picture.get("member") else "copy", model_id, None, collection,
            json.dumps({**picture, "source": source, "snapshot": snapshot_id}),
        ))


def _group_pictures(conn: sqlite3.Connection, plan: dict, unit_key: str, snapshot_id: int) -> None:
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
                json.dumps({**picture, "source": "llm", "snapshot": snapshot_id}),
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
            _picture(conn, plan, unit_key, model_id, model, models_before, held_back, snapshot_id)
            for tag in model.get("tags", []):
                # A tag a person took off this collection stays off. Only a person can put it back.
                if not conn.execute(queries.SELECT_REMOVED_TAG, (tag, model_id)).fetchone():
                    conn.execute(queries.INSERT_TAG, (model_id, tag))
        _group_pictures(conn, plan, unit_key, snapshot_id)
        conn.execute(queries.UPDATE_SNAPSHOT_HASH, (state_hash(state(conn, unit_key)), snapshot_id))
    return {"unit": unit_key, "models_before": len([m for m in before["models"] if m["state"] != "skipped"]),
            "models_after": len(plan["models"]), "pictures_held_back": held_back}


def _suggest(conn: sqlite3.Connection, import_id: int, plan: dict, verdict: dict) -> int:
    before = state(conn, plan["unit_key"])
    payload = {"cards": cards_for(conn, plan, before), "plan": plan}
    with conn:
        conn.execute(queries.REPLACE_WAITING_SUGGESTIONS, (plan["unit_key"],))
        suggestion_id = conn.execute(queries.INSERT_SUGGESTION, (
            import_id, plan["unit_key"], verdict.get("bucket") or "medium", json.dumps(payload), json.dumps(verdict.get("reasons", [])),
        )).lastrowid
        conn.execute(queries.UPDATE_UNIT_DECIDED, ("suggested", plan["fingerprint"], plan["unit_key"]))
    return int(suggestion_id or 0)


def review_now(conn: sqlite3.Connection, import_id: int, unit_key: str) -> dict[str, Any]:
    """Files one unit of a checked answer as waiting, so a person can review it before confirming the rest.

    The unit is marked filed in the stored report, so the later confirm leaves it to the review.

    Raises:
        ApplyRefused: The import isn't waiting for confirmation, the unit isn't in it, was refused,
            or is already filed.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_IMPORT, (import_id,)).fetchone()
    if not row:
        raise ApplyRefused("That import doesn't exist.")
    if row["status"] != "checked":
        raise ApplyRefused(f"That import was already {row['status']}.")
    report, plans = json.loads(row["report_json"]), json.loads(row["answer_json"])
    verdict = next((v for v in report["units"] if v["unit"] == unit_key), None)
    if verdict is None:
        raise ApplyRefused(f"{unit_key} isn't in that answer.")
    if verdict.get("filed"):
        raise ApplyRefused(f"{verdict['title']} is already waiting for you.")
    plan = plans.get(unit_key)
    if not plan or verdict["outcome"] not in (APPLY, SUGGEST):
        raise ApplyRefused(f"{verdict['title']} was refused, so there's nothing to review. Fix it with the LLM.")
    shown = verdict if verdict["outcome"] == SUGGEST else {**verdict, "reasons": [*verdict.get("reasons", []), HELD_BY_PERSON]}
    suggestion_id = _suggest(conn, import_id, plan, shown)
    verdict["filed"] = suggestion_id
    with conn:
        conn.execute(queries.UPDATE_IMPORT_REPORT, (json.dumps(report), import_id))
    audit.record(conn, "grouping_review_now", False, {"import_id": import_id, "unit": unit_key, "suggestion": suggestion_id})
    return {"import_id": import_id, "unit": unit_key, "suggestion_id": suggestion_id}


CHOOSE_APPLY, CHOOSE_WAIT, CHOOSE_SKIP = "apply", "wait", "skip"
# A person may hold back or leave out anything, but only the checks can let a unit apply unseen.
# A unit they sent to review goes through the editor; jumping it straight in would skip that.
CHOICES_ALLOWED = {
    APPLY: (CHOOSE_APPLY, CHOOSE_WAIT, CHOOSE_SKIP),
    SUGGEST: (CHOOSE_WAIT, CHOOSE_SKIP),
}
HELD_BY_PERSON = "You chose to review this one before it applies."


def _choices(report: dict, choices: dict[str, str]) -> dict[str, str]:
    """Each unit's choice, defaulting to what the checks decided. Refuses the lot on any bad one."""
    verdicts = {v["unit"]: v for v in report["units"]}
    for unit, choice in choices.items():
        verdict = verdicts.get(unit)
        if verdict is None:
            raise ApplyRefused(f"{unit} isn't in that answer.")
        if verdict.get("filed"):
            # Already in the review editor through Review Now; the confirm leaves it there.
            continue
        allowed = CHOICES_ALLOWED.get(verdict["outcome"], (CHOOSE_SKIP,))
        if choice not in allowed:
            if verdict["outcome"] == SUGGEST and choice == CHOOSE_APPLY:
                raise ApplyRefused(f"{verdict['title']} can't apply straight away. Let it wait, then accept it in the review editor.")
            raise ApplyRefused(f"{verdict['title']} can only be {' or '.join(allowed)}, not {choice}.")
    defaults = {APPLY: CHOOSE_APPLY, SUGGEST: CHOOSE_WAIT}
    return {unit: choices.get(unit, defaults.get(v["outcome"], CHOOSE_SKIP)) for unit, v in verdicts.items()}


def apply(conn: sqlite3.Connection, import_id: int, choices: dict[str, str] | None = None) -> dict[str, Any]:
    """Applies the units an import may apply, and files the ones waiting as suggestions.

    `choices` maps unit keys to apply, wait or skip, for a person holding back or leaving out
    units. Units it doesn't name go as the checks decided.

    Raises:
        ApplyRefused: The import doesn't exist, was already applied or undone, or a choice isn't
            allowed for its unit. Nothing changes then.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(queries.SELECT_IMPORT, (import_id,)).fetchone()
    if not row:
        raise ApplyRefused("That import doesn't exist.")
    if row["status"] != "checked":
        raise ApplyRefused(f"That import was already {row['status']}.")
    report, plans = json.loads(row["report_json"]), json.loads(row["answer_json"])
    chosen = _choices(report, choices or {})
    totals_before = file_totals(conn)
    result: dict[str, Any] = {"import_id": import_id, "applied": [], "suggested": [], "skipped": [], "filed": [],
                              "refused": [], "pictures_held_back": []}
    # What became of each unit, kept on the report for the batch list: applied, waiting, review,
    # skipped or refused. The audit row alone can't show it, and a choice isn't an outcome.
    became: dict[str, str] = {}
    for verdict in report["units"]:
        plan = plans.get(verdict["unit"])
        if not plan or verdict["outcome"] not in (APPLY, SUGGEST):
            continue
        if verdict.get("filed"):
            result["filed"].append(plan["unit_key"])
            became[verdict["unit"]] = "review"
            continue
        choice = chosen[verdict["unit"]]
        if choice == CHOOSE_SKIP:
            # Nothing is recorded as decided, so the unit keeps today's grouping and goes out in the next delta.
            result["skipped"].append(plan["unit_key"])
            became[verdict["unit"]] = "skipped"
            continue
        if choice == CHOOSE_WAIT and verdict["outcome"] == APPLY:
            verdict = {**verdict, "reasons": [*verdict.get("reasons", []), HELD_BY_PERSON]}
        try:
            if choice == CHOOSE_APPLY:
                applied = apply_plan(conn, import_id, plan)
                with conn:
                    conn.execute(queries.UPDATE_UNIT_DECIDED, ("applied", plan["fingerprint"], plan["unit_key"]))
                result["applied"].append(applied)
                became[verdict["unit"]] = "applied"
                if applied["pictures_held_back"]:
                    # A grouping can apply while a picture waits: 9A.4 holds a picture back on its own.
                    _suggest_pictures(conn, import_id, plan, applied["pictures_held_back"])
                    result["pictures_held_back"] += applied["pictures_held_back"]
            else:
                _suggest(conn, import_id, plan, verdict)
                result["suggested"].append(plan["unit_key"])
                became[verdict["unit"]] = "waiting"
        except (ApplyRefused, grouping_units.UnitError) as e:
            result["refused"].append({"unit": plan["unit_key"], "why": str(e)})
    for verdict in report["units"]:
        verdict["result"] = became.get(verdict["unit"], "refused")
    with conn:
        conn.execute(queries.UPDATE_IMPORT_REPORT, (json.dumps(report), import_id))
        conn.execute(queries.UPDATE_IMPORT_STATUS, ("applied", "applied", "applied", import_id))
    result["totals"] = {"before": totals_before, "after": file_totals(conn)}
    audit.record(conn, "grouping_apply", False, {
        "import_id": import_id, "applied": [a["unit"] for a in result["applied"]],
        "suggested": result["suggested"], "skipped": result["skipped"], "refused": result["refused"],
        "totals": result["totals"],
    })
    return result


def _suggest_pictures(conn: sqlite3.Connection, import_id: int, plan: dict, held_back: list[dict]) -> None:
    with conn:
        conn.execute(queries.INSERT_SUGGESTION, (
            import_id, plan["unit_key"], "medium", json.dumps({"cards": [], "pictures": held_back, "plan": None}),
            json.dumps(["Pictures the LLM wasn't sure of, or that would replace a picture found in the archive"]),
        ))


def file_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """Catalog-wide counts of every file record, for the person to see an apply or undo lost none."""
    conn.row_factory = sqlite3.Row
    return dict(conn.execute(queries.SELECT_FILE_TOTALS).fetchone())


def what_changed(now: dict[str, Any], plan: dict | None, before: dict[str, Any]) -> list[str]:
    """What a person changed since an apply, read against what the apply set out to do.

    Only the after state's hash is kept, so this compares today's unit with the answer's plan: a
    renamed model, files moved to another model, a model moved to another group, a picture the
    person chose, or a review they saved. Anything else falls to the caller's general wording.
    """
    notes: list[str] = []
    rows = {m["id"]: m for m in now["models"]}
    group_of = {member: g["name"] for g in now["groups"] for member in g["members"]}
    renamed, regrouped, moved = [], [], 0
    for model in (plan or {}).get("models", []):
        holder = now["artifacts"].get(str(model["artifacts"][0]))
        moved += sum(1 for artifact in model["artifacts"] if now["artifacts"].get(str(artifact)) != holder)
        row = rows.get(holder)
        if row is None:
            continue
        if row["name"] != model["name"]:
            renamed.append(f"{model['name']} is now called {row['name']}")
        elif (group_of.get(holder) or "") != (model.get("group") or ""):
            regrouped.append(row["name"])
    notes += renamed
    if moved:
        notes.append(f"{moved} file{'s' if moved != 1 else ''} moved to another model")
    if regrouped:
        notes.append(f"{', '.join(regrouped)} {'is' if len(regrouped) == 1 else 'are'} in another group")
    had_owner = {m["id"] for m in before.get("models", []) if m.get("image_source") == OWNER}
    chosen = [m["name"] for m in now["models"] if m.get("image_source") == OWNER and m["id"] not in had_owner]
    if chosen:
        notes.append(f"you chose a picture for {', '.join(chosen)}")
    if now.get("reviewed") and not before.get("reviewed"):
        notes.append("a person saved a review of it")
    return notes


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
    plans = json.loads(row["answer_json"])
    titles = {v["unit"]: v["title"] for v in json.loads(row["report_json"]).get("units", [])}
    totals_before = file_totals(conn)
    for snapshot in conn.execute(queries.SELECT_SNAPSHOTS, (import_id,)).fetchall():
        if snapshot["restored_at"]:
            continue
        unit_key = snapshot["unit_key"]
        now = state(conn, unit_key)
        if state_hash(now) != snapshot["after_hash"]:
            changes = what_changed(now, plans.get(unit_key), json.loads(snapshot["before_json"]))
            why = (f"Changed since the import applied: {'; '.join(changes)}. It was left as it is." if changes else
                   "Changed since the import applied, in a way the app can't name, such as its tags. It was left as it is.")
            left.append({"unit": unit_key, "title": titles.get(unit_key, unit_key), "why": why})
            continue
        with conn:
            restore(conn, unit_key, json.loads(snapshot["before_json"]))
            conn.execute(queries.UPDATE_SNAPSHOT_RESTORED, (snapshot["id"],))
            conn.execute(queries.UPDATE_UNIT_DECIDED, ("undone", None, unit_key))
        restored.append(unit_key)
    with conn:
        conn.execute(queries.UPDATE_IMPORT_STATUS, ("undone", "undone", "undone", import_id))
    result = {"import_id": import_id, "restored": restored, "left": left,
              "totals": {"before": totals_before, "after": file_totals(conn)}}
    audit.record(conn, "grouping_undo", False, result)
    return result
