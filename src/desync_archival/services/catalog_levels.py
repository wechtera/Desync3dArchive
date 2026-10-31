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
        "UPDATE catalog_models SET name = ?, state = ? WHERE id = ? AND state = ?;", (name, RESOLVED, model_id, PLACEHOLDER),
    )
    file_ids = dict(conn.execute("SELECT filename, file_id FROM model_files WHERE model_id = ?;", (model_id,)).fetchall())
    by_key = dict(conn.execute("SELECT external_key, file_id FROM model_files WHERE model_id = ?;", (model_id,)).fetchall())
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
        scale, supported, hollow = _attributes(rel_path)
        conn.execute(
            "INSERT INTO model_artifacts (catalog_model_id, file_id, path, filename, size_bytes, scale, supported, hollow)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            (model_id, file_id, rel_path, path.name, size, scale, supported, hollow),
        )
    for row in conn.execute(
        "SELECT id, file_id FROM model_artifacts WHERE catalog_model_id = ? AND archive_member_id IS NULL;", (model_id,)
    ).fetchall():
        if row[1] not in seen:
            conn.execute("DELETE FROM model_artifacts WHERE id = ?;", (row[0],))


def file_under_release(
    conn: sqlite3.Connection, model_id: int, library_id: int, release: str, group: str, tags: list[str],
) -> None:
    if group:
        row = conn.execute(
            "SELECT id FROM model_groups WHERE library_id = ? AND release_name = ? AND name = ?;", (library_id, release, group),
        ).fetchone()
        group_id = row[0] if row else conn.execute(
            "INSERT INTO model_groups (library_id, release_name, name) VALUES (?, ?, ?);", (library_id, release, group),
        ).lastrowid
        conn.execute("INSERT INTO model_group_members (group_id, catalog_model_id) VALUES (?, ?);", (group_id, model_id))
    conn.executemany(
        "INSERT INTO catalog_model_tags (catalog_model_id, tag) VALUES (?, ?);", [(model_id, clean_tag(t)) for t in tags],
    )


def _signature(entries: list[sqlite3.Row]) -> frozenset[tuple[str, int]] | None:
    # A strict match on purpose: joining two different designs is the one mistake that hides work.
    # Zero sizes prove nothing, so they never match.
    if any(not e["size_bytes"] for e in entries):
        return None
    return frozenset((e["filename"].lower(), e["size_bytes"]) for e in entries)


def _known_signatures(conn: sqlite3.Connection) -> dict[frozenset, int]:
    held: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute("SELECT catalog_model_id, filename, size_bytes FROM model_artifacts;").fetchall():
        held[row["catalog_model_id"]].append(row)
    return {sig: model_id for model_id, entries in held.items() if (sig := _signature(entries))}


def _group_id(conn: sqlite3.Connection, collection_id: int, name: str) -> int:
    row = conn.execute("SELECT id FROM model_groups WHERE collection_id = ? AND name = ?;", (collection_id, name)).fetchone()
    if row:
        return row[0]
    return int(conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, ?);", (collection_id, name)).lastrowid or 0)


def clean_tag(tag: str) -> str:
    # One word only: a tag from any source with spaces rolls into the dashed form.
    return re.sub(r"\s+", "-", tag.strip().lower())


