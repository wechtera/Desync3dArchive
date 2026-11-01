"""Human decisions about what's inside a collection's archives. Same rules as library review:
a card owns paths, the deepest claim wins, and every artifact must end up on exactly one card.

A model shared with another collection is one row, so a rename here shows everywhere. Moving an
artifact only moves this collection's copy.
"""

import sqlite3
from collections import defaultdict
from typing import Any

from desync_archival.db.queries import archive_review as queries
from desync_archival.services import model_resolver
from desync_archival.services.catalog_levels import MERGED, RESOLVED, SKIPPED
from desync_archival.services.library_review import DEFAULT_CATEGORY, ReviewError

MANY_MODELS = 20  # Past this, an archive is more likely over-split than really this big.


def _artifacts(conn: sqlite3.Connection, collection_id: int) -> list[sqlite3.Row]:
    return conn.execute(queries.SELECT_MODEL_ARTIFACTS_BY_MODEL_ID, (collection_id,)).fetchall()


def _shown_path(collection_id: int, row: sqlite3.Row) -> str:
    return f"{collection_id}/{row['archive']}/{row['path']}"


def _owner(path: str, claims: dict[str, int]) -> int | None:
    parts = path.split("/")
    for end in range(len(parts), 0, -1):
        owner = claims.get("/".join(parts[:end]))
        if owner is not None:
            return owner
    return None


def _review_flags(cards: list[dict[str, Any]]) -> None:
    """Same signals as a linked release, plus the one archives get wrong most: pieces as models."""
    models = [
        model_resolver.ResolvedModel(
            key=model_resolver.slug_of(model_resolver.words_in(card["name"])), name=card["name"],
            folders=sorted({m.rpartition("/")[0] for m in card["paths"]}),
        )
        for card in cards
    ]
    variants = set(model_resolver.PRINT_WORDS)
    model_resolver.flag_models(models, variants, set(model_resolver.PIECE_WORDS))
    for card, model in zip(cards, models, strict=True):
        card["flags"] += model.flags
        # Raygalius LT beside Raygalius is nearly always a piece that became a model.
        parent = max(
            (c["name"] for c in cards if card["name"].lower().startswith(f"{c['name'].lower()} ")), key=len, default="",
        )
        if parent:
            card["flags"].append(f"Looks like a part of {parent}")


def queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Worst first, so effort goes where a mistake is most likely."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(queries.SELECT_MODEL_GROUPS_BY_COLLECTION_ID).fetchall()
    items = [{**dict(r), "reviewed": bool(r["reviewed"]), "suspect": r["models"] >= MANY_MODELS} for r in rows]
    return sorted(items, key=lambda i: (i["reviewed"], not i["suspect"], -i["models"], i["name"].lower()))


def view(conn: sqlite3.Connection, collection_id: int) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    collection = conn.execute(queries.SELECT_COLLECTIONS_NAME_BY_ID, (collection_id,)).fetchone()
    artifacts = _artifacts(conn, collection_id)
    if not collection or not artifacts:
        raise ReviewError("This collection has no peeked artifacts.")

    held: dict[int, list[str]] = defaultdict(list)
    for row in artifacts:
        held[row["model_id"]].append(_shown_path(collection_id, row))
    files = [_shown_path(collection_id, r) for r in artifacts]
    folders = {f.rsplit("/", i)[0] for f in files for i in range(1, f.count("/"))}

    marks = ",".join("?" * len(held))
    models = conn.execute(f"SELECT id, name, category, state FROM models WHERE id IN ({marks});", list(held)).fetchall()
    groups = dict(conn.execute(
        queries.SELECT_MODEL_GROUP_MEMBERS_BY_COLLECTION_ID, (collection_id,),
    ).fetchall())
    shared = dict(conn.execute(
        f"SELECT model_id, COUNT(*) FROM model_collections WHERE model_id IN ({marks})"
        " GROUP BY model_id;", list(held),
    ).fetchall())
    review = conn.execute(queries.SELECT_COLLECTION_REVIEWS_REVIEWED_BY_COLLECTION_ID, (collection_id,)).fetchone()
    cards = [
        {
            "key": str(m["id"]), "name": m["name"], "category": m["category"] or "", "paths": held[m["id"]],
            "skip": m["state"] == SKIPPED, "group": groups.get(m["id"], ""), "tags": [],
            "flags": [f"Shared with {shared[m['id']] - 1} other collections"] if shared.get(m["id"], 1) > 1 else [],
        }
        for m in sorted(models, key=lambda x: x["name"].lower())
    ]
    _review_flags(cards)
    return {
        "release": str(collection_id), "title": collection["name"], "folders": sorted(folders), "files": files,
        "peeked_at": conn.execute(queries.MAX_MODEL_FILES_BY_MODEL_ID, (collection_id,)).fetchone()[0],
        "cards": cards,
        "reviewed": bool(review and review["reviewed"]), "changed": False, "locked": [], "settled": [],
    }


