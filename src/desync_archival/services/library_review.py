"""Human decisions about what a linked library's models are. These always beat the resolver.

Each release's cards are saved whole, so the resolver's next guess can't touch them. A card owns
paths, either folders or single files, and the deepest claim on a path wins: a card holding a
folder gives up whatever another card claims inside it.
"""

import json
import sqlite3
from typing import Any

from desync_archival.db.queries import library_review as queries
from desync_archival.services import model_resolver

PROPOSED = "proposed"
APPROVED = "approved"
SKIPPED = "skipped"
ADDED = "added"
GONE = "gone"

NEW_SINCE_REVIEW = "New since your review"
RENAME_MIN_FILES = 2  # One shared file name like body.stl proves nothing.
RENAME_SHARE = 0.8
DEFAULT_CATEGORY = "Miniatures"


class ReviewError(ValueError):
    """The request can't be applied. The message is shown to the user."""


def carve(paths_of: dict[str, list[str]], locked: set[str] | None = None) -> dict[str, list[str]]:
    """What each card must leave out: paths inside its paths that belong to someone else."""
    owners = [(path, key) for key, paths in paths_of.items() for path in paths]
    owners += [(path, None) for path in locked or ()]
    return {
        key: sorted({
            path for path, owner in owners
            if owner != key and any(path.startswith(f"{member}/") for member in paths)
        })
        for key, paths in paths_of.items()
    }


def _within(path: str, held: set[str]) -> bool:
    parts = path.split("/")
    return any("/".join(parts[:end]) in held for end in range(1, len(parts) + 1))


def _tree(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, list[str]] | None:
    row = conn.execute(
        queries.SELECT_LIBRARY_RELEASE_TREES_BY_LIBRARY_ID,
        (library_id, release),
    ).fetchone()
    return {"folders": json.loads(row[0]), "files": json.loads(row[1])} if row else None


def save_tree(conn: sqlite3.Connection, library_id: int, run_id: int, release: str, tree: dict[str, list[str]]) -> None:
    """Stores a release's tree, so reviewing never walks the share again."""
    values = (run_id, json.dumps(tree["folders"]), json.dumps(tree["files"]), library_id, release)
    updated = conn.execute(
        queries.UPDATE_LIBRARY_RELEASE_TREES_RUN_ID_BY_LIBRARY_ID,
        values,
    )
    if not updated.rowcount:
        conn.execute(
            queries.INSERT_LIBRARY_RELEASE_TREES,
            values,
        )


def _saved(conn: sqlite3.Connection, library_id: int) -> dict[str, dict[str, Any]]:
    return {
        row[0]: {"cards": json.loads(row[1]), "reviewed": bool(row[2])}
        for row in conn.execute(
            queries.SELECT_LIBRARY_RELEASE_REVIEWS_BY_LIBRARY_ID, (library_id,)
        ).fetchall()
    }


def _store(conn: sqlite3.Connection, library_id: int, release: str, cards: list[dict], reviewed: bool) -> None:
    updated = conn.execute(
        queries.UPDATE_LIBRARY_RELEASE_REVIEWS_CARDS_JSON_BY_LIBRARY_ID,
        (json.dumps(cards), int(reviewed), library_id, release),
    )
    if not updated.rowcount:
        conn.execute(
            queries.INSERT_LIBRARY_RELEASE_REVIEWS,
            (library_id, release, json.dumps(cards), int(reviewed)),
        )


def _known_paths(conn: sqlite3.Connection, library_id: int, release: str) -> tuple[set[str], set[str]]:
    # Keys count too: a card can't take the identity of a model already in the catalog.
    prefix = f"{release}/"
    keys = {
        row[0].split("/", 1)[1] for row in conn.execute(
            queries.SELECT_COLLECTIONS_EXTERNAL_KEY_BY_LIBRARY_ID, (library_id,)
        ).fetchall()
    }
    held = {
        row[0] for row in conn.execute(
            queries.SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_LIBRARY_ID, (library_id,)
        ).fetchall()
    }
    # Approved cards count as taken too, even before the add reaches them.
    for rel_path, folders in conn.execute(
        queries.SELECT_LIBRARY_CANDIDATES_BY_LIBRARY_ID,
        (library_id, release, APPROVED, ADDED),
    ).fetchall():
        keys.add(rel_path)
        held.update(json.loads(folders))
    return {k for k in keys if k.startswith(prefix)}, {p for p in held if p.startswith(prefix)}


