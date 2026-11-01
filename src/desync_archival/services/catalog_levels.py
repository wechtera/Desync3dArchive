"""Fills the catalog levels from what a peek already stored. Needs no archive and no NAS.

Results are proposals until a human reviews them. Old peek rows are never touched, because stash
items cascade from them and their previews took real render time.
"""

import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import catalog_levels as queries
from desync_archival.services import model_resolver
from desync_archival.services.logger import log_layer

PROPOSED = "proposed"
RESOLVED = "resolved"
SKIPPED = "skipped"
MERGED = "merged"
PLACEHOLDER = "placeholder"
FIRST_REAL_ID = 1_000_000_000_000  # Placeholders share their collection's ID, so real models start here.
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
SCALE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s?mm\b", re.IGNORECASE)


def _attributes(path: str) -> tuple[str | None, int, int]:
    scale = SCALE.search(path)
    low = path.lower()
    unsupported = re.search(r"unsupported|nosupports?|no supports?", low)
    return (f"{scale.group(1)}mm" if scale else None, int("support" in low and not unsupported), int("hollow" in low))


def sync_loose_artifacts(conn: sqlite3.Connection, model_id: int, name: str, root: Path, found: list[tuple[Path, int, int]]) -> None:
    """Keeps a linked model's artifacts equal to its printable files. Archives wait for a peek."""
    conn.execute(
        queries.UPDATE_MODELS_NAME_BY_ID, (name, RESOLVED, model_id, PLACEHOLDER),
    )
    file_ids = dict(conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID, (model_id,)).fetchall())
    by_key = dict(conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID_V2, (model_id,)).fetchall())
    # By file, not by model: a review can move an artifact to another model, and a rescan must not
    # put a second copy back on this one.
    existing = {r[1]: r[0] for r in conn.execute(queries.SELECT_LOOSE_ARTIFACTS_OF_COLLECTION, (model_id,)).fetchall()}
    seen = set()
    for path, size, _ in found:
        if not path.name.lower().endswith(model_resolver.PRINTABLE):
            continue
        rel_path = str(path.relative_to(root))
        key = next((k for k in by_key if k and k.split("/", 1)[1] == rel_path), None)
        file_id = by_key[key] if key else file_ids.get(path.name)
        if file_id is None:
            continue
        seen.add(file_id)
        if file_id in existing:
            continue
        scale, supported, hollow = _attributes(rel_path)
        conn.execute(
            queries.INSERT_MODEL_ARTIFACTS,
            (model_id, file_id, rel_path, path.name, size, scale, supported, hollow),
        )
    for file_id, artifact_id in existing.items():
        if file_id not in seen:
            conn.execute(queries.DELETE_MODEL_ARTIFACTS_BY_ID, (artifact_id,))


def build_loose_models(
    conn: sqlite3.Connection, collection_id: int, label: str, files: list[tuple[int, Path, str, int]],
) -> None:
    """Turns imported loose files into models. The caller holds the transaction.

    A lone model takes the name it was imported under, when given one. Beside an archive it has to
    stand on its own name, because the import's name describes the whole collection.
    """
    by_path = {rel_path: (file_id, target, size) for file_id, target, rel_path, size in files}
    paths = list(by_path)
    variants = set(model_resolver.PRINT_WORDS)
    models = model_resolver.resolve_files(paths, variants)
    if len(models) == 1 and label:
        models[0].name, models[0].group = label, ""
    spare: int | None = collection_id
    for model in models:
        if spare is not None:
            model_id, spare = spare, None
            conn.execute(queries.UPDATE_MODELS_NAME_BY_ID, (model.name, RESOLVED, model_id, PLACEHOLDER))
        else:
            model_id = int(conn.execute(queries.INSERT_MODELS, (model.name, RESOLVED)).lastrowid or 0)
            conn.execute(queries.INSERT_MODEL_COLLECTIONS, (model_id, collection_id))
        for rel_path in model.files:
            file_id, target, size = by_path[rel_path]
            scale, supported, hollow = _attributes(rel_path)
            conn.execute(queries.INSERT_MODEL_ARTIFACTS, (model_id, file_id, rel_path, target.name, size, scale, supported, hollow))
        if model.group:
            conn.execute(queries.INSERT_MODEL_GROUP_MEMBERS, (_group_id(conn, collection_id, model.group), model_id))