def _apply_groups(conn: sqlite3.Connection, collection_id: int, assigned: list[tuple[int, str]]) -> None:
    conn.execute(
        queries.DELETE_MODEL_GROUP_MEMBERS_BY_GROUP_ID, (collection_id,),
    )
    wanted = {name for _, name in assigned if name}
    existing = dict(conn.execute(queries.SELECT_MODEL_GROUPS_BY_COLLECTION_ID_V2, (collection_id,)).fetchall())
    for name, group_id in existing.items():
        if name not in wanted:
            conn.execute(queries.DELETE_MODEL_GROUPS_BY_ID, (group_id,))
    for model_id, name in assigned:
        if name:
            group_id = existing.get(name) or conn.execute(
                queries.INSERT_MODEL_GROUPS, (collection_id, name),
            ).lastrowid
            existing[name] = group_id
            conn.execute(queries.INSERT_MODEL_GROUP_MEMBERS, (group_id, model_id))


def save(conn: sqlite3.Connection, collection_id: int, cards: list[dict], reviewed: bool) -> dict[str, Any]:
    """Applies a human's cards. Every artifact has to end up on exactly one card.

    Raises:
        ReviewError: The review can't be applied.
    """
    conn.row_factory = sqlite3.Row
    artifacts = _artifacts(conn, collection_id)
    if not artifacts:
        raise ReviewError("This collection has no peeked artifacts.")
    shown = {_shown_path(collection_id, r): r for r in artifacts}
    folders = {f.rsplit("/", i)[0] for f in shown for i in range(1, f.count("/"))}
    linked = {r["model_id"] for r in artifacts}

    claims: dict[str, int] = {}
    for index, card in enumerate(cards):
        if not str(card.get("name") or "").strip():
            raise ReviewError("Every card needs a name.")
        for member in card.get("paths") or []:
            if member not in shown and member not in folders:
                raise ReviewError(f"{member} isn't in this collection. Reload it.")
            if member in claims:
                raise ReviewError(f"{member} is in two cards.")
            claims[member] = index

    owners = {path: _owner(path, claims) for path in shown}
    if unclaimed := [path for path, owner in owners.items() if owner is None]:
        raise ReviewError(f"{unclaimed[0].rpartition('/')[2]} has no card.")

    with conn:
        ids: list[int | None] = []
        for card in cards:
            key = str(card.get("key") or "")
            state = SKIPPED if card.get("skip") else RESOLVED
            name, category = str(card["name"]).strip(), str(card.get("category") or "").strip() or DEFAULT_CATEGORY
            if key.isdigit() and int(key) in linked:
                conn.execute(
                    queries.UPDATE_MODELS_NAME_BY_ID, (name, category, state, int(key)),
                )
                ids.append(int(key))
            elif any(owner == len(ids) for owner in owners.values()):
                model_id = conn.execute(
                    queries.INSERT_MODELS, (name, category, state),
                ).lastrowid
                conn.execute(
                    queries.INSERT_MODEL_COLLECTIONS, (model_id, collection_id),
                )
                ids.append(model_id)
            else:
                ids.append(None)

        for path, owner in owners.items():
            target = ids[owner] if owner is not None else None
            if target is not None:
                conn.execute(queries.UPDATE_MODEL_ARTIFACTS_MODEL_ID_BY_ID, (target, shown[path]["id"]))

        kept = next(i for i in ids if i is not None)
        for model_id in linked - set(ids):
            left = conn.execute(queries.COUNT_MODEL_ARTIFACTS_BY_MODEL_ID, (model_id,)).fetchone()[0]
            conn.execute(
                queries.DELETE_MODEL_COLLECTIONS_BY_MODEL_ID, (model_id, collection_id),
            )
            if not left:
                # An emptied model keeps its row and redirects, so an old ID never dangles.
                conn.execute(queries.UPDATE_MODELS_STATE_BY_ID, (MERGED, kept, model_id))

        _apply_groups(conn, collection_id, [
            (model_id, str(card.get("group") or "").strip()) for card, model_id in zip(cards, ids, strict=True) if model_id
        ])
        updated = conn.execute(
            queries.UPDATE_COLLECTION_REVIEWS_REVIEWED_BY_COLLECTION_ID,
            (int(reviewed), collection_id),
        )
        if not updated.rowcount:
            conn.execute(queries.INSERT_COLLECTION_REVIEWS, (collection_id, int(reviewed)))
    return {"success": True, "cards": len([i for i in ids if i]), "reviewed": reviewed}

