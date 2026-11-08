"""Reads an LLM's answer and decides, unit by unit, what may change. Nothing here writes to the catalog.

An answer is untrusted text. It can only point at the short IDs its unit was exported with, it
can't set who made a unit or what it is, and anything that fails a check refuses that one unit,
never the whole file. What fails is written up twice: plainly for the owner, and precisely for
the LLM, so the owner can paste it back and get a corrected answer.
"""

import difflib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from desync_archival.db.queries import grouping as queries
from desync_archival.services import grouping_contract, grouping_units, model_resolver
from desync_archival.services.grouping_contract import (
    AnswerGroup,
    AnswerModel,
    AnswerUnit,
    Unplaced,
)

MAX_ANSWER_BYTES = 8 * 1024 * 1024
MISSING_SHOWN = 20
UNPLACED_SHARE = 0.10  # A high unit may leave out less than this share of its printables (E54-1).
APPLY, SUGGEST, REFUSE = "apply", "suggest", "refuse"
# Fields that say who made a unit or what it is. The batch shows them, and an answer may repeat them
# unchanged. A different value is an attempt to rewrite the catalog, and refuses the unit.
UNIT_IDENTITY = ("collection", "creator", "release_month", "source", "kind", "tags_allowed", "printables", "images",
                 "other_files", "current", "current_tags")
UNIT_FORBIDDEN = ("creator_name", "designer", "designer_name", "collection_name", "collection_id", "release",
                  "library", "library_id", "title", "path", "paths", "files", "size", "size_bytes", "model_id")
MODEL_FORBIDDEN = ("creator", "creator_name", "designer", "designer_name", "collection", "collection_name",
                   "collection_id", "source", "release", "release_month", "library", "library_id", "path", "paths",
                   "file", "files", "filename", "size", "size_bytes", "model_id", "file_id", "unit_id")
STOP_WORDS = frozenset({"the", "and", "of", "with", "for", "a", "an", "in", "on", "to", "x"})
BAD_NAME = re.compile(r"[<>{}]|://")


@dataclass
class Verdict:
    unit_key: str
    title: str = ""
    creator: str = ""
    outcome: str = REFUSE
    bucket: str = ""
    problems: list[str] = field(default_factory=list)  # Each one precise enough for the LLM to fix.
    reasons: list[str] = field(default_factory=list)  # Why a person should look, in plain words.
    warnings: list[str] = field(default_factory=list)  # What was read past or dropped.
    stale: bool = False
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None


# Reading the text ----------------------------------------------------------------------------

def _candidates(text: str) -> list[tuple[str, str]]:
    """The text as given, then with the wrapping LLMs add taken off, each with what was taken off."""
    found = [(text, "")]
    fence = re.search(r"```(?:json|JSON)?\s*\n(.*?)\n?```", text, re.DOTALL)
    if fence:
        found.append((fence.group(1), "Read the JSON inside a code fence"))
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        found.append((text[start:end + 1], "Read past text before or after the JSON"))
    return found


def read_text(text: str) -> tuple[Any, list[str], str]:
    """(data, notes, error). Tries the text as sent, then with the usual wrapping removed, and
    reports what it had to do. The error says exactly where the JSON breaks."""
    if len(text.encode("utf-8")) > MAX_ANSWER_BYTES:
        return None, [], f"The answer is over {MAX_ANSWER_BYTES // (1024 * 1024)} MB. Split it into smaller answers."
    text = text.lstrip("﻿").strip()
    if not text:
        return None, [], "The answer is empty."
    last_error = ""
    for candidate, note in _candidates(text):
        for body, fix in ((candidate, ""), (re.sub(r",(\s*[}\]])", r"\1", candidate), "Removed commas before a closing bracket")):
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                if not last_error:
                    lines = body.splitlines()
                    near = lines[e.lineno - 1].strip()[:120] if 0 < e.lineno <= len(lines) else ""
                    last_error = f"It isn't valid JSON: {e.msg} at line {e.lineno}, column {e.colno}. Near: {near}"
                continue
            return data, [n for n in (note, fix) if n], ""
    return None, [], last_error


# Checks --------------------------------------------------------------------------------------

def visible(text: Any, limit: int = 60) -> str:
    """Text from an answer, safe to quote in a message: hidden characters shown as codes, so a
    direction override can't reverse the rest of the sentence on screen."""
    out = "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs") else c for c in str(text))
    return out[:limit]