def file_under_release(
    conn: sqlite3.Connection, model_id: int, library_id: int, release: str, group: str, tags: list[str],
) -> None:
    if group:
        row = conn.execute(
            queries.SELECT_MODEL_GROUPS_ID_BY_LIBRARY_ID, (library_id, release, group),
        ).fetchone()
        group_id = row[0] if row else conn.execute(
            queries.INSERT_MODEL_GROUPS, (library_id, release, group),
        ).lastrowid
        conn.execute(queries.INSERT_MODEL_GROUP_MEMBERS, (group_id, model_id))
    conn.executemany(
        queries.INSERT_MODEL_TAGS, [(model_id, clean_tag(t)) for t in tags],
    )


def _signature(entries: list[sqlite3.Row]) -> frozenset[tuple[str, int]] | None:
    # A strict match on purpose: joining two different designs is the one mistake that hides work.
    # Zero sizes prove nothing, so they never match.
    if any(not e["size_bytes"] for e in entries):
        return None
    return frozenset((e["filename"].lower(), e["size_bytes"]) for e in entries)


def _known_signatures(conn: sqlite3.Connection) -> dict[frozenset, int]:
    held: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(queries.SELECT_MODEL_ARTIFACTS).fetchall():
        held[row["model_id"]].append(row)
    return {sig: model_id for model_id, entries in held.items() if (sig := _signature(entries))}


def _group_id(conn: sqlite3.Connection, collection_id: int, name: str) -> int:
    row = conn.execute(queries.SELECT_MODEL_GROUPS_ID_BY_COLLECTION_ID, (collection_id, name)).fetchone()
    if row:
        return row[0]
    return int(conn.execute(queries.INSERT_MODEL_GROUPS_V2, (collection_id, name)).lastrowid or 0)


def clean_tag(tag: str) -> str:
    # One word only: a tag from any source with spaces rolls into the dashed form.
    return re.sub(r"[\s-]+", "-", tag.strip().lower()).strip("-")


def _add_artifacts(conn: sqlite3.Connection, model_id: int, entries: list[sqlite3.Row]) -> None:
    for entry in entries:
        scale, supported, hollow = _attributes(entry["internal_path"])
        conn.execute(
            queries.INSERT_MODEL_ARTIFACTS_V2,
            (model_id, entry["file_id"], entry["id"], entry["internal_path"], entry["filename"], entry["size_bytes"],
             scale, supported, hollow),
        )


def _store_model(
    conn: sqlite3.Connection, model: model_resolver.ResolvedModel, collection_id: int, spare: int | None,
    entries: list[sqlite3.Row], known: dict[frozenset, int],
) -> tuple[int, bool]:
    signature = _signature(entries)
    if signature in known:
        # A shared model keeps every copy: either archive can supply it, and each stays findable.
        model_id = known[signature]
        conn.execute(
            queries.INSERT_MODEL_COLLECTIONS, (model_id, collection_id),
        )
        _add_artifacts(conn, model_id, entries)
        return model_id, False

    if spare is not None:
        # The first model keeps the placeholder's ID, so anything pointing at it still works.
        model_id = spare
        conn.execute(queries.UPDATE_MODELS_NAME_BY_ID_V2, (model.name, PROPOSED, model_id))
    else:
        cursor = conn.execute(queries.INSERT_MODELS, (model.name, PROPOSED))
        model_id = int(cursor.lastrowid or 0)
        conn.execute(
            queries.INSERT_MODEL_COLLECTIONS, (model_id, collection_id),
        )
    _add_artifacts(conn, model_id, entries)
    if signature:
        known[signature] = model_id
    return model_id, True


def _image_score(model_slug: str, folders: set[str], image: sqlite3.Row) -> int:
    folder, _, name = image["internal_path"].rpartition("/")
    stem = model_resolver.slug_of([name.rpartition(".")[0]])
    named = bool(stem) and (model_slug in stem or stem.removesuffix("s") in model_slug)
    return (10 if named else 0) + (5 if folder in folders else 0)


