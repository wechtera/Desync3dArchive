"""Reads for browsing models. A model is what the user browses: collections and groups lead to it."""

import sqlite3
from typing import Any

from desync_archival.services.catalog_levels import MERGED, SKIPPED
from desync_archival.services.stash_service import StashService

PAGE_SIZE = 48

_VISIBLE = f"""
    m.state NOT IN ('{MERGED}', '{SKIPPED}') AND EXISTS (
        SELECT 1 FROM model_collections l JOIN collections c ON c.id = l.collection_id
        WHERE l.model_id = m.id AND c.is_active = 1
    )
"""

# One query per level, then one UNION, because OR across FTS tables reads every row.
_SEARCH = """
    SELECT rowid FROM catalog_search_models WHERE name LIKE ?1
    UNION SELECT model_id FROM model_group_members
        WHERE group_id IN (SELECT rowid FROM catalog_search_groups WHERE name LIKE ?1)
    UNION SELECT model_id FROM model_artifacts
        WHERE id IN (SELECT rowid FROM catalog_search_artifacts WHERE name LIKE ?1)
"""


def _groups_of(conn: sqlite3.Connection, model_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("""
        SELECT g.id, g.name FROM model_groups g JOIN model_group_members m ON m.group_id = g.id
        WHERE m.model_id = ? ORDER BY g.name;
    """, (model_id,)).fetchall()]


def _collections_of(conn: sqlite3.Connection, model_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("""
        SELECT c.id, c.name, c.designer_name, c.primary_image_url FROM collections c
        JOIN model_collections l ON l.collection_id = c.id
        WHERE l.model_id = ? ORDER BY c.id;
    """, (model_id,)).fetchall()]


def _summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    collections = _collections_of(conn, row["id"])
    return {
        **dict(row), "groups": _groups_of(conn, row["id"]), "collections": collections,
        "designer_name": collections[0]["designer_name"] if collections else "",
        "cover_id": collections[0]["id"] if collections else row["id"],
        "image_url": collections[0]["primary_image_url"] if collections else None,
    }


def list_models(conn: sqlite3.Connection, search: str = "", page: int = 1) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    where, args = _VISIBLE, []
    if search:
        where += f" AND m.id IN ({_SEARCH})"
        args = [f"%{search}%"]
    total = conn.execute(f"SELECT COUNT(*) FROM models m WHERE {where};", args).fetchone()[0]
    offset = (max(1, page) - 1) * PAGE_SIZE
    rows = conn.execute(
        f"""SELECT m.id, m.name, m.category, m.state, m.image_path,
                   (SELECT COUNT(*) FROM model_artifacts a WHERE a.model_id = m.id) AS artifacts
            FROM models m WHERE {where} ORDER BY m.name COLLATE NOCASE, m.id LIMIT {PAGE_SIZE} OFFSET {offset};""",
        args,
    ).fetchall()
    return {"models": [_summary(conn, r) for r in rows], "total": total, "page": max(1, page), "page_size": PAGE_SIZE}


def model_detail(conn: sqlite3.Connection, model_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, name, category, state, merged_into, image_path FROM models WHERE id = ?;", (model_id,)).fetchone()
    # Old IDs keep working after a merge, however many times it happened.
    seen = set()
    while row and row["merged_into"] and row["id"] not in seen:
        seen.add(row["id"])
        row = conn.execute(
            "SELECT id, name, category, state, merged_into, image_path FROM models WHERE id = ?;", (row["merged_into"],)
        ).fetchone()
    if not row:
        return None
    artifacts = conn.execute("""
        SELECT a.id, a.filename, a.path, a.size_bytes, a.scale, a.supported, a.hollow, a.file_id, a.archive_member_id,
               f.filename AS archive, f.model_id AS collection_id, m.submodel_id
        FROM model_artifacts a LEFT JOIN model_files f ON f.file_id = a.file_id
        LEFT JOIN file_archive_members m ON m.id = a.archive_member_id
        WHERE a.model_id = ? ORDER BY a.scale, a.filename;
    """, (row["id"],)).fetchall()
    # A model sold twice has each file twice. One line each, and the first copy supplies it.
    unique = {(a["filename"].lower(), a["size_bytes"]): dict(a) for a in reversed(artifacts)}
    tags = [r[0] for r in conn.execute("""
        SELECT tag FROM model_tags WHERE model_id = ?1
        UNION SELECT t.tag FROM collection_tags t JOIN model_collections l ON l.collection_id = t.model_id
        WHERE l.model_id = ?1 ORDER BY 1;
    """, (row["id"],)).fetchall()]
    first = conn.execute("""
        SELECT c.release_month, c.url, c.published_at FROM collections c JOIN model_collections l ON l.collection_id = c.id
        WHERE l.model_id = ? ORDER BY c.id LIMIT 1;
    """, (row["id"],)).fetchone()
    return {
        **_summary(conn, row), **(dict(first) if first else {}), "tags": tags,
        "artifacts": sorted(unique.values(), key=lambda a: (a["scale"] or "", a["filename"].lower())),
        "total_bytes": sum(a["size_bytes"] or 0 for a in unique.values()),
        "scales": sorted({a["scale"] for a in unique.values() if a["scale"]}),
    }


def group_detail(conn: sqlite3.Connection, group_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    group = conn.execute("SELECT id, name, collection_id, release_name FROM model_groups WHERE id = ?;", (group_id,)).fetchone()
    if not group:
        return None
    rows = conn.execute("""
        SELECT m.id, m.name, m.category, m.state, m.image_path,
               (SELECT COUNT(*) FROM model_artifacts a WHERE a.model_id = m.id) AS artifacts
        FROM models m JOIN model_group_members g ON g.model_id = m.id
        WHERE g.group_id = ? AND m.state NOT IN (?, ?) ORDER BY m.name COLLATE NOCASE;
    """, (group_id, MERGED, SKIPPED)).fetchall()
    return {**dict(group), "models": [_summary(conn, r) for r in rows]}


def stash_selection(conn: sqlite3.Connection, model_id: int, scale: str = "", support: str = "") -> dict[str, Any]:
    """Stashes the artifacts of one size and support type, not every copy of a model.

    Goes in as ordinary artifact items, so export needs no new rules. An item that can't go in,
    such as one in an archive that isn't downloaded, is counted and doesn't stop the rest.
    """
    service = StashService()
    stash = service.get_active_stash(conn=conn)
    model = model_detail(conn, model_id)
    if not stash or not model:
        raise ValueError("Pick an active stash first." if model else "Model not found.")
    picked = [
        a for a in model["artifacts"]
        if (not scale or a["scale"] == scale) and (not support or bool(a["supported"]) == (support == "supported"))
    ]
    added, skipped = 0, []
    for artifact in picked:
        member = artifact["archive_member_id"]
        try:
            service.add_to_stash(
                stash["id"], "archive_member" if member else "file", artifact["collection_id"],
                file_id=artifact["file_id"], submodel_id=artifact["submodel_id"], archive_member_id=member, conn=conn,
            )
            added += 1
        except (ValueError, sqlite3.Error) as e:
            skipped.append(f"{artifact['filename']}: {e}")
    return {"success": True, "stash": stash["name"], "matched": len(picked), "added": added, "skipped": skipped[:5]}
