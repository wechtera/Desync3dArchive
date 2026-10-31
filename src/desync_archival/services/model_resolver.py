"""Works out what models are in a pile of paths. Knows nothing about where the paths came from.

Libraries and archives both land here, so no creator's or library's words belong in this file.
The vocabulary is learned from the paths themselves: a word sitting beside many different names
describes a print (GloomPrint_Ogre, GloomPrint_Troll), and a word that doesn't names something.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations, pairwise
from statistics import median

PRINTABLE = (".stl", ".lys", ".ctb", ".chitubox", ".obj", ".3mf")
ARCHIVE = (".zip", ".rar", ".7z")

# Printing words, not library words. Learning covers the rest, but a small pile has too few
# folders to learn from, and these mean the same thing everywhere.
PRINT_WORDS = frozenset({
    "fdm", "resin", "stl", "stls", "lychee", "chitubox", "slicer", "supported", "unsupported", "presupported",
    "nosupports", "supports", "support", "hollow", "hollowed", "solid", "files", "all", "readytoslice", "models",
    "scale", "no", "lys", "ctb",
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

# Review signals. They only sort a human's queue, so a false alarm costs one glance.
SIMILAR_NAME = 0.86  # Same bar as typos: CanGolderProp beside CanHolderProp.
MIN_SIMILAR_LENGTH = 6
CONTAINER_MIN_MODELS = 3  # A name that also parents this many other models is a grouping folder.
OUTLIER_MIN_FOLDERS = 6
OUTLIER_SHARE = 2.5

# A lone letter or number after a shared name is a pose: Ghul_A, Ghul_B.
POSE = re.compile(r"^(?:[a-z]|p?\d{1,2})$")
# Pieces that mean the same thing everywhere. Learning covers the rest, but one archive is too small.
PIECE_WORDS = frozenset({
    "base", "bases", "body", "torso", "head", "heads", "arm", "arms", "leg", "legs", "hand", "hands", "weapon",
    "weapons", "sword", "shield", "wing", "wings", "tail", "cape", "cloak", "hair", "helmet", "foot", "feet",
})
BASE_WORDS = frozenset({"base", "basesscenic", "basesandscenic", "scenicbase"})
BASE_GROUP = "Bases"
BASE_TAG = "base"
PIECE_MIN_NAMES = 3  # A trailing word seen after this many different names is a piece, not a pose name.

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
    flags: list[str] = field(default_factory=list)
    group: str = ""
    files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def primary_folder_rank(rel_path: str) -> tuple[int, int, str]:
    """The folder a character is shown from. Unsupported 32mm renders cleanest."""
    low = rel_path.lower()
    unsupported = "unsupported" in low or "nosupports" in low.replace(" ", "")
    return (0 if unsupported else 1, 0 if "32mm" in low else 1, rel_path)


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


def group_models(models: list[ResolvedModel]) -> None:
    """Fills in related models within one scope. A bust joins its character, and bases gather up."""
    by_slug = {_slug(m): m for m in models if not m.form}
    for model in models:
        parts = [slug_of(_plain_words(p)).removesuffix("s") for f in model.folders for p in f.split("/")]
        if any(p in BASE_WORDS for p in parts):
            model.group, model.tags = BASE_GROUP, [BASE_TAG]
        elif model.form and (character := by_slug.get(_slug(model))):
            model.group = character.group = character.name


def _plain_words(folder: str) -> list[str]:
    return [w for w in words_in(folder) if w.lower() not in PRINT_WORDS and not SCALE.match(w.lower())]


def _real_words(folder: str) -> list[str]:
    # Sollid and Suppoted are print words spelled badly, not names lost to the variant list.
    return [
        w for w in _plain_words(folder)
        if not any(SequenceMatcher(None, w.lower(), known).ratio() >= 0.8 for known in PRINT_WORDS)
    ]


def _slug(model: ResolvedModel) -> str:
    return model.key.removesuffix(f"-{model.form}") if model.form else model.key


def flag_models(
    models: list[ResolvedModel],
    variants: set[str],
    parts: set[str],
    nested: dict[str, list[str]] | None = None,
) -> None:
    """Notes what a human should look at. Never changes a model: a guess stays a guess.

    models must come from one scope. nested maps a model's folder to the folders below it that
    hold printable files, which is the only way to see an item swallowed by its parent.
    """
    nested = nested or {}
    below: dict[str, set[str]] = defaultdict(set)
    for model in models:
        for folder in model.folders:
            for part in folder.split("/")[:-1]:
                if slug := slug_of(_plain_words(part)):
                    below[slug].add(model.key)

    typical = median(len(m.folders) for m in models) if models else 0
    for model in models:
        # The leaf was thrown away as a variant, so the name came from a parent. Throne ends up
        # called Objects this way, because Throne sits beside too many other words in the library.
        dropped = {
            " ".join(words) for f in model.folders
            if (leaf := f.rpartition("/")[2]) and (words := _real_words(leaf)) and not subject_words(leaf, variants)
        }
        if dropped:
            model.flags.append(f"Named from a parent folder, but its folder says {', '.join(sorted(dropped)[:3])}")
        # Busts are excluded because a release's bust pack shares the release's name by design.
        if not model.form and len(below[_slug(model)] - {model.key}) >= CONTAINER_MIN_MODELS:
            model.flags.append(f"{model.name} is also a folder grouping other models")

        inside = {
            " ".join(words) for folder in model.folders for child in nested.get(folder, [])
            if (words := subject_words(child[len(folder) + 1:].rpartition("/")[2], variants))
            and slug_of(words) not in parts and slug_of(words) != _slug(model)
        }
        if inside:
            model.flags.append(f"Holds other named items: {', '.join(sorted(inside)[:3])}")
        if len(model.folders) >= OUTLIER_MIN_FOLDERS and len(model.folders) >= typical * OUTLIER_SHARE:
            model.flags.append(f"Spans {len(model.folders)} folders, most models here span {typical:g}")

    for one, other in combinations(models, 2):
        a, b = _slug(one), _slug(other)
        # Shadow1 and Shadow2 are a set, and Hammer beside HammerProp or Male beside Female is a
        # real pair. A typo keeps the length, which is what removed most false alarms on Loot.
        if min(len(a), len(b)) < MIN_SIMILAR_LENGTH or a.rstrip("0123456789") == b.rstrip("0123456789"):
            continue
        if abs(len(a) - len(b)) > 1 or one.form != other.form:
            continue
        if SequenceMatcher(None, a, b).ratio() >= SIMILAR_NAME:
            one.flags.append(f"Very close to {other.name}, possibly the same thing")
            other.flags.append(f"Very close to {one.name}, possibly the same thing")


def _stem_words(path: str, variants: set[str]) -> tuple[str, ...]:
    stem = path.rpartition("/")[2].rpartition(".")[0]
    words = [w for raw in words_in(stem) for w in CAMEL.sub(" ", raw).split()]
    return tuple(w for w in words if not _is_variant(w, variants))


def _subject_of(path: str, variants: set[str]) -> str:
    # Scale and support folders vanish, so 28mm Supported/Ghuls and 32mm Unsupported/Ghuls are one place.
    folders = path.split("/")[:-1]
    return "/".join(slug for part in folders if (slug := slug_of(subject_words(part, variants))))


def _shared_start(stems: list[tuple[str, ...]]) -> tuple[str, ...]:
    shared: list[str] = []
    for words in zip(*stems, strict=False):
        if len({w.lower() for w in words}) > 1:
            break
        shared.append(words[0])
    return tuple(shared)


def learn_piece_words(paths: list[str], variants: set[str]) -> set[str]:
    """Trailing words shared by many differently named files, like Tentacle or Staff."""
    names = defaultdict(set)
    for path in paths:
        words = [w.lower() for w in _stem_words(path, variants)]
        if len(words) > 1:
            names[words[-1]].add(" ".join(words[:-1]))
    return set(PIECE_WORDS) | {w for w, seen in names.items() if len(seen) >= PIECE_MIN_NAMES and not POSE.match(w)}


def resolve_files(paths: list[str], variants: set[str], pieces: set[str] | None = None) -> list[ResolvedModel]:
    """Models inside one scope of printable files, usually one archive.

    Folders alone can't do this: 32mm_Ghul_A and 32mm_Ghul_B share a folder and are two models,
    while Ziadtroks and Ziadtroks_Tentacle_A share one and are a single model. Same shape, same
    model, so scale and support words are ignored and one model keeps every size.
    """
    pieces = pieces if pieces is not None else learn_piece_words(paths, variants)
    places: dict[str, dict[tuple[str, ...], list[str]]] = defaultdict(lambda: defaultdict(list))
    for path in paths:
        places[_subject_of(path, variants)][_stem_words(path, variants)].append(path)

    models: list[ResolvedModel] = []
    for place, stems in places.items():
        leaf = place.rpartition("/")[2]
        cores: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for stem, held in stems.items():
            cores[_core_of(stem, pieces)].extend(held)
        named = [c for c in cores if c]
        pure_poses = len(named) > 1 and all(POSE.match(c[-1].lower()) for c in named) and len(_shared_start(named)) > 0
        inside_folder = leaf and all(leaf.removesuffix("s") in slug_of(list(c)) for c in named)

        # Tithe Tree P1 to P8 in a Tithe Tree folder is one build. Ghul A to E in Ghuls is a set
        # of poses, and the plural folder is what tells them apart.
        # Each base is its own model, gathered in one group and tagged so they can be filtered out.
        base_pack = leaf.removesuffix("s") in BASE_WORDS
        if base_pack:
            for core, held in cores.items():
                model = _model_of(_display(list(core)) or BASE_GROUP, place, held)
                model.group, model.tags = BASE_GROUP, [BASE_TAG]
                models.append(model)
            continue

        one_build = len(named) <= 1 or (inside_folder and not (pure_poses and leaf.endswith("s")))
        if one_build:
            words = list(named[0]) if len(named) == 1 else _title_of(place, named)
            held = [p for files in cores.values() for p in files]
            # Filenames shorten names: Piotr.stl lives in Piotr Kvade - Warrior of the Hollow Moon.
            title = _folder_title(held[0], leaf, variants) if slug_of(words) and slug_of(words) in leaf else ""
            models.append(_model_of(title or _display(words) or "Untitled", place, held))
            continue

        shared = _shared_start(named)
        group = _display(list(shared)) if shared else _display([place.rpartition("/")[2]]) if leaf else ""
        for core, held in cores.items():
            model = _model_of(_display(list(core)) or group or "Untitled", place, held)
            model.group = group
            models.append(model)
    return models


def _core_of(stem: tuple[str, ...], pieces: set[str]) -> tuple[str, ...]:
    # Pieces go wherever they sit: Base_Archibald and Archibald_Base are the same model. A pose
    # ends the name, because Hussar_01_Rider and Hussar_01_Wolf build one Hussar 01.
    core: list[str] = []
    for word in stem:
        if word.lower() in pieces:
            continue
        core.append(word)
        if len(core) > 1 and POSE.match(word.lower()):
            break
    return tuple(core)


def _folder_title(path: str, leaf: str, variants: set[str]) -> str:
    # Only the tail is trimmed: Warrior of the Hollow Moon keeps a word that's a print word elsewhere.
    for part in reversed(path.split("/")[:-1]):
        if slug_of(subject_words(part, variants)) == leaf:
            words = words_in(part)
            while words and _is_variant(words[-1], variants):
                words.pop()
            return _display(words)
    return ""


def _title_of(place: str, named: list[tuple[str, ...]]) -> list[str]:
    return list(_shared_start(named)) or [place.rpartition("/")[2]]


def _model_of(name: str, place: str, files: list[str]) -> ResolvedModel:
    return ResolvedModel(key=f"{place}/{slug_of([name])}".strip("/"), name=name, files=sorted(files))
