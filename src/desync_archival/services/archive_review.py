"""Human decisions about what's inside a collection's archives. Same rules as library review:
a card owns paths, the deepest claim wins, and every artifact must end up on exactly one card.

A model shared with another collection is one row, so a rename here shows everywhere. Moving an
artifact only moves this collection's copy.
"""

import sqlite3
from collections import defaultdict
from typing import Any

from desync_archival.services.catalog_levels import MERGED, RESOLVED, SKIPPED
from desync_archival.services.library_review import DEFAULT_CATEGORY, ReviewError

MANY_MODELS = 20  # Past this, an archive is more likely over-split than really this big.


def _artifacts(conn: sqlite3.Connection, collection_id: int) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT a.id, a.catalog_model_id, a.path, f.filename AS archive
        FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
        WHERE f.model_id = ? AND a.archive_member_id IS NOT NULL ORDER BY a.path;
    """, (collection_id,)).fetchall()


def _shown_path(collection_id: int, row: sqlite3.Row) -> str:
    return f"{collection_id}/{row['archive']}/{row['path']}"


def _owner(path: str, claims: dict[str, int]) -> int | None:
    parts = path.split("/")
    for end in range(len(parts), 0, -1):
        owner = claims.get("/".join(parts[:end]))
        if owner is not None:
            return owner
    return None


def queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Worst first, so effort goes where a mistake is most likely."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.id, m.name, COUNT(DISTINCT a.catalog_model_id) AS models, COUNT(a.id) AS artifacts,
               COALESCE(MAX(r.reviewed), 0) AS reviewed,
               (SELECT COUNT(*) FROM model_groups g WHERE g.collection_id = m.id) AS model_groups
        FROM models m
        JOIN model_files f ON f.model_id = m.id
        JOIN model_artifacts a ON a.file_id = f.file_id AND a.archive_member_id IS NOT NULL
        LEFT JOIN collection_reviews r ON r.collection_id = m.id
        GROUP BY m.id;
    """).fetchall()
    items = [{**dict(r), "reviewed": bool(r["reviewed"]), "suspect": r["models"] >= MANY_MODELS} for r in rows]
    return sorted(items, key=lambda i: (i["reviewed"], not i["suspect"], -i["models"], i["name"].lower()))


def view(conn: sqlite3.Connection, collection_id: int) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    collection = conn.execute("SELECT name FROM models WHERE id = ?;", (collection_id,)).fetchone()
    artifacts = _artifacts(conn, collection_id)
    if not collection or not artifacts:
        raise ReviewError("This collection has no peeked artifacts.")

    held: dict[int, list[str]] = defaultdict(list)
    for row in artifacts:
        held[row["catalog_model_id"]].append(_shown_path(collection_id, row))
    files = [_shown_path(collection_id, r) for r in artifacts]
    folders = {f.rsplit("/", i)[0] for f in files for i in range(1, f.count("/"))}

    marks = ",".join("?" * len(held))
    models = conn.execute(f"SELECT id, name, category, state FROM catalog_models WHERE id IN ({marks});", list(held)).fetchall()
    groups = dict(conn.execute(
        "SELECT m.catalog_model_id, g.name FROM model_group_members m JOIN model_groups g ON g.id = m.group_id"
        " WHERE g.collection_id = ?;", (collection_id,),
    ).fetchall())
    shared = dict(conn.execute(
        f"SELECT catalog_model_id, COUNT(*) FROM catalog_model_collections WHERE catalog_model_id IN ({marks})"
        " GROUP BY catalog_model_id;", list(held),
    ).fetchall())
    review = conn.execute("SELECT reviewed FROM collection_reviews WHERE collection_id = ?;", (collection_id,)).fetchone()
    return {
        "release": str(collection_id), "title": collection["name"], "folders": sorted(folders), "files": files,
        "cards": [
            {
                "key": str(m["id"]), "name": m["name"], "category": m["category"] or "", "members": held[m["id"]],
                "skip": m["state"] == SKIPPED, "group": groups.get(m["id"], ""), "tags": [],
                "flags": [f"Shared with {shared[m['id']] - 1} other collections"] if shared.get(m["id"], 1) > 1 else [],
            }
            for m in sorted(models, key=lambda x: x["name"].lower())
        ],
        "reviewed": bool(review and review["reviewed"]), "changed": False, "locked": [], "settled": [],
    }


