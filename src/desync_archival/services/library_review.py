"""Human decisions about what a linked library's models are. These always beat the resolver.

Each release's cards are saved whole, so the resolver's next guess can't touch them. A card owns
paths, either folders or single files, and the deepest claim on a path wins: a card holding a
folder gives up whatever another card claims inside it.
"""

import json
import sqlite3
from typing import Any

from desync_archival.services import model_resolver

PROPOSED = "proposed"
APPROVED = "approved"
SKIPPED = "skipped"
ADDED = "added"
GONE = "gone"

NEW_SINCE_REVIEW = "New since your review"
DEFAULT_CATEGORY = "Miniatures"


class ReviewError(ValueError):
    """The request can't be applied. The message is shown to the user."""


def carve(members_of: dict[str, list[str]], locked: set[str] | None = None) -> dict[str, list[str]]:
    """What each card must leave out: paths inside its members that belong to someone else."""
    owners = [(path, key) for key, members in members_of.items() for path in members]
    owners += [(path, None) for path in locked or ()]
    return {
        key: sorted({
            path for path, owner in owners
            if owner != key and any(path.startswith(f"{member}/") for member in members)
        })
        for key, members in members_of.items()
    }


def _within(path: str, held: set[str]) -> bool:
    parts = path.split("/")
    return any("/".join(parts[:end]) in held for end in range(1, len(parts) + 1))


def _tree(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, list[str]] | None:
    row = conn.execute(
        "SELECT folders_json, files_json FROM library_release_trees WHERE library_id = ? AND release_name = ?;",
        (library_id, release),
    ).fetchone()
    return {"folders": json.loads(row[0]), "files": json.loads(row[1])} if row else None


def save_tree(conn: sqlite3.Connection, library_id: int, run_id: int, release: str, tree: dict[str, list[str]]) -> None:
    values = (run_id, json.dumps(tree["folders"]), json.dumps(tree["files"]), library_id, release)
    updated = conn.execute(
        "UPDATE library_release_trees SET run_id = ?, folders_json = ?, files_json = ? WHERE library_id = ? AND release_name = ?;",
        values,
    )
    if not updated.rowcount:
        conn.execute(
            "INSERT INTO library_release_trees (run_id, folders_json, files_json, library_id, release_name) VALUES (?, ?, ?, ?, ?);",
            values,
        )


def _saved(conn: sqlite3.Connection, library_id: int) -> dict[str, dict[str, Any]]:
    return {
        row[0]: {"cards": json.loads(row[1]), "reviewed": bool(row[2])}
        for row in conn.execute(
            "SELECT release_name, cards_json, reviewed FROM library_release_reviews WHERE library_id = ?;", (library_id,)
        ).fetchall()
    }


def _store(conn: sqlite3.Connection, library_id: int, release: str, cards: list[dict], reviewed: bool) -> None:
    updated = conn.execute(
        "UPDATE library_release_reviews SET cards_json = ?, reviewed = ?, updated_at = CURRENT_TIMESTAMP"
        " WHERE library_id = ? AND release_name = ?;",
        (json.dumps(cards), int(reviewed), library_id, release),
    )
    if not updated.rowcount:
        conn.execute(
            "INSERT INTO library_release_reviews (library_id, release_name, cards_json, reviewed) VALUES (?, ?, ?, ?);",
            (library_id, release, json.dumps(cards), int(reviewed)),
        )


def _known_paths(conn: sqlite3.Connection, library_id: int, release: str) -> tuple[set[str], set[str]]:
    # Keys count too: a card can't take the identity of a model already in the catalog.
    prefix = f"{release}/"
    keys = {
        row[0].split("/", 1)[1] for row in conn.execute(
            "SELECT external_key FROM models WHERE library_id = ? AND external_key IS NOT NULL;", (library_id,)
        ).fetchall()
    }
    held = {
        row[0] for row in conn.execute(
            "SELECT f.rel_path FROM library_model_folders f JOIN models m ON m.id = f.model_id"
            " WHERE m.library_id = ? AND f.excluded = 0;", (library_id,)
        ).fetchall()
    }
    return {k for k in keys if k.startswith(prefix)}, {p for p in held if p.startswith(prefix)}


def _settled(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, str]:
    return dict(conn.execute(
        "SELECT rel_path, status FROM library_candidates WHERE library_id = ? AND release_name = ? AND status IN (?, ?);",
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
    carved = carve({c["key"]: c["members"] for c in cards}, keys | held)
    conn.execute(
        "DELETE FROM library_candidates WHERE library_id = ? AND release_name = ? AND status IN (?, ?);",
        (library_id, release, PROPOSED, SKIPPED),
    )
    for card in cards:
        rel_path = f"{release}/{card['key']}"
        if rel_path in settled:
            continue
        # Folders first: the first member becomes the model's vault path, and art and renders look there.
        members = sorted(card["members"], key=lambda p: (p in files, model_resolver.primary_folder_rank(p)))
        conn.execute(
            "INSERT INTO library_candidates (library_id, run_id, rel_path, release_name, name, category, folders_json,"
            " exclude_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (library_id, run_id, rel_path, release, card["name"], card["category"], json.dumps(members),
             json.dumps(carved[card["key"]]), SKIPPED if card.get("skip") else PROPOSED),
        )


def _clean(cards: list[dict], paths: set[str], locked: set[str], release: str, taken: set[str]) -> list[dict]:
    prefix = f"{release}/"
    seen: set[str] = set()
    clean = []
    for card in cards:
        name = str(card.get("name") or "").strip()
        members = [str(m) for m in card.get("members") or []]
        if not name:
            raise ReviewError("Every card needs a name.")
        if not members:
            raise ReviewError(f"{name} has nothing in it.")
        for member in members:
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
            "members": members, "skip": bool(card.get("skip")), "flags": [str(f) for f in card.get("flags") or []],
        })
    return clean


