"""Compares several LLMs' answers to one export, for choosing which LLM to trust. Writes nothing.

Each answer is checked exactly as the app would check it, and separately read for the grouping it
proposed, so an answer refused for a typo still shows how well it grouped. Groupings are compared by
which files they put together (the adjusted Rand index: 1 is the same grouping, 0 is no better than
chance), with each other and with a reference answer when there is one, such as one a person fixed.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from desync_archival.db.queries import grouping as queries
from desync_archival.services import grouping_answer
from desync_archival.services.grouping_answer import APPLY, REFUSE, SUGGEST

REFERENCE = "reference"
NAMES_SHOWN = 8


class CompareRefused(ValueError):
    """The answers can't be compared. The message says why."""


@dataclass
class Proposal:
    """What one answer proposed for one unit, in the export's short IDs."""

    models: list[dict[str, Any]] = field(default_factory=list)
    placed: dict[str, int] = field(default_factory=dict)
    groups: int = 0


def _pairs(count: int) -> float:
    return count * (count - 1) / 2


def adjusted_rand(first: dict[str, Any], second: dict[str, Any], items: list[str]) -> float:
    """How alike two groupings of the same items are. An item neither places is in a group of its own."""
    if len(items) < 2:
        return 1.0
    a = [first.get(item, ("alone", item)) for item in items]
    b = [second.get(item, ("alone", item)) for item in items]
    cells: dict[tuple, int] = {}
    rows: dict[Any, int] = {}
    cols: dict[Any, int] = {}
    for x, y in zip(a, b, strict=True):
        cells[(x, y)] = cells.get((x, y), 0) + 1
        rows[x] = rows.get(x, 0) + 1
        cols[y] = cols.get(y, 0) + 1
    index = sum(_pairs(n) for n in cells.values())
    row_pairs = sum(_pairs(n) for n in rows.values())
    col_pairs = sum(_pairs(n) for n in cols.values())
    expected = row_pairs * col_pairs / _pairs(len(items))
    most = (row_pairs + col_pairs) / 2
    if most == expected:
        return 1.0 if index == most else 0.0
    return round((index - expected) / (most - expected), 3)


def _proposal(raw: Any, artifacts: set[str]) -> Proposal:
    proposal = Proposal()
    if not isinstance(raw, dict):
        return proposal
    for index, model in enumerate(raw.get("models") or []):
        if not isinstance(model, dict):
            continue
        shorts = [s for s in model.get("artifacts") or [] if isinstance(s, str) and s in artifacts]
        for short in shorts:
            proposal.placed.setdefault(short, index)
        proposal.models.append({
            "name": str(model.get("name") or "")[:80], "artifacts": shorts,
            "image": model.get("image") if isinstance(model.get("image"), str) else None,
        })
    proposal.groups = len({m.get("group") for m in raw.get("models") or [] if isinstance(m, dict) and m.get("group")})
    return proposal


def _pictures_matching(proposal: Proposal, reference: Proposal) -> tuple[int, int]:
    """Of the reference's models with a picture, how many this answer gave the same picture, matching
    each reference model to the answer's model holding most of its files."""
    matched = total = 0
    for model in reference.models:
        if not model["image"] or not model["artifacts"]:
            continue
        total += 1
        best = max(proposal.models, key=lambda m: len(set(m["artifacts"]) & set(model["artifacts"])), default=None)
        if best and set(best["artifacts"]) & set(model["artifacts"]) and best["image"] == model["image"]:
            matched += 1
    return matched, total


def _read_one(text: str) -> tuple[dict[str, Any], str, list[str]]:
    data, notes, error = grouping_answer.read_text(text)
    if error:
        return {}, error, notes
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        return {}, "It has no units list", notes
    return data, "", notes


def _read(texts: list[str]) -> tuple[dict[str, Any], str]:
    """One LLM's answer, which may come as one file per batch. Their units are put together."""
    merged: dict[str, Any] = {"units": [], "notes": []}
    errors = []
    for number, text in enumerate(texts, 1):
        data, error, notes = _read_one(text)
        merged["notes"] += [n for n in notes if n not in merged["notes"]]
        if error:
            errors.append(f"file {number}: {error}" if len(texts) > 1 else error)
            continue
        merged.setdefault("export_id", data.get("export_id"))
        if data.get("export_id") != merged["export_id"]:
            errors.append(f"file {number} answers export {data.get('export_id')}, not {merged['export_id']}")
            continue
        merged["units"] += data["units"]
    if not merged["units"]:
        return {}, "; ".join(errors) or "It has no units"
    return merged, "; ".join(errors)