def _apply_groups(conn: sqlite3.Connection, collection_id: int, assigned: list[tuple[int, str]]) -> None:
    conn.execute(
        "DELETE FROM model_group_members WHERE group_id IN (SELECT id FROM model_groups WHERE collection_id = ?);", (collection_id,),
    )
    wanted = {name for _, name in assigned if name}
    existing = dict(conn.execute("SELECT name, id FROM model_groups WHERE collection_id = ?;", (collection_id,)).fetchall())
    for name, group_id in existing.items():
        if name not in wanted:
            conn.execute("DELETE FROM model_groups WHERE id = ?;", (group_id,))
    for model_id, name in assigned:
        if name:
            group_id = existing.get(name) or conn.execute(
                "INSERT INTO model_groups (collection_id, name) VALUES (?, ?);", (collection_id, name),
            ).lastrowid
            existing[name] = group_id
            conn.execute("INSERT INTO model_group_members (group_id, catalog_model_id) VALUES (?, ?);", (group_id, model_id))


def save(conn: sqlite3.Connection, collection_id: int, cards: list[dict], reviewed: bool) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    artifacts = _artifacts(conn, collection_id)
    if not artifacts:
        raise ReviewError("This collection has no peeked artifacts.")
    shown = {_shown_path(collection_id, r): r for r in artifacts}
    folders = {f.rsplit("/", i)[0] for f in shown for i in range(1, f.count("/"))}
    linked = {r["catalog_model_id"] for r in artifacts}

    claims: dict[str, int] = {}
    for index, card in enumerate(cards):
        if not str(card.get("name") or "").strip():
            raise ReviewError("Every card needs a name.")
        for member in card.get("members") or []:
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
                    "UPDATE catalog_models SET name = ?, category = ?, state = ? WHERE id = ?;", (name, category, state, int(key)),
                )
                ids.append(int(key))
            elif any(owner == len(ids) for owner in owners.values()):
                model_id = conn.execute(
                    "INSERT INTO catalog_models (name, category, state) VALUES (?, ?, ?);", (name, category, state),
                ).lastrowid
                conn.execute(
                    "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?);", (model_id, collection_id),
                )
                ids.append(model_id)
            else:
                ids.append(None)

        for path, owner in owners.items():
            target = ids[owner] if owner is not None else None
            if target is not None:
                conn.execute("UPDATE model_artifacts SET catalog_model_id = ? WHERE id = ?;", (target, shown[path]["id"]))

        kept = next(i for i in ids if i is not None)
        for model_id in linked - set(ids):
            left = conn.execute("SELECT COUNT(*) FROM model_artifacts WHERE catalog_model_id = ?;", (model_id,)).fetchone()[0]
            conn.execute(
                "DELETE FROM catalog_model_collections WHERE catalog_model_id = ? AND collection_id = ?;", (model_id, collection_id),
            )
            if not left:
                # An emptied model keeps its row and redirects, so an old ID never dangles.
                conn.execute("UPDATE catalog_models SET state = ?, merged_into = ? WHERE id = ?;", (MERGED, kept, model_id))

        _apply_groups(conn, collection_id, [
            (model_id, str(card.get("group") or "").strip()) for card, model_id in zip(cards, ids, strict=True) if model_id
        ])
        updated = conn.execute(
            "UPDATE collection_reviews SET reviewed = ?, updated_at = CURRENT_TIMESTAMP WHERE collection_id = ?;",
            (int(reviewed), collection_id),
        )
        if not updated.rowcount:
            conn.execute("INSERT INTO collection_reviews (collection_id, reviewed) VALUES (?, ?);", (collection_id, int(reviewed)))
    return {"success": True, "cards": len([i for i in ids if i]), "reviewed": reviewed}