def assign_images(conn: sqlite3.Connection, collection_id: int) -> int:
    """Gives each model the best picture peeked from its own archive, if one really was extracted."""
    conn.row_factory = sqlite3.Row
    images = [
        r for r in conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_MODEL_ID, (collection_id,)).fetchall()
        if r["internal_path"].lower().endswith(IMAGE_EXTENSIONS) and not r["internal_path"].startswith("/")
    ]
    models = conn.execute(queries.SELECT_MODELS_BY_MODEL_ID, (collection_id,)).fetchall()
    assigned = 0
    for model in models:
        folders = {
            r[0].rpartition("/")[0] for r in conn.execute(
                queries.SELECT_MODEL_ARTIFACTS_PATH_BY_MODEL_ID, (model["id"],)
            ).fetchall()
        }
        slug = model_resolver.slug_of(model_resolver.words_in(model["name"]))
        for image in sorted(images, key=lambda i: -_image_score(slug, folders, i)):
            if not _image_score(slug, folders, image):
                break
            name = re.sub(r"[^\w\-_\.]", "_", image["internal_path"].rpartition("/")[2])
            on_disk = app_config.previews_dir().resolve() / str(collection_id) / str(image["file_id"]) / name
            if on_disk.is_file() and on_disk.stat().st_size > 0:
                conn.execute(
                    queries.UPDATE_MODELS_IMAGE_PATH_BY_ID,
                    (f"previews/{collection_id}/{image['file_id']}/{name}", model["id"]),
                )
                assigned += 1
                break
    return assigned


def rebuild_unreviewed(conn: sqlite3.Connection) -> dict[str, Any]:
    """Throws away proposals nobody has reviewed and builds them again. Reviewed work stays."""
    conn.row_factory = sqlite3.Row
    reviewed = {r[0] for r in conn.execute(queries.SELECT_COLLECTION_REVIEWS_COLLECTION_ID_BY_REVIEWED).fetchall()}
    targets = [
        r[0] for r in conn.execute(queries.SELECT_MODEL_ARTIFACTS_MODEL_ID_BY_ARCHIVE_MEMBER_ID).fetchall() if r[0] not in reviewed
    ]
    with conn:
        for collection_id in targets:
            conn.execute(queries.DELETE_MODEL_ARTIFACTS_BY_ARCHIVE_MEMBER_ID, (collection_id,))
            conn.execute(queries.DELETE_MODEL_GROUPS_BY_COLLECTION_ID, (collection_id,))
            conn.execute(
                queries.DELETE_MODEL_COLLECTIONS_BY_COLLECTION_ID,
                (collection_id, collection_id),
            )
            conn.execute(queries.UPDATE_MODELS_NAME_BY_ID_V3, (collection_id, PLACEHOLDER, collection_id))
            conn.execute(
                queries.INSERT_MODEL_COLLECTIONS,
                (collection_id, collection_id),
            )
        conn.execute(queries.DELETE_MODELS_BY_STATE, (PROPOSED, FIRST_REAL_ID))
    return {"reset": len(targets), **build_archive_models(conn)}


def clean_orphan_links(conn: sqlite3.Connection) -> int:
    """Clears links to rows that are gone. An early rebuild ran with foreign keys off and left some.

    Not a migration, because the migration runner refuses any change in row counts. IDs here are
    never reused, so these links could never attach to the wrong thing.
    """
    with conn:
        members = conn.execute(queries.DELETE_MODEL_GROUP_MEMBERS_BY_GROUP_ID).rowcount
        tags = conn.execute(
            queries.DELETE_MODEL_TAGS_BY_MODEL_ID
        ).rowcount
    return members + tags


