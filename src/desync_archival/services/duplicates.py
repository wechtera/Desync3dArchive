"""Reports files and models that look like duplicates. It reads the catalog only.

A SHA-256 match is exact. Linked libraries are not hashed, so a file without a hash can only be a
candidate, matched on name and size. The same file often belongs in several models on purpose, so
nothing here is enforced, and names like bases and supports are left out unless asked for.
"""

import json
import re
import sqlite3
from collections import defaultdict
from itertools import combinations
from typing import Any

from desync_archival.db.queries import duplicates as queries
from desync_archival.services import audit

DEFAULT_IGNORE_NAMES = "base,support"
MAX_LISTED = 200
DECISIONS = ("keep", "ignore")


def normalize_name(filename: str) -> str:
    """Strips size prefixes and punctuation so renamed copies still match."""
    # Strips a leading size or number, as in `[12.5 MB] name` or `003_name`.
    stem = re.sub(r"^\[?\d+(?:\.\d+)?\s*(?:mb|kb|gb|b)?\]?[_\-\s]+", "", filename.strip().lower())
    return re.sub(r"[^a-z0-9.]+", " ", stem).strip()


def _catalog_files(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(queries.SELECT_MODEL_FILES_BY_IS_ACTIVE).fetchall()
    columns = ("file_id", "model_id", "filename", "size_bytes", "sha256", "model_name", "source", "disk_path")
    return [dict(zip(columns, tuple(row), strict=True)) for row in rows]


def _pair_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _side(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": item["file_id"], "filename": item["filename"], "size_bytes": item["size_bytes"],
        "model_id": item["model_id"], "model_name": item["model_name"], "source": item["source"],
        "path": item["disk_path"] or "",
    }


def _open_pairs(members: list[dict[str, Any]], decided: set[tuple[int, int]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    # Two copies in one model aren't a duplicate here. Only a match across models ever was.
    return [
        (a, b) for a, b in combinations(members, 2)
        if a["model_id"] != b["model_id"] and _pair_key(a["file_id"], b["file_id"]) not in decided
    ]


def _spans_models(files: list[dict[str, Any]]) -> bool:
    return len({f["model_id"] for f in files}) > 1


def _group(files: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "filename": files[0]["filename"],
        "size_bytes": files[0]["size_bytes"],
        "models": [
            {"model_id": mid, "model_name": name, "source": source}
            for mid, name, source in sorted({(f["model_id"], f["model_name"], f["source"]) for f in files})
        ],
    }


def _model_level(groups: list[list[dict[str, Any]]], file_counts: dict[int, int], names: dict[int, str]) -> list[dict[str, Any]]:
    shared: dict[tuple[int, int], int] = defaultdict(int)
    for files in groups:
        for pair in combinations(sorted({f["model_id"] for f in files}), 2):
            shared[pair] += 1

    results = []
    for (left, right), count in shared.items():
        kind = "duplicate" if count >= file_counts[left] and count >= file_counts[right] else "overlap"
        results.append({
            "kind": kind,
            "shared_files": count,
            "models": [
                {"model_id": left, "model_name": names[left], "file_count": file_counts[left]},
                {"model_id": right, "model_name": names[right], "file_count": file_counts[right]},
            ],
        })
    results.sort(key=lambda item: (item["kind"] != "duplicate", -item["shared_files"]))
    return results


def find_duplicates(conn: sqlite3.Connection, include_shared: bool = False) -> dict[str, Any]:
    """Finds exact and likely duplicates from the catalog, without touching the NAS."""
    row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY).fetchone()
    ignored = [word.strip().lower() for word in (row[0] if row else DEFAULT_IGNORE_NAMES).split(",") if word.strip()]

    files = _catalog_files(conn)
    file_counts: dict[int, int] = defaultdict(int)
    names: dict[int, str] = {}
    for item in files:
        file_counts[item["model_id"]] += 1
        names[item["model_id"]] = item["model_name"]

    if not include_shared:
        # FIXME(R-13): a substring test, so `support` also drops every file named supported or unsupported.
        files = [f for f in files if not any(word in f["filename"].lower() for word in ignored)]

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in files:
        if item["sha256"]:
            by_hash[item["sha256"]].append(item)
        if item["size_bytes"]:
            by_name[(normalize_name(item["filename"]), item["size_bytes"])].append(item)

    decided = {(row[0], row[1]) for row in conn.execute(queries.SELECT_DECISIONS)}
    exact = [members for members in by_hash.values() if _open_pairs(members, decided)]
    # Two hashed files with the same name and size that differ in hash are proven different.
    candidate = [
        members for members in by_name.values()
        if _open_pairs(members, decided) and any(not m["sha256"] for m in members)
    ]

    file_groups = [_group(m, "exact") for m in exact] + [_group(m, "candidate") for m in candidate]
    file_groups.sort(key=lambda g: (g["kind"] != "exact", -len(g["models"]), g["filename"]))
    model_groups = _model_level(exact + candidate, file_counts, names)
    pairs = [
        {"kind": kind, "first": _side(a), "second": _side(b)}
        for kind, groups in (("exact", exact), ("candidate", candidate))
        for members in groups for a, b in _open_pairs(members, decided)
    ]

    return {
        "success": True,
        "summary": {
            "exact_groups": len(exact),
            "candidate_groups": len(candidate),
            "duplicate_models": sum(1 for g in model_groups if g["kind"] == "duplicate"),
            "overlapping_models": sum(1 for g in model_groups if g["kind"] == "overlap"),
            "ignored_names": [] if include_shared else ignored,
            "pairs": len(pairs),
        },
        "include_shared": include_shared,
        "pairs": pairs[:MAX_LISTED],
        "files": file_groups[:MAX_LISTED],
        "models": model_groups[:MAX_LISTED],
    }


class DecisionRefused(ValueError):
    """The message is shown to the user."""


def decide(conn: sqlite3.Connection, first: int, second: int, decision: str) -> dict[str, Any]:
    """Remembers a decision so a pair isn't raised again.

    Raises:
        DecisionRefused: The decision was refused.
    """
    if decision not in DECISIONS:
        raise DecisionRefused("Choose keep or ignore.")
    low, high = _pair_key(first, second)
    if low == high or conn.execute(queries.COUNT_FILES, (low, high)).fetchone()[0] != 2:
        raise DecisionRefused("Those files are no longer in the catalog.")
    with conn:
        conn.execute(queries.INSERT_DECISION, (low, high, decision))
    result = {"success": True, "first_file_id": low, "second_file_id": high, "decision": decision}
    audit.record(conn, "duplicate_decision", False, result)
    return result


def latest(conn: sqlite3.Connection) -> dict[str, Any]:
    """The last results, kept with their job so they survive leaving the page and restarts.

    Pairs someone has since acted on drop out, so the list only ever shrinks until the next run.
    """
    row = conn.execute(queries.SELECT_LATEST_RUN).fetchone()
    try:
        saved = json.loads(row[1] or "{}") if row else {}
    except ValueError:
        saved = {}
    if not saved.get("pairs"):
        return {"success": True, "pairs": [], "has_results": bool(saved), "task_id": row[0] if row else None}
    decided = {(r[0], r[1]) for r in conn.execute(queries.SELECT_DECISIONS)}
    known = {r[0] for r in conn.execute(queries.SELECT_KNOWN_FILE_IDS)}
    pairs = [
        p for p in saved["pairs"]
        if p["first"]["file_id"] in known and p["second"]["file_id"] in known
        and _pair_key(p["first"]["file_id"], p["second"]["file_id"]) not in decided
    ]
    return {**saved, "pairs": pairs, "has_results": True, "task_id": row[0], "completed_at": row[2]}