def clean_name(raw: Any) -> tuple[str, str]:
    """(name, problem). Names are shown to people, so control characters, direction overrides,
    markup and links are refused, and spaces are tidied."""
    if not isinstance(raw, str):
        return "", "isn't text"
    name = unicodedata.normalize("NFC", " ".join(raw.split()))
    if not name:
        return "", "is empty"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs") for c in name):
        return "", "holds hidden or control characters"
    if BAD_NAME.search(name):
        return "", "holds markup or a link"
    if len(name) > grouping_contract.NAME_MAX:
        return "", f"is longer than {grouping_contract.NAME_MAX} characters"
    if not any(c.isalnum() for c in name):
        return "", "has no letters or digits"
    return name, ""


def _structure_problems(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors()[:15]:
        where = ".".join(str(p) for p in item["loc"]) or "the unit"
        problems.append(f"{where}: {item['msg']}")
    return problems


def _identity_problems(raw: dict, exported: dict) -> tuple[list[str], list[str]]:
    problems, notes = [], []
    for key in UNIT_IDENTITY:
        if key in raw:
            if raw[key] == exported.get(key):
                notes.append(f"Repeated {key} unchanged, which is fine")
            else:
                problems.append(f"It changes {key}. Only the app sets that: leave {key} out of the answer.")
    for key in UNIT_FORBIDDEN:
        if key in raw:
            problems.append(f"It sets {key}. Only the app sets that: leave {key} out of the answer.")
    for index, model in enumerate(raw.get("models") or []):
        if isinstance(model, dict):
            for key in MODEL_FORBIDDEN:
                if key in model:
                    problems.append(f"models[{index}] sets {key}. Only the app sets that: leave it out.")
    return problems, notes


IGNORED = "was ignored"


def _unknown(keys: Any, known: set[str], where: str) -> list[str]:
    """A field the contract doesn't have is dropped. Saying so, with the likely name, lets a person
    or the LLM see that its alternates or tags never arrived."""
    warnings = []
    for key in sorted(str(k) for k in keys if str(k) not in known)[:5]:
        close = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]}?" if close else ""
        warnings.append(f"Unknown field {visible(key, 40)} in {where} {IGNORED}.{hint}")
    return warnings


def _unknown_fields(raw: dict) -> list[str]:
    unit_known = set(AnswerUnit.model_fields) | set(UNIT_IDENTITY) | set(UNIT_FORBIDDEN)
    warnings = _unknown(raw, unit_known, "the unit")
    for index, model in enumerate(raw.get("models") or []):
        if isinstance(model, dict):
            where = f"model {visible(model.get('name'), 40)}" if model.get("name") else f"models[{index}]"
            warnings += _unknown(model, set(AnswerModel.model_fields) | set(MODEL_FORBIDDEN), where)
    for index, group in enumerate(raw.get("groups") or []):
        if isinstance(group, dict):
            warnings += _unknown(group, set(AnswerGroup.model_fields), f"groups[{index}]")
    for index, item in enumerate(raw.get("unplaced") or []):
        if isinstance(item, dict):
            warnings += _unknown(item, set(Unplaced.model_fields), f"unplaced[{index}]")
    return warnings[:10]


