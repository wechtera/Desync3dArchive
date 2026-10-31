"""Works out what models are in a pile of paths. Knows nothing about where the paths came from.

Libraries and archives both land here, so no creator's or library's words belong in this file.
The vocabulary is learned from the paths themselves: a word sitting beside many different names
describes a print (GloomPrint_Ogre, GloomPrint_Troll), and a word that doesn't names something.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import pairwise

PRINTABLE = (".stl", ".lys", ".ctb", ".chitubox", ".obj", ".3mf")
ARCHIVE = (".zip", ".rar", ".7z")

# Printing words, not library words. Learning covers the rest, but a small pile has too few
# folders to learn from, and these mean the same thing everywhere.
PRINT_WORDS = frozenset({
    "fdm", "resin", "stl", "stls", "lychee", "chitubox", "slicer", "supported", "unsupported", "presupported",
    "nosupports", "supports", "support", "hollow", "hollowed", "solid", "files", "all", "readytoslice", "models",
    "scale", "no",
})
SCALE = re.compile(r"^(fdm)?[\d.]*\d+mm$")
# Forms that are separate prints of the same subject, so they stay separate models.
SEPARATE_FORMS = ("bust",)

MIN_LEARNED_SHARE = 0.01  # NOTE: tuned on Loot, where this is about 170 different neighbours.
MIN_LEARNED_PARENTS = 8  # Below this a repeat is a coincidence, not a pattern.
TYPO_SIMILARITY = 0.86  # Catches NoSuports and ReadyToSlicer without eating short real words.
MIN_TYPO_LENGTH = 5
# A name repeated under this many unrelated parents is a part (Base, Body), not a subject.
PART_MIN_PARENTS = 3

SPLIT = re.compile(r"[_\s\-.(),&]+")
CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


@dataclass
class ResolvedModel:
    key: str
    name: str
    form: str = ""
    category: str = ""
    folders: list[str] = field(default_factory=list)
    confidence: str = "high"
    reason: str = ""


def words_in(folder: str) -> list[str]:
    return [w for w in SPLIT.split(folder) if w]


def slug_of(words: list[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", "".join(words).lower())


def learn_variant_words(folders: list[str]) -> set[str]:
    # Paths use / and exclude the file itself. Pass the whole pile: bigger piles learn better.
    # What a word sits beside is the tell. BrazenBull_32mm only ever sits beside print words, so a
    # character stays a character however many folders repeat it. Counting folders alone made any
    # well-covered character look like a variant in a small library.
    beside = defaultdict(set)
    for folder in set(folders):
        words = [w.lower() for w in words_in(folder.rpartition("/")[2])]
        for word in words:
            others = {w for w in words if w != word and w not in PRINT_WORDS and not SCALE.match(w)}
            beside[word].add(" ".join(sorted(others)))

    needed = max(MIN_LEARNED_PARENTS, int(len(set(folders)) * MIN_LEARNED_SHARE))
    learned = {word for word, seen in beside.items() if len(seen) >= needed}
    known = learned | PRINT_WORDS
    typos = {
        word for word in beside
        if len(word) >= MIN_TYPO_LENGTH and word not in known and any(
            abs(len(word) - len(other)) <= 2 and SequenceMatcher(None, word, other).ratio() >= TYPO_SIMILARITY
            for other in known if len(other) >= MIN_TYPO_LENGTH
        )
    }
    return known | typos


def learn_part_words(file_folders: list[str], variants: set[str]) -> set[str]:
    """Names that sit under many different subjects across the pile, like Base, Body or Weapon."""
    parents = defaultdict(set)
    for folder in file_folders:
        steps = [slug for part in folder.split("/") if (slug := slug_of(subject_words(part, variants)))]
        for above, slug in pairwise(steps):
            parents[slug].add(above)
    return {slug for slug, seen in parents.items() if len(seen) >= PART_MIN_PARENTS}


def _is_variant(word: str, variants: set[str]) -> bool:
    low = word.lower()
    return low in variants or bool(SCALE.match(low)) or any(low == f"{form}s" or low == form for form in SEPARATE_FORMS)


def subject_words(folder: str, variants: set[str]) -> list[str]:
    return [w for w in words_in(folder) if not _is_variant(w, variants)]


def _category_of(folder: str, variants: set[str]) -> str:
    # Enemies and Heroes get learned as variants, which is right for naming and wrong here. Only
    # true print words are skipped, so the nearest real parent word survives.
    for part in reversed(folder.split("/")[:-1]):
        words = [w for w in words_in(part) if w.lower() not in PRINT_WORDS and not SCALE.match(w.lower())]
        if words and not any(w.lower() in SEPARATE_FORMS or w.lower()[:-1] in SEPARATE_FORMS for w in words):
            return _display(words[-1:])
    return ""


def _display(words: list[str]) -> str:
    return CAMEL.sub(" ", " ".join(words)).strip()


def resolve(
    file_folders: list[str],
    all_folders: list[str] | None = None,
    variants: set[str] | None = None,
    parts: set[str] | None = None,
) -> list[ResolvedModel]:
    """file_folders hold printable files directly. Each one's path is relative to one scope.

    A scope is whatever one collection is: a release in a library, or a single archive. Variant
    folders are dropped from each path, which leaves a tree of subjects. Its leaves are the
    models, unless they're only parts of whatever sits above them.
    """
    variants = variants if variants is not None else learn_variant_words(all_folders or file_folders)

    paths: list[tuple[str, list[tuple[str, list[str]]]]] = []
    for folder in file_folders:
        steps = [(slug_of(w), w) for part in folder.split("/") if (w := subject_words(part, variants))]
        paths.append((folder, [(slug, w) for slug, w in steps if slug]))

    # Part words are learned across every scope, because one release rarely repeats them enough.
    parts = parts if parts is not None else learn_part_words(file_folders, variants)

    models: dict[tuple[str, str], ResolvedModel] = {}
    for folder, steps in paths:
        while len(steps) > 1 and steps[-1][0] in parts:
            steps = steps[:-1]

        form = next((f for f in SEPARATE_FORMS if f in folder.lower()), "")
        slug, words = steps[-1] if steps else ("", [])
        reason = "named by its folder" if steps else "no folder in its path has a usable name"
        entry = models.setdefault((slug, form), ResolvedModel(
            key=f"{slug or 'untitled'}{f'-{form}' if form else ''}",
            name=(_display(words) or "Untitled") + (f" {form.title()}" if form else ""),
            form=form,
            category=form.title() + "s" if form else _category_of(folder, variants),
            confidence="high" if steps else "low",
            reason=reason,
        ))
        entry.folders.append(folder)
    return list(models.values())
