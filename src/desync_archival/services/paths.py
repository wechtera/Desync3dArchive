"""Path name rules shared across the app."""

import re
from pathlib import Path

# Fixed on purpose: ad-hoc imports always sit here under the vault root, with no setting to move them.
IMPORT_FOLDER = "ad-hoc"
# Hidden on purpose, and fixed for the same reason.
PENDING_DELETION_FOLDER = ".Pending-Deletion"
# Slicer projects only exist once supports are placed.
SUPPORTED_PROJECT_EXTENSIONS = (".lys", ".ctb", ".ags", ".chitubox", ".prusa")
_UNSUPPORTED_WORDS = re.compile(r"(?:un|non?|not)[_\- ]?sup+ort", re.IGNORECASE)
# Doubled p allowed because Presuported ships in real packs. Lychee and ready-to-slice exports are always supported.
_SUPPORTED_WORDS = re.compile(r"sup+orted|with[_\- ]supports|support stls|lychee|readytoslice", re.IGNORECASE)


def sanitize_segment(name: str) -> str:
    """Replaces spaces and special characters with single underscores."""
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(name))
    clean = re.sub(r"_+", "_", clean)
    return clean.strip("_") or "unknown"


def says_unsupported(path_text: str) -> bool:
    return bool(_UNSUPPORTED_WORDS.search(path_text))


def is_supported(path_text: str) -> bool:
    """One rule for pre-supported files, read from a bare name or a full path."""
    if Path(path_text).suffix.lower() in SUPPORTED_PROJECT_EXTENSIONS:
        return True
    # Removed first because `unsupported` contains `supported`.
    return bool(_SUPPORTED_WORDS.search(_UNSUPPORTED_WORDS.sub("", path_text)))


def stored_filename(filename: str) -> str:
    """The name a downloaded file takes on disk, with MMF's numeric prefix removed."""
    # The name comes from MMF, so it can't be trusted to stay inside the model's folder.
    base = Path(str(filename).replace("\\", "/")).name
    if base in ("", ".", ".."):
        raise ValueError(f"Unusable file name: {filename!r}")
    cleaned = re.sub(r"^[0-9]+[_\-\s]+", "", base)
    # `12_.stl` would clean down to a hidden file with no name.
    return base if not cleaned or cleaned.startswith(".") else cleaned


def distinct_filename(name: str, file_id: int, taken: set[str]) -> str:
    """Adds the file ID when another file in the model already has this name, and records the result."""
    # Casefolded because the NAS share treats `A.stl` and `a.stl` as one file.
    while name.casefold() in taken:
        path = Path(name)
        name = f"{path.stem}_{file_id}{path.suffix}"
    taken.add(name.casefold())
    return name