def _latest_run(conn: sqlite3.Connection, library_id: int) -> tuple[int, dict[str, Any]] | None:
    row = conn.execute(
        "SELECT id, report_json FROM library_scan_runs WHERE library_id = ? AND mode = 'preview' AND status = 'completed'"
        " ORDER BY id DESC LIMIT 1;", (library_id,),
    ).fetchone()
    return (row[0], json.loads(row[1])) if row and row[1] else None


def _code_cards(report: dict[str, Any], release: str) -> list[dict]:
    prefix = f"{release}/"
    entry = next((r for r in report.get("releases", []) if r["name"] == release), None)
    return [
        {
            "key": item["rel_path"].removeprefix(prefix), "name": item.get("name") or item["rel_path"],
            "category": item.get("category") or DEFAULT_CATEGORY, "members": item["folders"], "skip": False,
            "flags": item.get("flags") or [],
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
    return {
        "release": release,
        **tree,
        "cards": saved["cards"] if saved else _code_cards(latest[1], release),
        "reviewed": bool(saved and saved["reviewed"]),
        "changed": saved is not None,
        "locked": sorted(keys | held),
        "settled": sorted(_settled(conn, library_id, release)),
    }


def save(conn: sqlite3.Connection, library_id: int, release: str, cards: list[dict], reviewed: bool) -> dict[str, Any]:
    latest = _latest_run(conn, library_id)
    tree = _tree(conn, library_id, release)
    if not latest or tree is None:
        raise ReviewError("Run a preview first.")
    if _settled(conn, library_id, release):
        raise ReviewError(f"{release} is already approved, so its cards can't change.")
    files = set(tree["files"])
    keys, held = _known_paths(conn, library_id, release)
    prefix = f"{release}/"
    clean = _clean(cards, set(tree["folders"]) | files, keys | held, release, {k.removeprefix(prefix) for k in keys})
    with conn:
        _store(conn, library_id, release, clean, reviewed)
        write_candidates(conn, library_id, latest[0], release, clean, files)
    return {"success": True, "cards": len(clean), "skipped": sum(c["skip"] for c in clean), "reviewed": reviewed}


def reset(conn: sqlite3.Connection, library_id: int, release: str) -> dict[str, Any]:
    latest = _latest_run(conn, library_id)
    tree = _tree(conn, library_id, release)
    if not latest or tree is None:
        raise ReviewError("Run a preview first.")
    if _settled(conn, library_id, release):
        raise ReviewError(f"{release} is already approved, so its cards can't change.")
    cards = _code_cards(latest[1], release)
    with conn:
        conn.execute(
            "DELETE FROM library_release_reviews WHERE library_id = ? AND release_name = ?;", (library_id, release)
        )
        write_candidates(conn, library_id, latest[0], release, cards, set(tree["files"]))
    return {"success": True, "cards": len(cards)}


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
            members = [m for m in card["members"] if m in paths]
            changed = changed or len(members) != len(card["members"])
            if members:
                cards.append({**card, "members": members})

        covered = {m for card in cards for m in card["members"]}
        taken = {card["key"] for card in cards}
        entry = next((r for r in report["releases"] if r["name"] == release), {"new": []})
        for item in entry["new"]:
            fresh = [f for f in item["folders"] if not _within(f, covered)]
            if fresh:
                # A new folder is the one thing the human hasn't seen, so it reopens the release.
                key = _unique_key(item["rel_path"].removeprefix(f"{release}/"), taken)
                cards.append({
                    "key": key, "name": item["name"], "category": item["category"], "members": fresh,
                    "skip": False, "flags": [NEW_SINCE_REVIEW],
                })
                changed = True
        _store(conn, library_id, release, cards, saved["reviewed"] and not changed)
        write_candidates(conn, library_id, run_id, release, cards, set(tree["files"]))
    return decided


def with_reviews(conn: sqlite3.Connection, library_id: int, report: dict[str, Any]) -> dict[str, Any]:
    """The report as the human left it. Saved cards replace the resolver's guess for that release."""
    saved = _saved(conn, library_id)
    for entry in report.get("releases", []):
        review = saved.get(entry["name"])
        entry["reviewed"] = bool(review and review["reviewed"])
        entry["edited"] = review is not None
        if not review:
            continue
        keep = [c for c in review["cards"] if not c.get("skip")]
        entry["new"] = [
            {"rel_path": f"{entry['name']}/{c['key']}", "name": c["name"], "category": c["category"],
             "folders": c["members"], "flags": c.get("flags") or []}
            for c in keep
        ]
        entry["skipped"] = len(review["cards"]) - len(keep)
        entry["low_confidence"] = []
    if report.get("totals"):
        report["totals"]["new"] = sum(len(r["new"]) for r in report["releases"])
        report["totals"]["low_confidence"] = sum(len(r["low_confidence"]) for r in report["releases"])
    return report