def _as_texts(answer: str | list[str]) -> list[str]:
    return [answer] if isinstance(answer, str) else list(answer)


def compare(conn: sqlite3.Connection, answers: list[tuple[str, str | list[str]]], reference: str | list[str] | None = None,
            expected: dict[str, int] | None = None) -> dict[str, Any]:
    """Checks and compares answers to one export.

    `answers` are (label, text) pairs, where the text may be a list, one per batch. `reference` is an
    answer taken as right, and `expected` a model count per unit key or collection name, such as the
    spec's sample estimates.

    Raises:
        CompareRefused: No answer names an export this database made, or they name different ones.
    """
    conn.row_factory = sqlite3.Row
    expected = expected or {}
    entries = [(label, _as_texts(text)) for label, text in answers] + ([(REFERENCE, _as_texts(reference))] if reference else [])
    parsed = {label: _read(texts) for label, texts in entries}
    export_keys = {str(data["export_id"]) for data, _error in parsed.values() if data.get("export_id")}
    if not export_keys:
        raise CompareRefused("No answer could be read with an export_id, so there's nothing to compare against.")
    if len(export_keys) > 1:
        raise CompareRefused(f"The answers name different exports: {', '.join(sorted(export_keys))}. Compare answers to one export.")
    export_key = export_keys.pop()
    export = conn.execute(queries.SELECT_EXPORT_BY_KEY, (export_key,)).fetchone()
    if not export:
        raise CompareRefused(f"Export {export_key} isn't in this database. Compare on the database that made it.")

    exported = {
        row["unit_key"]: json.loads(row["ids_json"])
        for row in conn.execute(queries.SELECT_EXPORT_UNITS, (export["id"],))
    }
    # Checked as one answer, so a unit sent twice across batches is caught as the app would catch it.
    checked = {label: grouping_answer.check(conn, json.dumps({k: v for k, v in parsed[label][0].items() if k != "notes"}))
               if parsed[label][0] else grouping_answer.check(conn, texts[0]) for label, texts in entries}

    labels = [label for label, _ in answers]
    summary: dict[str, dict[str, Any]] = {}
    for label in labels:
        data, error = parsed[label]
        report = checked[label]
        counts = {APPLY: 0, SUGGEST: 0, REFUSE: 0}
        for unit in report["units"]:
            counts[unit["outcome"]] += 1
        summary[label] = {
            "readable": bool(data) and not report["error"], "error": error or report["error"],
            "notes": data.get("notes", []),
            "units_answered": len(data.get("units") or []), "units_missing": 0, "counts": counts,
            "problems": sum(len(u["problems"]) for u in report["units"]), "warnings": sum(len(u["warnings"]) for u in report["units"]),
            "agreement": [], "expected_off": [], "pictures": [0, 0],
        }

    units = []
    for unit_key, ids in exported.items():
        items = sorted((k for k, v in ids.items() if v["kind"] == "artifact"), key=lambda s: int(s[1:]))
        artifacts = set(items)
        title = next((u["title"] for r in checked.values() for u in r["units"] if u["unit"] == unit_key and u["title"]), unit_key)
        proposals: dict[str, Proposal | None] = {}
        verdicts: dict[str, dict[str, Any] | None] = {}
        for label, _ in entries:
            data, _error = parsed[label]
            raw = next((u for u in data.get("units") or [] if isinstance(u, dict) and u.get("unit_id") == unit_key), None)
            proposals[label] = _proposal(raw, artifacts) if raw is not None else None
            found = [u for u in checked[label]["units"] if u["unit"] == unit_key]
            # The first is what the app would act on. A unit sent twice adds a refused second, whose problem counts too.
            verdicts[label] = {**found[0], "problems": [p for u in found for p in u["problems"]]} if found else None
        want = expected.get(unit_key, expected.get(title))
        row: dict[str, Any] = {"unit": unit_key, "title": title, "files": len(items), "expected": want, "answers": {}, "pairs": {}}
        for label in labels:
            proposal, verdict = proposals[label], verdicts[label]
            if proposal is None:
                summary[label]["units_missing"] += 1
                row["answers"][label] = None
                continue
            entry = {
                "outcome": verdict["outcome"] if verdict else REFUSE, "bucket": verdict["bucket"] if verdict else "",
                "problems": verdict["problems"] if verdict else [], "models": len(proposal.models), "groups": proposal.groups,
                "unplaced": len(artifacts - set(proposal.placed)),
                "pictures": sum(1 for m in proposal.models if m["image"]),
                "names": [m["name"] for m in proposal.models][:NAMES_SHOWN],
            }
            if want is not None:
                summary[label]["expected_off"].append(abs(len(proposal.models) - want))
            reference_proposal = proposals.get(REFERENCE)
            if reference_proposal is not None:
                entry["vs_reference"] = adjusted_rand(proposal.placed, reference_proposal.placed, items)
                summary[label]["agreement"].append(entry["vs_reference"])
                matched, total = _pictures_matching(proposal, reference_proposal)
                entry["pictures_matching"] = [matched, total]
                summary[label]["pictures"][0] += matched
                summary[label]["pictures"][1] += total
            row["answers"][label] = entry
        for first, second in combinations(labels, 2):
            one, other = proposals[first], proposals[second]
            if one is not None and other is not None:
                row["pairs"][f"{first} / {second}"] = adjusted_rand(one.placed, other.placed, items)
        if (truth := proposals.get(REFERENCE)) is not None:
            row["reference_models"] = len(truth.models)
        units.append(row)

    for item in summary.values():
        scores, off = item.pop("agreement"), item.pop("expected_off")
        item["vs_reference"] = round(sum(scores) / len(scores), 3) if scores else None
        item["expected_off"] = round(sum(off) / len(off), 1) if off else None
    return {"export_id": export_key, "answers": labels, "has_reference": reference is not None, "summary": summary, "units": units}


