"""Changing linked library models that are already in the catalog.

Stash items and downloads point at files, not models, so moving files between models can't break
either. A card that existed keeps its ID, a new card gets a new one, and a card left empty keeps
its row and redirects to where its files went.
"""

import sqlite3
from typing import Any

from desync_archival.db.queries import catalog_review as queries
from desync_archival.services.archive_review import _owner, _review_flags
from desync_archival.services.catalog_levels import MERGED, RESOLVED, SKIPPED
from desync_archival.services.library_review import DEFAULT_CATEGORY, ReviewError


def _artifacts(conn: sqlite3.Connection, library_id: int, release: str) -> list[sqlite3.Row]:
    return conn.execute(queries.SELECT_RELEASE_ARTIFACTS, (library_id, f"{library_id}/{release}/%")).fetchall()


def _folders(files: list[str]) -> list[str]:
    return sorted({f.rsplit("/", i)[0] for f in files for i in range(1, f.count("/"))})


def view(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    artifacts = _artifacts(conn, library_id, release)
    if not artifacts:
        raise ReviewError(f"Nothing from {release} is in the catalog yet.")
    held: dict[int, list[str]] = {}
    for row in artifacts:
        held.setdefault(row["model_id"], []).append(row["path"])
    marks = ",".join("?" * len(held))
    models = conn.execute(queries.SELECT_MODELS.format(marks=marks), list(held)).fetchall()
    groups = dict(conn.execute(queries.SELECT_RELEASE_GROUPS, (library_id, release)).fetchall())
    cards = [
        {
            "key": str(m["id"]), "name": m["name"], "category": m["category"] or "", "paths": held[m["id"]],
            "skip": m["state"] == SKIPPED, "group": groups.get(m["id"], ""), "tags": [], "flags": [],
        }
        for m in sorted(models, key=lambda x: x["name"].lower())
    ]
    _review_flags(cards)
    files = [r["path"] for r in artifacts]
    return {
        "release": release, "title": f"{release}: models in the catalog", "folders": _folders(files), "files": files,
        "cards": cards, "reviewed": False, "changed": False, "locked": [], "settled": [],
    }


def _apply_groups(conn: sqlite3.Connection, library_id: int, release: str, assigned: list[tuple[int, str]]) -> None:
    conn.execute(queries.DELETE_RELEASE_GROUP_MEMBERS, (library_id, release))
    wanted = {name for _, name in assigned if name}
    existing = dict(conn.execute(queries.SELECT_RELEASE_GROUP_IDS, (library_id, release)).fetchall())
    for name, group_id in existing.items():
        if name not in wanted:
            conn.execute(queries.DELETE_GROUP, (group_id,))
    for model_id, name in assigned:
        if name:
            group_id = existing.get(name) or conn.execute(queries.INSERT_RELEASE_GROUP, (library_id, release, name)).lastrowid
            existing[name] = group_id
            conn.execute(queries.INSERT_GROUP_MEMBER, (group_id, model_id))


def save(conn: sqlite3.Connection, library_id: int, release: str, cards: list[dict]) -> dict[str, Any]:
    """Applies edits. An emptied card keeps its row and redirects, so no ID dangles.

    Raises:
        ReviewError: The review can't be applied.
    """
    conn.row_factory = sqlite3.Row
    artifacts = _artifacts(conn, library_id, release)
    if not artifacts:
        raise ReviewError(f"Nothing from {release} is in the catalog yet.")
    shown = {r["path"]: r for r in artifacts}
    folders = set(_folders(list(shown)))
    linked = {r["model_id"] for r in artifacts}

    claims: dict[str, int] = {}
    for index, card in enumerate(cards):
        if not str(card.get("name") or "").strip():
            raise ReviewError("Every card needs a name.")
        for path in card.get("paths") or []:
            if path not in shown and path not in folders:
                raise ReviewError(f"{path} isn't in this release. Reload it.")
            if path in claims:
                raise ReviewError(f"{path} is in two cards.")
            claims[path] = index
    owners = {path: _owner(path, claims) for path in shown}
    if unclaimed := [path for path, owner in owners.items() if owner is None]:
        raise ReviewError(f"{unclaimed[0].rpartition('/')[2]} has no card.")

    with conn:
        ids: list[int | None] = []
        for index, card in enumerate(cards):
            key = str(card.get("key") or "")
            state = SKIPPED if card.get("skip") else RESOLVED
            name = str(card["name"]).strip()
            category = str(card.get("category") or "").strip() or DEFAULT_CATEGORY
            if key.isdigit() and int(key) in linked:
                conn.execute(queries.UPDATE_MODEL, (name, category, state, int(key)))
                # The Collections view still reads the row this model started from.
                conn.execute(queries.UPDATE_COLLECTION_NAME, (name, category, int(key)))
                ids.append(int(key))
            elif index in owners.values():
                ids.append(int(conn.execute(queries.INSERT_MODEL, (name, category, state)).lastrowid or 0))
            else:
                ids.append(None)

        went_to: dict[int, int] = {}
        for path, owner in owners.items():
            target = ids[owner] if owner is not None else None
            if target is not None:
                went_to[shown[path]["model_id"]] = target
            if target is not None and target != shown[path]["model_id"]:
                conn.execute(queries.MOVE_ARTIFACT, (target, shown[path]["id"]))
            if target is not None:
                conn.execute(queries.LINK_MODEL, (target, shown[path]["collection_id"]))

        for model_id in linked - set(ids):
            # The collection row stays, because it's what holds the files.
            conn.execute(queries.REDIRECT_MODEL, (MERGED, went_to[model_id], model_id))
            conn.execute(queries.UNLINK_MODEL, (model_id,))

        _apply_groups(conn, library_id, release, [
            (model_id, str(card.get("group") or "").strip()) for card, model_id in zip(cards, ids, strict=True) if model_id
        ])
    return {"success": True, "cards": len([i for i in ids if i])}