def _norm(word: str) -> str:
    word = word.lower()
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _words(text: str, variants: set[str]) -> set[str]:
    raw = model_resolver.words_in(re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", text))
    return {
        _norm(w) for w in raw
        if w.lower() not in variants and w.lower() not in STOP_WORDS and not w.isdigit()
        and not model_resolver.POSE.match(w.lower()) and not w.lower().startswith("pose")
    }


def _file_words(path: str, variants: set[str], pieces: set[str]) -> set[str]:
    """What a file's own name says, plus the nearest folder that names something."""
    words = {_norm(w) for w in model_resolver.file_words(path, variants, pieces) if w not in STOP_WORDS}
    for part in reversed(PurePosixPath(path).parts[:-1]):
        if folder := _words(part, variants):
            return words | folder
    return words


def _unrelated(paths: list[str], name: str, variants: set[str], pieces: set[str]) -> list[str]:
    """Files in a model that share no word with the model's name or with the files around them.

    Kit folders and figure names tie a figure's swap parts together, so those stay connected. The
    Sisters Wraith filed as a Frog Rider pose doesn't, and is what this is for.
    """
    words = [_file_words(p, variants, pieces) for p in paths]
    hub = _words(name, variants)
    seen = [False] * len(paths)
    for start in range(len(paths)):
        if seen[start] or not (words[start] & hub):
            continue
        stack = [start]
        while stack:
            at = stack.pop()
            if seen[at]:
                continue
            seen[at] = True
            stack += [i for i in range(len(paths)) if not seen[i] and words[i] & words[at]]
    return [paths[i] for i in range(len(paths)) if not seen[i] and words[i]]


def _joins_pictured(shorts: list[str], current_models: list[dict]) -> list[str]:
    """Today's models this one takes over, when two or more had their own picture.

    One big model with many parts is normal, and Voragheel's 46 files are one. Five posed figures,
    each with its own render, folded into one is not, and a person should see it first.
    """
    taken = set(shorts)
    pictured = {}
    for hint in current_models:
        image = hint.get("image")
        if image and image != grouping_units.COVER and taken & set(hint.get("artifacts", [])):
            pictured.setdefault(image, hint["name"])
    return list(pictured.values())[:4] if len(pictured) > 1 else []


def check_unit(
    raw: Any, ids: dict[str, dict], exported: dict, current: grouping_units.Unit | None, tag_list: set[str],
    variants: set[str], pieces: set[str],
) -> Verdict:
    """One unit of an answer against exactly what was exported for it."""
    verdict = Verdict(unit_key=str(exported["unit_id"]), title=exported.get("collection", ""), creator=exported.get("creator", ""))
    current_models = exported.get("current", {}).get("models", [])
    verdict.before = {"models": len([m for m in current_models if not m.get("skipped")]),
                      "groups": len({m["group"] for m in current_models if m.get("group")})}
    if not isinstance(raw, dict):
        verdict.problems.append("Each unit must be a JSON object.")
        return verdict

    identity, notes = _identity_problems(raw, exported)
    verdict.problems += identity
    verdict.warnings += notes + _unknown_fields(raw)
    try:
        unit = AnswerUnit.model_validate(raw)
    except ValidationError as e:
        verdict.problems += _structure_problems(e)
        return verdict
    if unit.fingerprint != exported["fingerprint"]:
        verdict.problems.append(f"Its fingerprint isn't the one exported. Copy it exactly: {exported['fingerprint']}")
    if current is None or current.fingerprint != exported["fingerprint"]:
        verdict.stale = True
        verdict.reasons.append("Its files changed after this export, so the answer may not fit them. Export again.")

    artifacts = {k for k, v in ids.items() if v["kind"] == "artifact"}
    images = {k for k, v in ids.items() if v["kind"] == "image"}
    where: dict[str, list[str]] = {}

    def image_problem(ref: str | None, owner: str) -> None:
        if ref in (None, grouping_units.COVER):
            return
        if ref not in images:
            what = "a printable, not an image" if ref in artifacts else "not in this unit"
            verdict.problems.append(f"{owner} uses picture {ref}, which is {what}. Use an i-ID this unit lists, \"cover\" or null.")

    names_seen: dict[str, str] = {}
    group_names = {}
    for group in unit.groups:
        name, problem = clean_name(group.name)
        if problem:
            verdict.problems.append(f"Group name \"{visible(group.name)}\" {problem}.")
            continue
        if name.lower() in group_names:
            verdict.warnings.append(f"Group {name} is listed twice, so the two were joined.")
        group_names.setdefault(name.lower(), (name, group))
        image_problem(group.image, f"Group {name}")

    pictures: dict[str, list[int]] = {}
    for index, model in enumerate(unit.models):
        name, problem = clean_name(model.name)
        if problem:
            verdict.problems.append(f"models[{index}] \"{visible(model.name)}\": its name {problem}.")
            # Its files still count, or each would also be reported missing, which only confuses.
            name = f"models[{index}]"
        elif name.lower() in names_seen:
            verdict.problems.append(f"Two models are both called {name}. Give each its own name, or make them one model.")
        names_seen[name.lower()] = name
        for short in model.artifacts:
            where.setdefault(short, []).append(name)
            if short not in artifacts:
                what = "an image, not a printable" if short in images else "not in this unit"
                verdict.problems.append(f"{name} lists {short}, which is {what}.")
        if model.group is not None:
            group, problem = clean_name(model.group)
            if problem:
                verdict.problems.append(f"{name}'s group name {problem}.")
            elif group.lower() not in group_names:
                group_names[group.lower()] = (group, None)
                verdict.warnings.append(f"Group {group} wasn't in groups, so it was added.")
        image_problem(model.image, name)
        for alternate in model.image_alternates:
            image_problem(alternate, f"{name}'s alternates")
        if model.render_artifact is not None and model.render_artifact not in model.artifacts:
            verdict.problems.append(f"{name}'s render_artifact {model.render_artifact} isn't one of its own artifacts.")
        if model.image and model.image != grouping_units.COVER:
            pictures.setdefault(model.image, []).append(index)
        if model.tags and not exported.get("tags_allowed"):
            verdict.warnings.append(f"{name}'s tags were dropped: this unit takes no tags.")
        elif unknown := [t for t in model.tags if t not in tag_list]:
            verdict.warnings.append(f"{name}'s tags {', '.join(unknown[:5])} aren't in tags.txt, so they wait as suggestions.")

    for image, users in pictures.items():
        if len(users) > 1 and not all(unit.models[i].image_shared for i in users):
            names = ", ".join(visible(unit.models[i].name) for i in users[:4])
            verdict.problems.append(f"Picture {image} is used by {names}. Set image_shared to true on each, or give each its own.")

    for item in unit.unplaced:
        where.setdefault(item.id, []).append("unplaced")
        if item.id not in artifacts:
            verdict.problems.append(f"unplaced lists {item.id}, which isn't a printable in this unit.")
    for short, owners in where.items():
        if len(owners) > 1 and short in artifacts:
            verdict.problems.append(f"{short} is placed {len(owners)} times: in {', '.join(owners[:4])}. Place it once.")
    missing = sorted(artifacts - set(where), key=lambda s: int(s[1:]))
    if missing:
        shown = ", ".join(missing[:MISSING_SHOWN]) + (f" and {len(missing) - MISSING_SHOWN} more" if len(missing) > MISSING_SHOWN else "")
        verdict.problems.append(f"These printables are missing: {shown}. Put each in a model or in unplaced.")
    if verdict.problems:
        return verdict

    # The answer is well formed. Now: does it make sense to a person?
    path_of = {k: v["path"] for k, v in ids.items()}
    for model in unit.models:
        # Only an STL can be rendered. Rather than a render that can't happen, use the model's own STL.
        if model.render_artifact and not path_of[model.render_artifact].lower().endswith(".stl"):
            stl = next((s for s in model.artifacts if path_of[s].lower().endswith(".stl")), None)
            verdict.warnings.append(
                f"{model.name}'s render_artifact {model.render_artifact} isn't an STL, so "
                + (f"{stl} will be rendered instead." if stl else "no render will be made.")
            )
            model.render_artifact = stl
    total = len(artifacts)
    for model in unit.models:
        paths = [path_of[s] for s in model.artifacts]
        if odd := _unrelated(paths, model.name, variants, pieces):
            verdict.reasons.append(
                f"{model.name} holds files that share no name or folder with it, such as {PurePosixPath(odd[0]).name}."
            )
        if joined := _joins_pictured(model.artifacts, current_models):
            verdict.reasons.append(f"{model.name} joins {', '.join(joined)}, which each had their own picture.")
    if unit.confidence != "high":
        verdict.reasons.append(f"The LLM rated the unit {unit.confidence}: {visible(unit.reason, 300) or 'no reason given'}")
    for model in unit.models:
        if model.confidence != "high":
            verdict.reasons.append(f"The LLM rated {model.name} {model.confidence}: {visible(model.reason, 300) or 'no reason given'}")
    if len(unit.unplaced) >= UNPLACED_SHARE * total:
        verdict.reasons.append(f"{len(unit.unplaced)} of {total} files are left out of every model.")

    verdict.bucket = "low" if unit.confidence == "low" else ("high" if not verdict.reasons else "medium")
    verdict.outcome = APPLY if verdict.bucket == "high" else SUGGEST
    verdict.plan = _plan(unit, ids, group_names, tag_list, exported)
    verdict.after = {
        "models": len(unit.models), "groups": len({m.group for m in unit.models if m.group}), "unplaced": len(unit.unplaced),
        "pictures": len([m for m in unit.models if m.image]),
    }
    verdict.models = [
        {"name": clean_name(m.name)[0], "group": clean_name(m.group)[0] if m.group else "", "files": len(m.artifacts),
         "picture": PurePosixPath(path_of[m.image]).name if m.image in images else (m.image or ""),
         "picture_confidence": m.image_confidence, "confidence": m.confidence}
        for m in unit.models
    ]
    return verdict


def _clean_tags(tags: list[str]) -> list[str]:
    """Suggested tags wait for approval, but they're still text from an answer: cleaned like any
    tag, and dropped if they hold hidden characters or markup."""
    from desync_archival.services.catalog_levels import clean_tag

    kept = {clean_tag(t) for t in tags if isinstance(t, str) and not clean_name(t)[1]}
    return sorted(t for t in kept if t)[:grouping_contract.TAGS_PER_MODEL * 5]


def _plan(unit: AnswerUnit, ids: dict[str, dict], group_names: dict, tag_list: set[str], exported: dict) -> dict[str, Any]:
    """What applying would do, in catalog IDs, so apply never reads the answer's text again."""
    tags_allowed = bool(exported.get("tags_allowed"))

    def picture(ref: str | None) -> dict[str, Any] | None:
        if ref is None:
            return None
        if ref == grouping_units.COVER:
            return {"cover": True}
        return {"short": ref, **ids[ref]}

    used_groups = {clean_name(m.group)[0].lower() for m in unit.models if m.group}
    return {
        "unit_key": unit.unit_id, "fingerprint": unit.fingerprint, "confidence": unit.confidence, "reason": visible(unit.reason, 500),
        "models": [
            {
                "name": clean_name(m.name)[0],
                "group": group_names[clean_name(m.group)[0].lower()][0] if m.group else "",
                "artifacts": [ids[s]["id"] for s in m.artifacts],
                "image": picture(m.image), "image_confidence": m.image_confidence, "image_shared": m.image_shared,
                "image_reason": visible(m.image_reason, 500),
                "alternates": [picture(a) for a in m.image_alternates if a != m.image],
                "render_artifact": ids[m.render_artifact]["id"] if m.render_artifact else None,
                "tags": [t for t in m.tags if t in tag_list] if tags_allowed else [],
                # Tags it asked for that don't exist yet. Approving one adds it to these models.
                "wanted_tags": _clean_tags([t for t in m.tags if t not in tag_list]) if tags_allowed else [],
                "confidence": m.confidence, "reason": visible(m.reason, 500),
            }
            for m in unit.models
        ],
        "groups": [
            {"name": name, "image": picture(g.image) if g else None, "image_confidence": g.image_confidence if g else "low"}
            for key, (name, g) in group_names.items() if key in used_groups
        ],
        "unplaced": [{"artifact": ids[u.id]["id"], "reason": visible(u.reason, 500) or "No reason given"} for u in unit.unplaced],
        # MMF's own tags are the only ones an MMF unit gets, so it suggests none.
        "suggested_tags": _clean_tags([*unit.suggested_tags, *(t for m in unit.models for t in m.tags if t not in tag_list)])
        if tags_allowed else [],
    }


# The whole answer ----------------------------------------------------------------------------

def fix_request(export_key: str, verdicts: list[Verdict], read_error: str = "") -> str:
    """A message for the LLM: exactly what to fix, unit by unit."""
    if read_error:
        return (
            "The app couldn't read your answer. " + read_error + "\n\n"
            "Send the answer again as one JSON object matching answer-format.json, and nothing else: "
            "no prose and no markdown."
        )
    broken = [v for v in verdicts if v.problems]
    if not broken:
        return ""
    lines = [grouping_contract.FIX_INTRO.format(export_id=export_key)]
    for v in broken:
        lines.append(f"Unit {v.unit_key} ({v.title}):" if v.title else f"Unit {v.unit_key}:")
        lines += [f"- {p}" for p in v.problems]
        lines += [f"- {w}" for w in v.warnings if IGNORED in w]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def check(conn: sqlite3.Connection, text: str) -> dict[str, Any]:
    """Every unit in an answer, checked. Returns the report the dry run shows. Writes nothing."""
    conn.row_factory = sqlite3.Row
    data, notes, error = read_text(text)
    report: dict[str, Any] = {"export_id": "", "notes": notes, "error": error, "units": [], "fix_request": ""}
    if error:
        report["fix_request"] = fix_request("", [], error)
        return report
    if isinstance(data, list):
        error = "It's a list. Send one JSON object with export_id and units, as answer-format.json shows."
    elif not isinstance(data, dict) or not isinstance(data.get("units"), list) or not data.get("units"):
        error = "It needs export_id and a units list, as answer-format.json shows."
    if error or not isinstance(data, dict):
        report["error"] = error
        report["fix_request"] = fix_request("", [], error)
        return report

    export_key = str(data.get("export_id") or "")
    report["export_id"] = export_key
    export = conn.execute(queries.SELECT_EXPORT_BY_KEY, (export_key,)).fetchone()
    if not export:
        report["error"] = f"Export {export_key or '(none given)'} isn't one this app made. Answer the batch from the same zip."
        report["fix_request"] = fix_request(export_key, [], report["error"])
        return report

    variants, pieces = grouping_units.current_learned_words(conn)
    tag_list = {r[0] for r in conn.execute(queries.SELECT_TAGS)}
    verdicts: list[Verdict] = []
    seen: set[str] = set()
    for index, raw in enumerate(data["units"]):
        key = str(raw.get("unit_id") or "") if isinstance(raw, dict) else ""
        row = conn.execute(queries.SELECT_EXPORT_UNIT, (export["id"], key)).fetchone() if key else None
        if not row:
            verdict = Verdict(unit_key=key or f"units[{index}]")
            verdict.problems.append(f"Unit {key or '(no unit_id)'} wasn't in export {export_key}. Copy unit_id exactly from the batch.")
            verdicts.append(verdict)
            continue
        if key in seen:
            verdict = Verdict(unit_key=key)
            verdict.problems.append("This unit is in the answer twice. Send it once.")
            verdicts.append(verdict)
            continue
        seen.add(key)
        ids = json.loads(row["ids_json"])
        try:
            current = grouping_units.build(conn, key)
        except grouping_units.UnitError:
            current = None
        exported = _exported_payload(current, key, row["fingerprint"], ids)
        verdict = check_unit(raw, ids, exported, current, tag_list, variants, pieces)
        if current is not None and current.reviewed and verdict.outcome == APPLY:
            # A person's review outranks any answer. It becomes a suggestion for that person.
            verdict.outcome = SUGGEST
            verdict.reasons.append("A person reviewed this unit, so the answer waits for them.")
        verdicts.append(verdict)

    report["units"] = [_shown(v) for v in verdicts]
    report["fix_request"] = fix_request(export_key, verdicts)
    report["plans"] = {v.unit_key: v.plan for v in verdicts if v.plan and not v.problems and not v.stale}
    return report


def _exported_payload(current: grouping_units.Unit | None, key: str, fingerprint: str, ids: dict) -> dict[str, Any]:
    """The unit as the LLM saw it. The fingerprint and IDs come from the export; the rest, which only
    labels the report and checks echoed identity, from today's unit when its files are unchanged."""
    if current is not None and current.fingerprint == fingerprint:
        return current.payload
    return {"unit_id": key, "fingerprint": fingerprint, "collection": current.payload["collection"] if current else key,
            "creator": current.payload["creator"] if current else "", "current": {"models": []},
            "tags_allowed": current.payload["tags_allowed"] if current else False}


def _shown(v: Verdict) -> dict[str, Any]:
    outcome = REFUSE if (v.problems or v.stale) else v.outcome
    return {
        "unit": v.unit_key, "title": v.title, "creator": v.creator, "outcome": outcome, "bucket": v.bucket,
        "stale": v.stale, "problems": v.problems, "reasons": v.reasons, "warnings": v.warnings,
        "before": v.before, "after": v.after, "models": v.models,
        "summary": _summary(v, outcome),
    }


def _summary(v: Verdict, outcome: str) -> str:
    """One sentence a person can act on."""
    if v.stale:
        return "Its files changed after the export. Export it again."
    if v.problems:
        return f"Refused: {len(v.problems)} problem{'s' if len(v.problems) != 1 else ''} to fix with the LLM."
    change = f"{v.before.get('models', 0)} models become {v.after.get('models', 0)}"
    if outcome == APPLY:
        return f"Applies on confirm: {change}."
    return f"Waits for you in review: {change}. {v.reasons[0] if v.reasons else ''}".strip()


def record(conn: sqlite3.Connection, report: dict[str, Any], source_name: str) -> int:
    """Keeps a checked answer, so confirming applies exactly what the dry run showed."""
    export = conn.execute(queries.SELECT_EXPORT_BY_KEY, (report.get("export_id") or "",)).fetchone()
    plans = report.pop("plans", {}) or {}
    with conn:
        import_id = conn.execute(queries.INSERT_IMPORT, (
            export["id"] if export else None, source_name[:200], json.dumps(report), json.dumps(plans),
        )).lastrowid
    report["import_id"] = import_id
    return int(import_id or 0)