def _settled(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, str]:
    return dict(conn.execute(
        queries.SELECT_LIBRARY_CANDIDATES_BY_LIBRARY_ID_RELEASE_NAME,
        (library_id, release, APPROVED, ADDED),
    ).fetchall())


def _unique_key(wanted: str, taken: set[str]) -> str:
    key, number = wanted or "card", 2
    while key in taken:
        key, number = f"{wanted or 'card'}-{number}", number + 1
    taken.add(key)
    return key


def write_candidates(
    conn: sqlite3.Connection, library_id: int, run_id: int, release: str, cards: list[dict], files: set[str],
) -> None:
    """Replaces what's undecided in a release. Approved and added rows are history, so they stay."""
    settled = _settled(conn, library_id, release)
    keys, held = _known_paths(conn, library_id, release)
    carved = carve({c["key"]: c["paths"] for c in cards}, keys | held)
    conn.execute(
        queries.DELETE_LIBRARY_CANDIDATES_BY_LIBRARY_ID,
        (library_id, release, PROPOSED, SKIPPED),
    )
    for card in cards:
        rel_path = f"{release}/{card['key']}"
        if rel_path in settled:
            continue
        # Folders first: the first member becomes the model's vault path, and art and renders look there.
        paths = sorted(card["paths"], key=lambda p: (p in files, model_resolver.primary_folder_rank(p)))
        conn.execute(
            queries.INSERT_LIBRARY_CANDIDATES,
            (library_id, run_id, rel_path, release, card["name"], card["category"], json.dumps(paths),
             json.dumps(carved[card["key"]]), SKIPPED if card.get("skip") else PROPOSED,
             card.get("group") or "", json.dumps(card.get("tags") or []), card.get("relink")),
        )


def _clean(cards: list[dict], paths: set[str], locked: set[str], release: str, taken: set[str]) -> list[dict]:
    prefix = f"{release}/"
    seen: set[str] = set()
    clean = []
    for card in cards:
        name = str(card.get("name") or "").strip()
        claimed = [str(m) for m in card.get("paths") or []]
        if not name:
            raise ReviewError("Every card needs a name.")
        if not claimed:
            raise ReviewError(f"{name} has nothing in it.")
        for member in claimed:
            if member not in paths:
                raise ReviewError(f"{member} isn't in this release's snapshot. Run a new preview.")
            if member in seen:
                raise ReviewError(f"{member} is in two cards.")
            if _within(member, locked):
                raise ReviewError(f"{member} already belongs to a model in the library.")
            seen.add(member)
        wanted = str(card.get("key") or "").removeprefix(prefix)
        key = _unique_key(wanted or model_resolver.slug_of(model_resolver.words_in(name)), taken)
        clean.append({
            "key": key, "name": name, "category": str(card.get("category") or "").strip() or DEFAULT_CATEGORY,
            "paths": claimed, "skip": bool(card.get("skip")), "flags": [str(f) for f in card.get("flags") or []],
            "group": str(card.get("group") or "").strip(), "tags": [str(t) for t in card.get("tags") or []],
            "relink": int(card["relink"]) if card.get("relink") else None,
        })
    return clean


def _latest_run(conn: sqlite3.Connection, library_id: int) -> tuple[int, dict[str, Any]] | None:
    row = conn.execute(
        queries.SELECT_LIBRARY_SCAN_RUNS_BY_LIBRARY_ID, (library_id,),
    ).fetchone()
    return (row[0], json.loads(row[1])) if row and row[1] else None


def _code_cards(report: dict[str, Any], release: str) -> list[dict]:
    prefix = f"{release}/"
    entry = next((r for r in report.get("releases", []) if r["name"] == release), None)
    return [
        {
            "key": item["rel_path"].removeprefix(prefix), "name": item.get("name") or item["rel_path"],
            "category": item.get("category") or DEFAULT_CATEGORY, "paths": item["folders"], "skip": False,
            "flags": item.get("flags") or [], "group": item.get("group") or "", "tags": item.get("tags") or [],
        }
        for item in (entry or {}).get("new", [])
    ]