def _cell(entry: dict[str, Any] | None, has_reference: bool) -> str:
    if entry is None:
        return "not answered"
    text = f"{entry['models']} models, {entry['outcome']}"
    if entry["unplaced"]:
        text += f", {entry['unplaced']} left out"
    if has_reference and "vs_reference" in entry:
        text += f", agrees {entry['vs_reference']}"
    return text


def describe(result: dict[str, Any]) -> str:
    """The comparison as Markdown, for reading or keeping beside the answers."""
    labels, has_reference = result["answers"], result["has_reference"]
    lines = [f"# LLM answers to export {result['export_id']}", ""]
    lines += ["## Summary", "", "| Answer | Read | Apply | Wait | Refused | Problems | Units missing | Models off expected |"
              + (" Agrees with reference | Pictures as reference |" if has_reference else ""),
              "|---|---|---|---|---|---|---|---|" + ("---|---|" if has_reference else "")]
    for label in labels:
        s = result["summary"][label]
        row = (f"| {label} | {'yes' if s['readable'] else 'no: ' + s['error']} | {s['counts'][APPLY]} | {s['counts'][SUGGEST]} | "
               f"{s['counts'][REFUSE]} | {s['problems']} | {s['units_missing']} | "
               f"{'' if s['expected_off'] is None else s['expected_off']} |")
        if has_reference:
            pictures = s["pictures"]
            row += f" {'' if s['vs_reference'] is None else s['vs_reference']} | {pictures[0]} of {pictures[1]} |"
        lines.append(row)
    for label in labels:
        if notes := result["summary"][label]["notes"]:
            lines.append(f"- {label} needed cleaning up before it could be read: {'; '.join(notes)}")
    lines += ["", "Agreement is the adjusted Rand index over which files go together: 1 is the same grouping, 0 is chance.",
              "Models off expected is the average gap from the expected model count, where one was given.", ""]

    lines += ["## Units", ""]
    for row in result["units"]:
        lines.append(f"### {row['title']} ({row['unit']})")
        facts = [f"{row['files']} printable files"]
        if row["expected"] is not None:
            facts.append(f"about {row['expected']} models expected")
        if "reference_models" in row:
            facts.append(f"{row['reference_models']} in the reference")
        lines += ["", ", ".join(facts) + ".", ""]
        for label in labels:
            entry = row["answers"][label]
            lines.append(f"- **{label}:** {_cell(entry, has_reference)}")
            if entry:
                if entry["problems"]:
                    lines += [f"  - problem: {p}" for p in entry["problems"][:3]]
                lines.append(f"  - names: {', '.join(entry['names'])}{', ...' if entry['models'] > NAMES_SHOWN else ''}")
        if row["pairs"]:
            lines.append("- **Agreement between answers:** " + "; ".join(f"{pair} {score}" for pair, score in row["pairs"].items()))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