def _add_artifacts(conn: sqlite3.Connection, model_id: int, entries: list[sqlite3.Row]) -> None:
    for entry in entries:
        scale, supported, hollow = _attributes(entry["internal_path"])
        conn.execute(
            "INSERT INTO model_artifacts (catalog_model_id, file_id, archive_member_id, path, filename, size_bytes, scale,"
            " supported, hollow) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
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
            "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?);", (model_id, collection_id),
        )
        _add_artifacts(conn, model_id, entries)
        return model_id, False

    if spare is not None:
        # The first model keeps the placeholder's ID, so anything pointing at it still works.
        model_id = spare
        conn.execute("UPDATE catalog_models SET name = ?, state = ? WHERE id = ?;", (model.name, PROPOSED, model_id))
    else:
        cursor = conn.execute("INSERT INTO catalog_models (name, state) VALUES (?, ?);", (model.name, PROPOSED))
        model_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?);", (model_id, collection_id),
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
        r for r in conn.execute("""
            SELECT a.file_id, a.internal_path FROM file_archive_members a JOIN model_files f ON f.file_id = a.file_id
            WHERE f.model_id = ?;
        """, (collection_id,)).fetchall()
        if r["internal_path"].lower().endswith(IMAGE_EXTENSIONS) and not r["internal_path"].startswith("/")
    ]
    models = conn.execute("""
        SELECT DISTINCT m.id, m.name FROM catalog_models m JOIN model_artifacts a ON a.catalog_model_id = m.id
        JOIN model_files f ON f.file_id = a.file_id WHERE f.model_id = ? AND m.image_path IS NULL;
    """, (collection_id,)).fetchall()
    assigned = 0
    for model in models:
        folders = {
            r[0].rpartition("/")[0] for r in conn.execute(
                "SELECT path FROM model_artifacts WHERE catalog_model_id = ?;", (model["id"],)
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
                    "UPDATE catalog_models SET image_path = ? WHERE id = ?;",
                    (f"previews/{collection_id}/{image['file_id']}/{name}", model["id"]),
                )
                assigned += 1
                break
    return assigned


def rebuild_unreviewed(conn: sqlite3.Connection) -> dict[str, Any]:
    """Throws away proposals nobody has reviewed and builds them again. Reviewed work stays."""
    conn.row_factory = sqlite3.Row
    reviewed = {r[0] for r in conn.execute("SELECT collection_id FROM collection_reviews WHERE reviewed = 1;").fetchall()}
    targets = [
        r[0] for r in conn.execute("""
            SELECT DISTINCT f.model_id FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
            WHERE a.archive_member_id IS NOT NULL;
        """).fetchall() if r[0] not in reviewed
    ]
    with conn:
        for collection_id in targets:
            conn.execute("""
                DELETE FROM model_artifacts WHERE archive_member_id IS NOT NULL
                    AND file_id IN (SELECT file_id FROM model_files WHERE model_id = ?);
            """, (collection_id,))
            conn.execute("DELETE FROM model_groups WHERE collection_id = ?;", (collection_id,))
            conn.execute(
                "DELETE FROM catalog_model_collections WHERE collection_id = ? AND catalog_model_id != ?;",
                (collection_id, collection_id),
            )
            conn.execute("""
                UPDATE catalog_models SET name = (SELECT name FROM models WHERE id = ?), state = ?, merged_into = NULL,
                    image_path = NULL
                WHERE id = ?;
            """, (collection_id, PLACEHOLDER, collection_id))
            conn.execute(
                "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?);",
                (collection_id, collection_id),
            )
        conn.execute("""
            DELETE FROM catalog_models WHERE state = ? AND id >= ? AND NOT EXISTS (
                SELECT 1 FROM model_artifacts a WHERE a.catalog_model_id = catalog_models.id
            );
        """, (PROPOSED, FIRST_REAL_ID))
    return {"reset": len(targets), **build_archive_models(conn)}


def build_archive_models(conn: sqlite3.Connection, only_collection: int | None = None) -> dict[str, Any]:
    """Adds whatever a peek stored that has no artifact yet, so it can run any number of times.

    Learning always reads every archive, because one archive is too small to learn from.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT a.id, a.file_id, a.internal_path, a.filename, a.size_bytes, f.model_id AS collection_id
        FROM file_archive_members a JOIN model_files f ON f.file_id = a.file_id;
    """).fetchall()
    printable = [r for r in rows if r["internal_path"].lower().endswith(model_resolver.PRINTABLE)]
    paths = [r["internal_path"] for r in printable]
    variants = model_resolver.learn_variant_words(sorted({p.rpartition("/")[0] for p in paths if "/" in p}))
    variants |= model_resolver.PRINT_WORDS
    pieces = model_resolver.learn_piece_words(paths, variants)

    stored = {r[0] for r in conn.execute("SELECT archive_member_id FROM model_artifacts WHERE archive_member_id IS NOT NULL;")}
    by_collection: dict[int, dict[int, list[sqlite3.Row]]] = defaultdict(lambda: defaultdict(list))
    for row in printable:
        if row["id"] not in stored and only_collection in (None, row["collection_id"]):
            by_collection[row["collection_id"]][row["file_id"]].append(row)

    known = _known_signatures(conn)
    summary: dict[str, Any] = {"collections": 0, "models": 0, "shared": 0, "groups": 0, "artifacts": 0}
    for collection_id, archives in by_collection.items():
        # New contents are something a human hasn't seen.
        conn.execute("UPDATE collection_reviews SET reviewed = 0 WHERE collection_id = ?;", (collection_id,))
        placeholder = conn.execute(
            "SELECT state FROM catalog_models WHERE id = ?;", (collection_id,)
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

        with conn:
            for model, held in merged.values():
                model_id, created = _store_model(conn, model, collection_id, spare, held, known)
                if created and spare == model_id:
                    spare = None
                first = first or model_id
                conn.executemany(
                    "INSERT INTO catalog_model_tags (catalog_model_id, tag) VALUES (?, ?);",
                    [(model_id, clean_tag(tag)) for tag in model.tags],
                )
                summary["models" if created else "shared"] += 1
                summary["artifacts"] += len(held)
                if model.group:
                    group_id = _group_id(conn, collection_id, model.group)
                    groups.add(group_id)
                    conn.execute(
                        "INSERT INTO model_group_members (group_id, catalog_model_id) VALUES (?, ?);", (group_id, model_id),
                    )
            if spare is not None and first is not None:
                # Everything here is shared with another collection, so the placeholder redirects.
                conn.execute("UPDATE catalog_models SET state = ?, merged_into = ? WHERE id = ?;", (MERGED, first, spare))
                conn.execute(
                    "DELETE FROM catalog_model_collections WHERE catalog_model_id = ? AND collection_id = ?;", (spare, collection_id),
                )
        summary["collections"] += 1
        summary["groups"] += len(groups)
    # Outside the loop, because a repaired preview can belong to a reviewed collection with nothing
    # new to build. Only models without a picture are touched, so reviewed work stays as it was.
    peeked = [only_collection] if only_collection is not None else [
        r[0] for r in conn.execute("""
            SELECT DISTINCT f.model_id FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
            WHERE a.archive_member_id IS NOT NULL;
        """).fetchall()
    ]
    with conn:
        summary["images"] = sum(assign_images(conn, collection_id) for collection_id in peeked)
    log_layer("storage", 20, f"Catalog levels built from stored archive lists: {summary}")
    return summary
