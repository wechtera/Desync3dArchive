"""What E54 sends to an LLM for one unit, and what each short ID in it means.

A unit is an archive collection or a linked-library release. The LLM sees its files under short
IDs, a1 for a printable, i1 for an image and o1 for anything else, and answers in those IDs only.
The map from ID to artifact is kept with the export, so an answer can only ever point at files
this unit held when it went out.
"""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import grouping as queries
from desync_archival.services import model_resolver
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.image_size import size_of

ARCHIVE = "archive"
LIBRARY = "library"
COVER = "cover"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# A readme or PDF name can help, and a long list of them can't. Their names go out, never their contents.
OTHER_FILES_SHOWN = 60
# Words in an image's name that say what it shows. The LLM is told these are hints it can overrule.
ROLE_WORDS = {
    "group": {"all", "group", "set", "unit", "lineup", "family", "squad", "bundle"},
    "cover": {"cover", "banner", "header", "promo", "title"},
    "close-up": {"cu", "closeup", "close", "detail", "details"},
    "rear": {"rear", "back"},
    "side": {"side", "profile"},
    "scale": {"scale", "size", "naming", "comparison", "height"},
    "wip": {"wip", "sculpt", "unpainted", "grey", "gray"},
    "square": {"0sq", "sq", "square", "thumb", "thumbnail"},
    "painted": {"painted", "paint", "colour", "color"},
    "card": {"stats", "stat", "card", "lore", "rules"},
}


class UnitError(ValueError):
    """The unit doesn't exist, or holds nothing to group."""


@dataclass
class Unit:
    """One unit as exported. `payload` is what the LLM reads, and `ids` what each short ID means."""

    key: str
    kind: str
    reviewed: bool
    fingerprint: str
    payload: dict[str, Any]
    ids: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def lines(self) -> int:
        """Roughly how much of an LLM's context this unit takes, one line per file."""
        return len(self.payload["printables"]) + len(self.payload["images"]) + len(self.payload["other_files"]) + 10


def archive_key(collection_id: int) -> str:
    return f"{ARCHIVE}:{collection_id}"


def library_key(library_id: int, release: str) -> str:
    return f"{LIBRARY}:{library_id}:{release}"


def parse_key(key: str) -> tuple[str, int, str]:
    """(kind, collection or library ID, release). Raises UnitError for anything else."""
    kind, _, rest = str(key).partition(":")
    if kind == ARCHIVE and rest.isdigit():
        return ARCHIVE, int(rest), ""
    number, _, release = rest.partition(":")
    if kind == LIBRARY and number.isdigit() and release:
        return LIBRARY, int(number), release
    raise UnitError(f"{key} isn't a unit the app knows how to read")