def build_archive_models(conn: sqlite3.Connection, only_collection: int | None = None) -> dict[str, Any]:
    """Adds whatever a peek stored that has no artifact yet, so it can run any number of times.

    Learning always reads every archive, because one archive is too small to learn from.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS).fetchall()
    printable = [r for r in rows if r["internal_path"].lower().endswith(model_resolver.PRINTABLE)]
    paths = [r["internal_path"] for r in printable]
    variants = model_resolver.learn_variant_words(sorted({p.rpartition("/")[0] for p in paths if "/" in p}))
    variants |= model_resolver.PRINT_WORDS
    pieces = model_resolver.learn_piece_words(paths, variants)

    stored = {r[0] for r in conn.execute(queries.SELECT_MODEL_ARTIFACTS_ARCHIVE_MEMBER_ID_BY_ARCHIVE_MEMBER_ID)}
    by_collection: dict[int, dict[int, list[sqlite3.Row]]] = defaultdict(lambda: defaultdict(list))
    for row in printable:
        if row["id"] not in stored and only_collection in (None, row["collection_id"]):
            by_collection[row["collection_id"]][row["file_id"]].append(row)

    known = _known_signatures(conn)
    summary: dict[str, Any] = {"collections": 0, "models": 0, "shared": 0, "groups": 0, "artifacts": 0}
    for collection_id, archives in by_collection.items():
        # New contents are something a human hasn't seen.
        conn.execute(queries.UPDATE_COLLECTION_REVIEWS_REVIEWED_BY_COLLECTION_ID, (collection_id,))
        placeholder = conn.execute(
            queries.SELECT_MODELS_STATE_BY_ID, (collection_id,)
        ).fetchone()
        spare = collection_id if placeholder and placeholder["state"] == PLACEHOLDER else None
        first = None
        groups = set()
        # Unsupported and presupported often ship as separate archives. Same shape, same model.
        merged: dict[str, tuple[model_resolver.ResolvedModel, list[sqlite3.Row]]] = {}
        for entries in archives.values():
            by_path = {e["internal_path"]: e for e in entries}
            for model in model_resolver.resolve_files(list(by_path), variants, pieces):
                held = [by_path[p] for p in model.files]
                if model.name.lower() in merged:
                    merged[model.name.lower()][1].extend(held)
                else:
                    merged[model.name.lower()] = (model, held)

        # A fresh download peeks each archive on its own, so the sibling archive's models are
        # already in the catalog by the time this one arrives.
        named = {
            r[1].lower(): r[0] for r in conn.execute(queries.SELECT_MODELS_BY_COLLECTION_ID, (collection_id, PROPOSED, RESOLVED)).fetchall()
        }
        with conn:
            for key, (model, held) in merged.items():
                if key in named:
                    model_id, created = named[key], False
                    _add_artifacts(conn, model_id, held)
                else:
                    model_id, created = _store_model(conn, model, collection_id, spare, held, known)
                if created and spare == model_id:
                    spare = None
                first = first or model_id
                conn.executemany(
                    queries.INSERT_MODEL_TAGS,
                    [(model_id, clean_tag(tag)) for tag in model.tags],
                )
                summary["models" if created else "shared"] += 1
                summary["artifacts"] += len(held)
                if model.group:
                    group_id = _group_id(conn, collection_id, model.group)
                    groups.add(group_id)
                    conn.execute(
                        queries.INSERT_MODEL_GROUP_MEMBERS, (group_id, model_id),
                    )
            if spare is not None and first is not None:
                # Everything here is shared with another collection, so the placeholder redirects.
                conn.execute(queries.UPDATE_MODELS_STATE_BY_ID, (MERGED, first, spare))
                conn.execute(
                    queries.DELETE_MODEL_COLLECTIONS_BY_MODEL_ID, (spare, collection_id),
                )
        summary["collections"] += 1
        summary["groups"] += len(groups)
    # Outside the loop, because a repaired preview can belong to a reviewed collection with nothing
    # new to build. Only models without a picture are touched, so reviewed work stays as it was.
    peeked = [only_collection] if only_collection is not None else [
        r[0] for r in conn.execute(queries.SELECT_MODEL_ARTIFACTS_MODEL_ID_BY_ARCHIVE_MEMBER_ID).fetchall()
    ]
    with conn:
        summary["images"] = sum(assign_images(conn, collection_id) for collection_id in peeked)
    log_layer("storage", 20, f"Catalog levels built from stored archive lists: {summary}")
    return summary
