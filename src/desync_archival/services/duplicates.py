"""Reports files and models that look like duplicates. It reads the catalog only and never acts.

A SHA-256 match is exact. Linked libraries are not hashed, so a file without a hash can only be a
candidate, matched on name and size. The same file often belongs in several models on purpose, so
nothing here is enforced, and names like bases and supports are left out unless asked for.
"""

import re
import sqlite3
from collections import defaultdict
from itertools import combinations
from typing import Any

DEFAULT_IGNORE_NAMES = "base,support"
MAX_LISTED = 200


def normalize_name(filename: str) -> str:
    stem = re.sub(r"^\[?\d+(?:\.\d+)?\s*(?:mb|kb|gb|b)?\]?[_\-\s]+", "", filename.strip().lower())
    return re.sub(r"[^a-z0-9.]+", " ", stem).strip()


def _catalog_files(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT mf.file_id, mf.model_id, mf.filename, COALESCE(df.actual_bytes, mf.size_bytes) AS size_bytes,
               df.sha256, m.name AS model_name, m.source
        FROM model_files mf
        JOIN models m ON m.id = mf.model_id
        LEFT JOIN downloaded_files df ON df.file_id = mf.file_id
        WHERE m.is_active = 1 AND mf.filename IS NOT NULL AND mf.filename != '';
    """).fetchall()
    columns = ("file_id", "model_id", "filename", "size_bytes", "sha256", "model_name", "source")
    return [dict(zip(columns, tuple(row), strict=True)) for row in rows]


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
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'duplicate_ignore_names';").fetchone()
    ignored = [word.strip().lower() for word in (row[0] if row else DEFAULT_IGNORE_NAMES).split(",") if word.strip()]

    files = _catalog_files(conn)
    file_counts: dict[int, int] = defaultdict(int)
    names: dict[int, str] = {}
    for item in files:
        file_counts[item["model_id"]] += 1
        names[item["model_id"]] = item["model_name"]

    if not include_shared:
        files = [f for f in files if not any(word in f["filename"].lower() for word in ignored)]

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in files:
        if item["sha256"]:
            by_hash[item["sha256"]].append(item)
        if item["size_bytes"]:
            by_name[(normalize_name(item["filename"]), item["size_bytes"])].append(item)

    exact = [members for members in by_hash.values() if _spans_models(members)]
    # Two hashed files with the same name and size that differ in hash are proven different.
    candidate = [
        members for members in by_name.values()
        if _spans_models(members) and any(not m["sha256"] for m in members)
    ]

    file_groups = [_group(m, "exact") for m in exact] + [_group(m, "candidate") for m in candidate]
    file_groups.sort(key=lambda g: (g["kind"] != "exact", -len(g["models"]), g["filename"]))
    model_groups = _model_level(exact + candidate, file_counts, names)

    return {
        "success": True,
        "summary": {
            "exact_groups": len(exact),
            "candidate_groups": len(candidate),
            "duplicate_models": sum(1 for g in model_groups if g["kind"] == "duplicate"),
            "overlapping_models": sum(1 for g in model_groups if g["kind"] == "overlap"),
            "ignored_names": [] if include_shared else ignored,
        },
        "files": file_groups[:MAX_LISTED],
        "models": model_groups[:MAX_LISTED],
    }