def release_view(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, Any]:
    latest = _latest_run(conn, library_id)
    tree = _tree(conn, library_id, release)
    if not latest or tree is None:
        raise ReviewError("Run a preview first.")
    saved = _saved(conn, library_id).get(release)
    keys, held = _known_paths(conn, library_id, release)
    settled = _settled(conn, library_id, release)
    cards = saved["cards"] if saved else _code_cards(latest[1], release)
    return {
        "release": release,
        **tree,
        "cards": [c for c in cards if f"{release}/{c['key']}" not in settled],
        "reviewed": bool(saved and saved["reviewed"]),
        "changed": saved is not None,
        "locked": sorted(keys | held),
        "settled": sorted(settled),
    }


def save(conn: sqlite3.Connection, library_id: int, release: str, cards: list[dict], reviewed: bool) -> dict[str, Any]:
    """Saves a human's cards. Added cards are history and stay as they were.

    Raises:
        ReviewError: The review can't be applied.
    """
    latest = _latest_run(conn, library_id)
    tree = _tree(conn, library_id, release)
    if not latest or tree is None:
        raise ReviewError("Run a preview first.")
    # Added cards are history and stay as they were. The rest of the release stays open.
    settled = _settled(conn, library_id, release)
    earlier = _saved(conn, library_id).get(release, {}).get("cards") or _code_cards(latest[1], release)
    kept = [c for c in earlier if f"{release}/{c['key']}" in settled]
    files = set(tree["files"])
    keys, held = _known_paths(conn, library_id, release)
    prefix = f"{release}/"
    clean = _clean(cards, set(tree["folders"]) | files, keys | held, release, {k.removeprefix(prefix) for k in keys})
    with conn:
        _store(conn, library_id, release, [*kept, *clean], reviewed)
        write_candidates(conn, library_id, latest[0], release, [*kept, *clean], files)
    return {"success": True, "cards": len(clean), "skipped": sum(c["skip"] for c in clean), "reviewed": reviewed}


def reset(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, Any]:
    """Puts a release back to the resolver's guess.

    Raises:
        ReviewError: The review can't be applied.
    """
    latest = _latest_run(conn, library_id)
    tree = _tree(conn, library_id, release)
    if not latest or tree is None:
        raise ReviewError("Run a preview first.")
    cards = _code_cards(latest[1], release)
    with conn:
        conn.execute(
            queries.DELETE_LIBRARY_RELEASE_REVIEWS_BY_LIBRARY_ID, (library_id, release)
        )
        write_candidates(conn, library_id, latest[0], release, cards, set(tree["files"]))
    return {"success": True, "cards": len(cards)}


def flag_renames(conn: sqlite3.Connection, library_id: int, report: dict[str, Any], trees: dict[str, dict]) -> None:
    """A folder rename looks like a new model and a lost one. Matching files suggest the link."""
    lost = {
        row[0]: (row[1], {r[0] for r in conn.execute(queries.SELECT_COLLECTION_FILENAMES, (row[0],)).fetchall()})
        for row in conn.execute(queries.SELECT_INACTIVE_LINKED, (library_id,)).fetchall()
    }
    lost = {key: value for key, value in lost.items() if len(value[1]) >= RENAME_MIN_FILES}
    if not lost:
        return
    for entry in report["releases"]:
        files = trees.get(entry["name"], {}).get("files", [])
        for item in entry["new"]:
            names = {f.rpartition("/")[2].lower() for f in files if any(f.startswith(f"{d}/") for d in item["folders"])}
            for collection_id, (name, old) in lost.items():
                if names and len(names & old) / len(names | old) >= RENAME_SHARE:
                    item.setdefault("flags", []).append(f"Looks like a rename of {name} (#{collection_id})")
                    break


