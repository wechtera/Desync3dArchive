"""Tags: one list, kept tidy. Imports add to it and people choose from it.

The database triggers in migration 0015 enforce what must hold for every importer. This module is
everything a person does on purpose.
"""

import sqlite3
from typing import Any

from desync_archival.db.queries import tags as queries
from desync_archival.services.catalog_levels import clean_tag as clean
from desync_archival.services.logger import log_layer

# Left by splitting titles into words. They match everything, so they find nothing.
JUNK_WORDS = ("a", "an", "and", "at", "by", "for", "in", "of", "on", "the", "to", "with")
SUGGESTION_LIMIT = 20
PAGE_LIMIT = 500


class TagRefused(ValueError):
    """The tag change wasn't made. The message is shown to the user."""


def _existing(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(queries.SELECT_TAG, (name,)).fetchone()
    if not row or row[1]:
        raise TagRefused(f"'{name}' isn't a tag yet. Create it on the Tags page first.")
    return name


def listing(conn: sqlite3.Connection, search: str = "", limit: int = PAGE_LIMIT) -> dict[str, Any]:
    rows = conn.execute(queries.SELECT_TAGS, (f"%{clean(search)}%", limit)).fetchall()
    return {"tags": [{"name": r[0], "uses": r[1]} for r in rows], "total": conn.execute(queries.COUNT_TAGS).fetchone()[0]}


def create(conn: sqlite3.Connection, name: str) -> str:
    tag = clean(name)
    if not tag:
        raise TagRefused("A tag needs a name.")
    with conn:
        conn.execute(queries.INSERT_TAG, (tag,))
        conn.execute(queries.UNBLOCK_TAG, (tag,))
    return tag


def rename(conn: sqlite3.Connection, old: str, new: str) -> str:
    """Also how two tags merge: rename dnd to d&d, and the repeats fall away."""
    target = clean(new)
    if not target:
        raise TagRefused("A tag needs a name.")
    if target == old:
        return target
    with conn:
        conn.execute(queries.INSERT_TAG, (target,))
        conn.execute(queries.UNBLOCK_TAG, (target,))
        conn.execute(queries.COPY_COLLECTION_TAG, (target, old))
        conn.execute(queries.COPY_MODEL_TAG, (target, old))
        conn.execute(queries.DELETE_COLLECTION_TAG_EVERYWHERE, (old,))
        conn.execute(queries.DELETE_MODEL_TAG_EVERYWHERE, (old,))
        # Blocked, or the next scan would bring the old spelling straight back.
        conn.execute(queries.BLOCK_TAG, (old,))
    log_layer("storage", 20, f"Tag {old} renamed to {target}")
    return target


def delete(conn: sqlite3.Connection, name: str) -> None:
    with conn:
        conn.execute(queries.BLOCK_TAG, (name,))
        conn.execute(queries.DELETE_COLLECTION_TAG_EVERYWHERE, (name,))
        conn.execute(queries.DELETE_MODEL_TAG_EVERYWHERE, (name,))
    log_layer("storage", 20, f"Tag {name} deleted")


def add_to_model(conn: sqlite3.Connection, model_id: int, name: str) -> None:
    tag = _existing(conn, clean(name))
    with conn:
        conn.execute(queries.INSERT_MODEL_TAG, (model_id, tag))
        for (collection_id,) in conn.execute(queries.SELECT_MODEL_COLLECTIONS, (model_id,)).fetchall():
            conn.execute(queries.DELETE_REMOVED_TAG, (collection_id, tag))


def add_to_group(conn: sqlite3.Connection, group_id: int, name: str) -> int:
    members = [r[0] for r in conn.execute(queries.SELECT_GROUP_MEMBERS, (group_id,)).fetchall()]
    for model_id in members:
        add_to_model(conn, model_id, name)
    return len(members)


def remove_from_model(conn: sqlite3.Connection, model_id: int, name: str) -> None:
    """A model shows its collections' tags too, so taking one off has to reach them."""
    with conn:
        conn.execute(queries.DELETE_MODEL_TAG, (model_id, name))
        for (collection_id,) in conn.execute(queries.SELECT_MODEL_COLLECTIONS, (model_id,)).fetchall():
            conn.execute(queries.DELETE_COLLECTION_TAG, (collection_id, name))
            conn.execute(queries.INSERT_REMOVED_TAG, (collection_id, name))


def clean_up(conn: sqlite3.Connection) -> dict[str, int]:
    """One-off tidy: dashes for spaces, and the word-split junk gone for good."""
    every = [r[0] for r in conn.execute(queries.SELECT_ALL_USED_TAGS).fetchall()]
    junk = {t for t in every if t in JUNK_WORDS or len(t) < 2 or not any(c.isalpha() for c in t)}
    respell = [t for t in every if t not in junk and clean(t) != t]
    for tag in respell:
        rename(conn, tag, tag)
    for tag in junk:
        delete(conn, tag)
    # The list was filled before these spellings were settled, so it can hold names nothing uses.
    stale = [r[0] for r in conn.execute(queries.SELECT_UNBLOCKED_TAG_NAMES).fetchall() if clean(r[0]) != r[0]]
    with conn:
        conn.executemany(queries.DELETE_TAG, [(name,) for name in stale])
    return {"respelled": len(respell), "junk_removed": len(junk), "stale_names": len(stale)}
