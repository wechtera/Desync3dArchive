"""One-off repairs for faults the resolver used to make, run with `dbtool repair-models`.

B1: a model merged into itself, after a rebuild reset a placeholder another collection held.
B2: one model twice in a collection, split by a Pre-Supported folder the resolver didn't know.
B3: groups named by a folder's slug, such as courtknights or iarcaneswordstaff.
Unreviewed collections are then rebuilt with the fixed rules, which covers B4 as well. In
reviewed collections only the rows these faults broke change, so review work stays.
"""

import logging
import sqlite3
from typing import Any

from desync_archival.db.queries import catalog_repair as queries
from desync_archival.services import audit, catalog_levels, model_resolver
from desync_archival.services.catalog_levels import PROPOSED, RESOLVED
from desync_archival.services.logger import log_layer

# Enough to check a dry run by eye. The counts cover everything.
SHOWN = 60


def fix_self_merged(conn: sqlite3.Connection, variants: set[str], pieces: set[str]) -> list[dict[str, Any]]:
    """Unmerges B1's rows and names each from its own files, as a build would have."""
    fixed = []
    for model_id, was in conn.execute(queries.SELECT_SELF_MERGED).fetchall():
        paths = [r[0] for r in conn.execute(queries.SELECT_ARTIFACT_PATHS, (model_id,))]
        resolved = model_resolver.resolve_files(paths, variants, pieces) if paths else []
        name = max(resolved, key=lambda m: len(m.files)).name if resolved else was
        reviewed = conn.execute(queries.COUNT_REVIEWED_COLLECTIONS_OF_MODEL, (model_id,)).fetchone()[0]
        conn.execute(queries.UNMERGE_MODEL, (name, RESOLVED if reviewed else PROPOSED, model_id))
        fixed.append({"id": model_id, "was": was, "now": name})
    return fixed


def _merge(conn: sqlite3.Connection, gone: int, kept: int) -> None:
    # Links, groups and tags follow the files. The emptied row redirects, as a review merge does,
    # so stash items and old links that name it still reach the model.
    conn.execute(queries.MOVE_ARTIFACTS, (kept, gone))
    conn.execute(queries.COPY_COLLECTION_LINKS, (kept, gone))
    conn.execute(queries.DELETE_COLLECTION_LINKS, (gone,))
    conn.execute(queries.COPY_GROUP_MEMBERS, (kept, gone))
    conn.execute(queries.DELETE_GROUP_MEMBERS, (gone,))
    conn.execute(queries.COPY_TAGS, (kept, gone))
    conn.execute(queries.DELETE_TAGS, (gone,))
    conn.execute(queries.TAKE_IMAGE_IF_NONE, (gone, kept))
    conn.execute(queries.REDIRECT_MERGES, (kept, gone))
    conn.execute(queries.MERGE_MODEL, (kept, gone))


def _paths(conn: sqlite3.Connection, model_id: int) -> list[str]:
    return [r[0] for r in conn.execute(queries.SELECT_ARTIFACT_PATHS, (model_id,))]


def _words(conn: sqlite3.Connection, model_id: int, variants: set[str], pieces: set[str]) -> set[str]:
    return set().union(*(model_resolver.file_words(p, variants, pieces) for p in _paths(conn, model_id)))


def _same_subject(conn: sqlite3.Connection, a: int, b: int, variants: set[str], pieces: set[str]) -> bool:
    # A name can be wrong: Blighted Fields has a "Frog Riders Pose 01" holding the Sisters Wraith.
    words = [_words(conn, m, variants, pieces) for m in (a, b)]
    if not words[0] and not words[1]:
        # Nothing but pieces, such as a figure's 25mm Base.stl beside its supported copy.
        words = [_words(conn, m, variants, set()) for m in (a, b)]
    return bool(words[0] & words[1])