def merge_preview(conn: sqlite3.Connection, library_id: int, run_id: int, report: dict[str, Any], trees: dict[str, dict]) -> set[str]:
    """Carries saved cards onto a new preview. Returns the releases a human has already decided."""
    decided = set()
    for release, saved in _saved(conn, library_id).items():
        tree = trees.get(release)
        if tree is None:
            continue
        decided.add(release)
        paths = set(tree["folders"]) | set(tree["files"])
        cards, changed = [], False
        for card in saved["cards"]:
            surviving = [m for m in card["paths"] if m in paths]
            changed = changed or len(surviving) != len(card["paths"])
            if surviving:
                cards.append({**card, "paths": surviving})

        covered = {m for card in cards for m in card["paths"]}
        taken = {card["key"] for card in cards}
        entry = next((r for r in report["releases"] if r["name"] == release), {"new": []})
        for item in entry["new"]:
            fresh = [f for f in item["folders"] if not _within(f, covered)]
            if fresh:
                # A new folder is the one thing the human hasn't seen, so it reopens the release.
                key = _unique_key(item["rel_path"].removeprefix(f"{release}/"), taken)
                cards.append({
                    "key": key, "name": item["name"], "category": item["category"], "paths": fresh,
                    "skip": False, "flags": [NEW_SINCE_REVIEW], "group": item.get("group") or "",
                    "tags": item.get("tags") or [],
                })
                changed = True
        _store(conn, library_id, release, cards, saved["reviewed"] and not changed)
        write_candidates(conn, library_id, run_id, release, cards, set(tree["files"]))
    return decided


def _drop_settled(entry: dict[str, Any], settled: set[str]) -> None:
    waiting = [item for item in entry["new"] if item["rel_path"] not in settled]
    entry["added"] = len(entry["new"]) - len(waiting)
    entry["new"] = waiting
    entry["ready"] = sum(_is_ready(entry, item) for item in waiting)


def _is_ready(entry: dict[str, Any], item: dict[str, Any]) -> bool:
    # Trusted means a human looked, or the code raised nothing worth looking at.
    return bool(entry.get("reviewed")) or not (
        item.get("flags") or any(note.startswith(f"{item['rel_path']}:") for note in entry["low_confidence"])
    )


def approve_ready(conn: sqlite3.Connection, library_id: int, hold_releases: list[str]) -> dict[str, int]:
    """Approves what's reviewed or unflagged. The rest stays proposed, so nothing is lost by waiting."""
    latest = _latest_run(conn, library_id)
    if not latest:
        raise ReviewError("Run a preview first.")
    report = with_reviews(conn, library_id, latest[1])
    ready = [
        item["rel_path"] for entry in report["releases"] if entry["name"] not in hold_releases
        for item in entry["new"] if _is_ready(entry, item)
    ]
    with conn:
        approved = sum(
            conn.execute(
                queries.UPDATE_LIBRARY_CANDIDATES_STATUS_BY_LIBRARY_ID,
                (APPROVED, library_id, rel_path, PROPOSED),
            ).rowcount
            for rel_path in ready
        )
    waiting = conn.execute(
        queries.COUNT_LIBRARY_CANDIDATES_BY_LIBRARY_ID, (library_id, PROPOSED)
    ).fetchone()[0]
    return {"approved": approved, "waiting": waiting}


def with_reviews(conn: sqlite3.Connection, library_id: int, report: dict[str, Any]) -> dict[str, Any]:
    """The report as the human left it. Saved cards replace the resolver's guess for that release."""
    saved = _saved(conn, library_id)
    settled = {r[0] for r in conn.execute(
        queries.SELECT_LIBRARY_CANDIDATES_REL_PATH_BY_LIBRARY_ID, (library_id, APPROVED, ADDED)
    ).fetchall()}
    for entry in report.get("releases", []):
        review = saved.get(entry["name"])
        entry["reviewed"] = bool(review and review["reviewed"])
        entry["edited"] = review is not None
        if not review:
            _drop_settled(entry, settled)
            continue
        keep = [c for c in review["cards"] if not c.get("skip")]
        entry["new"] = [
            {"rel_path": f"{entry['name']}/{c['key']}", "name": c["name"], "category": c["category"],
             "folders": c["paths"], "flags": c.get("flags") or [], "group": c.get("group") or ""}
            for c in keep
        ]
        entry["skipped"] = len(review["cards"]) - len(keep)
        entry["low_confidence"] = []
        _drop_settled(entry, settled)
    if report.get("totals"):
        report["totals"]["ready"] = sum(r["ready"] for r in report["releases"])
        report["totals"]["new"] = sum(len(r["new"]) for r in report["releases"])
        report["totals"]["low_confidence"] = sum(len(r["low_confidence"]) for r in report["releases"])
    return report