def roles_of(name: str) -> list[str]:
    words = {w.lower() for w in re.split(r"[\W_]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)) if w}
    return [role for role, hints in ROLE_WORDS.items() if words & hints]


def fingerprint(entries: list[tuple[str, str, int]]) -> str:
    """Changes when a file arrives, leaves or changes size. A rename of a model doesn't touch it."""
    digest = hashlib.sha256("\n".join(f"{k}\t{p}\t{s}" for k, p, s in sorted(entries)).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def all_unit_keys(conn: sqlite3.Connection, scope: str) -> list[str]:
    keys: list[str] = []
    if scope in ("archives", "both"):
        keys += [archive_key(r[0]) for r in conn.execute(queries.SELECT_ARCHIVE_UNIT_IDS)]
    if scope in ("libraries", "both"):
        releases = {(r[0], r[1].split("/", 2)[1]) for r in conn.execute(queries.SELECT_LIBRARY_KEYS) if r[1].count("/") >= 2}
        keys += [library_key(lib, rel) for lib, rel in sorted(releases)]
    return keys


def _hints(conn: sqlite3.Connection, held: dict[int, list[str]], groups: dict[int, str], image_ids: dict[str, str]) -> list[dict]:
    """Today's models in short IDs. A hint only: the LLM is told it may be wrong."""
    if not held:
        return []
    marks = ",".join("?" * len(held))
    models = conn.execute(queries.SELECT_MODELS.format(marks=marks), list(held)).fetchall()
    hints = []
    for m in sorted(models, key=lambda r: r["name"].lower()):
        if m["image_path"] in image_ids:
            image = image_ids[m["image_path"]]
        else:
            image = COVER if m["image_source"] == COVER else None
        hints.append({
            "name": m["name"], "group": groups.get(m["id"]), "artifacts": held[m["id"]], "image": image,
            "skipped": m["state"] == "skipped",
        })
    return hints


def _tags(conn: sqlite3.Connection, model_ids: list[int]) -> list[str]:
    if not model_ids:
        return []
    marks = ",".join("?" * len(model_ids))
    return sorted({r[1] for r in conn.execute(queries.SELECT_MODEL_TAGS.format(marks=marks), model_ids)})


def _image_entry(short: str, path: str, size: int, local: Path | None) -> dict[str, Any]:
    pixels = size_of(local) if local else None
    return {
        "id": short, "path": path, "size": size, "pixels": f"{pixels[0]}x{pixels[1]}" if pixels else None,
        "roles": roles_of(PurePosixPath(path).stem), "available": bool(local),
    }


def archive_unit(conn: sqlite3.Connection, collection_id: int) -> Unit:
    conn.row_factory = sqlite3.Row
    collection = conn.execute(queries.SELECT_COLLECTION, (collection_id,)).fetchone()
    artifacts = conn.execute(queries.SELECT_ARCHIVE_ARTIFACTS, (collection_id,)).fetchall()
    if not collection or not artifacts:
        raise UnitError(f"Collection {collection_id} has no peeked archive files")

    ids: dict[str, dict[str, Any]] = {}
    printables, held, entries = [], defaultdict(list), []
    placed = {a["archive_member_id"] for a in artifacts}
    for n, a in enumerate(artifacts, 1):
        short, path = f"a{n}", f"{a['archive']}/{a['path']}"
        ids[short] = {"kind": "artifact", "id": a["id"], "path": path}
        printables.append({"id": short, "path": path, "size": a["size_bytes"] or 0})
        held[a["model_id"]].append(short)
        entries.append(("a", path, a["size_bytes"] or 0))

    members = [
        m for m in conn.execute(queries.SELECT_ARCHIVE_MEMBERS, (collection_id,)).fetchall()
        if m["id"] not in placed and not m["internal_path"].endswith("/")
        and "__macosx" not in m["internal_path"].lower() and not PurePosixPath(m["internal_path"]).name.startswith("._")
    ]
    pictures = [m for m in members if m["internal_path"].lower().endswith(IMAGE_EXTENSIONS)]
    others = [m for m in members if m not in pictures]

    # The peek names previews this way, a hash in front only where two images share a name.
    previews: dict[int, str] = {}
    for file_id in {m["file_id"] for m in pictures}:
        paths = [m["internal_path"] for m in pictures if m["file_id"] == file_id]
        names = ArchivePeeker._preview_names(paths)
        for m in pictures:
            if m["file_id"] == file_id:
                previews[m["id"]] = f"previews/{collection_id}/{file_id}/{names[m['internal_path']]}"

    root = app_config.data_dir().resolve()
    images, image_ids = [], {}
    for n, m in enumerate(pictures, 1):
        short, path, preview = f"i{n}", f"{m['archive']}/{m['internal_path']}", previews[m["id"]]
        local = root / preview
        on_disk = local if local.is_file() and local.stat().st_size > 0 else None
        ids[short] = {"kind": "image", "member": m["id"], "file_id": m["file_id"], "path": path, "preview": preview,
                      "available": bool(on_disk)}
        images.append(_image_entry(short, path, m["size_bytes"] or 0, on_disk))
        image_ids[preview] = short
        entries.append(("i", path, m["size_bytes"] or 0))

    groups = dict(conn.execute(queries.SELECT_ARCHIVE_GROUPS, (collection_id,)).fetchall())
    reviewed = conn.execute(queries.SELECT_ARCHIVE_REVIEWED, (collection_id,)).fetchone()
    source = collection["source"] or "mmf"
    # Most units come from one archive. Naming it once, not on every path, keeps a batch within an
    # LLM's context. The ID map keeps full paths, so the checks never depend on this.
    archives = sorted({a["archive"] for a in artifacts} | {m["archive"] for m in members})
    inside = f"{archives[0]}/" if len(archives) == 1 else ""

    def shown(path: str) -> str:
        return path[len(inside):] if inside and path.startswith(inside) else path

    for entry in (*printables, *images):
        entry["path"] = shown(entry["path"])
    payload = {
        "unit_id": archive_key(collection_id), "kind": ARCHIVE, "source": source,
        "collection": collection["name"], "creator": collection["creator"], "release_month": collection["release_month"] or "",
        "fingerprint": fingerprint(entries),
        "tags_allowed": source != "mmf",
        "archives": archives,
        "printables": printables, "images": images,
        "other_files": [shown(f"{m['archive']}/{m['internal_path']}") for m in others[:OTHER_FILES_SHOWN]],
        "current": {"note": "How the app groups this unit today. It may be wrong.", "models": _hints(conn, held, groups, image_ids)},
        "current_tags": _tags(conn, list(held)) if source != "mmf" else [],
    }
    return Unit(archive_key(collection_id), ARCHIVE, bool(reviewed and reviewed["reviewed"]), payload["fingerprint"], payload, ids)


def library_unit(conn: sqlite3.Connection, library_id: int, release: str) -> Unit:
    conn.row_factory = sqlite3.Row
    library = conn.execute(queries.SELECT_LIBRARY, (library_id,)).fetchone()
    prefix = f"{library_id}/{release}/"
    collections = conn.execute(queries.SELECT_RELEASE_COLLECTIONS, (library_id, prefix, prefix)).fetchall()
    artifacts = conn.execute(queries.SELECT_LIBRARY_ARTIFACTS, (library_id, prefix, prefix)).fetchall()
    if not library or not artifacts:
        raise UnitError(f"Release {release} has no models in the catalog yet")

    ids: dict[str, dict[str, Any]] = {}
    printables, held, entries = [], defaultdict(list), []
    for n, a in enumerate(artifacts, 1):
        short = f"a{n}"
        path = a["path"][len(release) + 1:] if a["path"].startswith(f"{release}/") else a["path"]
        ids[short] = {"kind": "artifact", "id": a["id"], "path": path, "collection": a["collection_id"]}
        printables.append({"id": short, "path": path, "size": a["size_bytes"] or 0})
        held[a["model_id"]].append(short)
        entries.append(("a", path, a["size_bytes"] or 0))

    root = Path(library["path"])
    images, image_ids = [], {}
    for n, row in enumerate(conn.execute(queries.SELECT_LIBRARY_IMAGES, (library_id, release)).fetchall(), 1):
        short = f"i{n}"
        path = row["rel_path"][len(release) + 1:] if row["rel_path"].startswith(f"{release}/") else row["rel_path"]
        # Library images live on the NAS until one is chosen, so no size and no local copy yet.
        ids[short] = {"kind": "image", "library_image": row["id"], "path": path, "origin": str(root / row["rel_path"]),
                      "available": False}
        images.append({"id": short, "path": path, "size": row["size_bytes"] or 0, "pixels": None,
                       "roles": roles_of(PurePosixPath(path).stem), "available": False})
        entries.append(("i", path, row["size_bytes"] or 0))

    groups = dict(conn.execute(queries.SELECT_LIBRARY_GROUPS, (library_id, release)).fetchall())
    reviewed = conn.execute(queries.SELECT_LIBRARY_REVIEWED, (library_id, release)).fetchone()
    creators = sorted({c["creator"] for c in collections if c["creator"]}) or [library["default_creator"] or library["name"]]
    months = sorted({c["release_month"] for c in collections if c["release_month"]})
    key = library_key(library_id, release)
    payload = {
        "unit_id": key, "kind": LIBRARY, "source": "library",
        "collection": release, "creator": ", ".join(creators), "release_month": months[0] if months else "",
        "fingerprint": fingerprint(entries),
        "tags_allowed": True,
        "printables": printables, "images": images, "other_files": [],
        "current": {"note": "How the app groups this unit today. It may be wrong.", "models": _hints(conn, held, groups, image_ids)},
        "current_tags": _tags(conn, list(held)),
    }
    return Unit(key, LIBRARY, bool(reviewed and reviewed["reviewed"]), payload["fingerprint"], payload, ids)


def build(conn: sqlite3.Connection, key: str) -> Unit:
    kind, number, release = parse_key(key)
    return archive_unit(conn, number) if kind == ARCHIVE else library_unit(conn, number, release)


def ids_json(unit: Unit) -> str:
    return json.dumps(unit.ids, sort_keys=True)


def current_learned_words(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """The print and piece words the build uses, for checks that compare an answer with its files."""
    from desync_archival.services import catalog_levels

    _, variants, pieces = catalog_levels.vocabulary(conn)
    return variants | set(model_resolver.PRINT_WORDS), pieces