def merge_same_name(
    conn: sqlite3.Connection, variants: set[str], pieces: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merges B2's duplicates into the oldest row. A placeholder's ID is a collection's, so it's lowest.

    Returns the merges, and the pairs left alone because their files name different things.
    """
    # One pair often shows in a bundle and its single, so sets are joined before anything moves.
    parent: dict[int, int] = {}

    def root(model_id: int) -> int:
        while parent.setdefault(model_id, model_id) != model_id:
            model_id = parent[model_id]
        return model_id

    skipped = []
    for row in conn.execute(queries.SELECT_SAME_NAME).fetchall():
        ids = sorted(int(i) for i in row[2].split(","))
        for other in ids[1:]:
            if not _same_subject(conn, ids[0], other, variants, pieces):
                skipped.append({"collection": row[0], "name": row[1], "ids": [ids[0], other]})
                continue
            a, b = root(ids[0]), root(other)
            if a != b:
                parent[max(a, b)] = min(a, b)

    sets: dict[int, list[int]] = {}
    for model_id in parent:
        sets.setdefault(root(model_id), []).append(model_id)
    merged = []
    for kept, members in sorted(sets.items()):
        name = conn.execute(queries.SELECT_MODEL_NAME, (kept,)).fetchone()[0]
        for gone in sorted(set(members) - {kept}):
            _merge(conn, gone, kept)
            merged.append({"kept": kept, "gone": gone, "name": name})
    return merged, skipped


def _title_from(name: str, path: str, variants: set[str]) -> str | None:
    """The title of the folder whose slug is name, or None when no folder in path has it."""
    parts = path.split("/")[:-1]
    for i, part in enumerate(parts):
        words = model_resolver.words_in(part)
        # Learned words change as archives arrive, so the slug the group was built from may have
        # kept a word that's learned since, such as The in The_Pumpka_Jackomobi_(100mm).
        slugs = {
            model_resolver.slug_of(model_resolver.subject_words(part, variants)),
            model_resolver.slug_of(words),
            model_resolver.slug_of([
                w for w in words if w.lower() not in model_resolver.PRINT_WORDS and not model_resolver.SCALE.match(w.lower())
            ]),
        }
        if name in slugs:
            # A kit's group belongs to the figure above it, so the kit folder itself is no name.
            if model_resolver.is_kit(model_resolver.words_in(part)):
                return model_resolver.group_title("/".join([*parts[:i], "file"]), variants)
            return model_resolver.folder_title(part, variants)
    return None


def rename_slug_groups(
    conn: sqlite3.Connection, variants: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Names B3's groups from the folder their slug came from. Only a name that is a folder's slug
    changes, and only when every member's folder agrees on the title.

    Returns the renames, and the groups left alone because their members came from different folders.
    """
    renamed, skipped = [], []
    for group_id, collection_id, name in conn.execute(queries.SELECT_SLUG_GROUPS).fetchall():
        paths = [r[0] for r in conn.execute(queries.SELECT_GROUP_PATHS, (group_id,))]
        titles = {t for p in paths if (t := _title_from(name, p, variants)) is not None}
        if len(titles) > 1:
            skipped.append({"collection": collection_id, "name": name, "titles": sorted(titles)})
            continue
        title = next(iter(titles), "")
        if not title or title == name:
            continue
        same = conn.execute(queries.SELECT_GROUP_BY_NAME, (collection_id, title, group_id)).fetchone()
        if same:
            conn.execute(queries.MOVE_GROUP_MEMBERS, (same[0], group_id))
            conn.execute(queries.DELETE_GROUP, (group_id,))
        else:
            conn.execute(queries.RENAME_GROUP, (title, group_id))
        renamed.append({"collection": collection_id, "was": name, "now": title, "joined": bool(same)})
    return renamed, skipped


def repair(conn: sqlite3.Connection) -> dict[str, Any]:
    """Runs every repair, then rebuilds unreviewed collections. Writes to whatever conn opens."""
    _, variants, pieces = catalog_levels.vocabulary(conn)
    before = conn.execute(queries.COUNT_LIVE_ARCHIVE_MODELS).fetchone()[0]
    with conn:
        self_merged = fix_self_merged(conn, variants, pieces)
        duplicates, different = merge_same_name(conn, variants, pieces)
        groups, mixed = rename_slug_groups(conn, variants)
    rebuilt = catalog_levels.rebuild_unreviewed(conn)
    after = conn.execute(queries.COUNT_LIVE_ARCHIVE_MODELS).fetchone()[0]
    report = {
        "models_before": before, "models_after": after, "self_merged": self_merged,
        "duplicates_merged": duplicates, "same_name_kept_apart": different, "groups_renamed": groups,
        "groups_left_mixed": mixed, "rebuilt": rebuilt,
    }
    audit.record(conn, "catalog_repair", False, {
        "models_before": before, "models_after": after, "self_merged": len(self_merged),
        "duplicates_merged": len(duplicates), "groups_renamed": len(groups), "rebuilt": rebuilt.get("reset"),
    })
    log_layer("storage", logging.WARNING, f"Catalog repair: {before} archive models before, {after} after")
    return report


def describe(report: dict[str, Any], applied: bool) -> str:
    """The report as plain lines, for a person deciding whether to apply it."""
    lines = [
        "Applied." if applied else "Dry run on a copy. Nothing changed. Add --apply to make these changes.",
        f"Archive models: {report['models_before']} before, {report['models_after']} after",
        f"B1, models merged into themselves: {len(report['self_merged'])}",
        *(f"  #{r['id']}: {r['was']} -> {r['now']}" for r in report["self_merged"][:SHOWN]),
        f"B2, duplicate models merged: {len(report['duplicates_merged'])}",
        *(f"  #{r['gone']} into #{r['kept']}: {r['name']}" for r in report["duplicates_merged"][:SHOWN]),
        f"  Kept apart, same name but different files: {len(report['same_name_kept_apart'])}",
        *(f"    collection {r['collection']}: {r['name']} #{r['ids'][0]} and #{r['ids'][1]}"
          for r in report["same_name_kept_apart"][:SHOWN]),
        f"B3, groups renamed: {len(report['groups_renamed'])}",
        *(f"  collection {r['collection']}: {r['was']} -> {r['now']}{' (joined)' if r['joined'] else ''}"
          for r in report["groups_renamed"][:SHOWN]),
        f"  Left as they are, members from different folders: {len(report['groups_left_mixed'])}",
        *(f"    collection {r['collection']}: {r['name']} ({', '.join(r['titles'])})" for r in report["groups_left_mixed"][:SHOWN]),
        (f"Unreviewed collections rebuilt: {report['rebuilt'].get('reset', 0)}, "
         f"{report['rebuilt'].get('models', 0)} models made, {report['rebuilt'].get('shared', 0)} shared"),
    ]
    return "\n".join(lines)
