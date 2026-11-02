"""Path name rules shared across the app."""

import re
import shutil
import time
from pathlib import Path

# Fixed on purpose: ad-hoc imports always sit here under the vault root, with no setting to move them.
IMPORT_FOLDER = "ad-hoc"
# Hidden on purpose, and fixed for the same reason.
PENDING_DELETION_FOLDER = ".Pending-Deletion"
# Whatever a slicer or a mesh tool opens. Exports and archive peeks go by this list.
MODEL_EXTENSIONS = {".stl", ".obj", ".lys", ".ctb", ".ags", ".3mf", ".chitubox", ".prusa"}
# Narrower on purpose until the owner decides (Q15). A library scan records these and archives, and
# the resolver groups the second list. Widening either adds files to the catalog on the next scan.
SCANNED_EXTENSIONS = {".stl", ".lys", ".ctb", ".chitubox", ".zip", ".rar", ".7z"}
GROUPED_EXTENSIONS = (".stl", ".lys", ".ctb", ".chitubox", ".obj", ".3mf")
# Past this a work folder is a crash's leftover. No import or export runs this long.
LEFTOVER_AGE_SECONDS = 86400
# Slicer projects only exist once supports are placed.
SUPPORTED_PROJECT_EXTENSIONS = (".lys", ".ctb", ".ags", ".chitubox", ".prusa")
_UNSUPPORTED_WORDS = re.compile(r"(?:un|non?|not)[_\- ]?sup+ort", re.IGNORECASE)
# Doubled p allowed because Presuported ships in real packs. Lychee and ready-to-slice exports are always supported.
_SUPPORTED_WORDS = re.compile(r"sup+orted|with[_\- ]supports|support stls|lychee|readytoslice", re.IGNORECASE)


def sanitize_folder_name(name: str) -> str:
    """Keeps a name readable for an export folder, removing only what a filesystem refuses."""
    if not name:
        return "Unknown"
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    clean = re.sub(r"\s+", " ", clean).rstrip(". ")
    return clean or "Unknown"


def sanitize_title(name: str) -> str:
    """Turns a file name into a display title."""
    if not name:
        return "Untitled Model"
    clean = re.sub(r"^[0-9]+[_\-\s]+", "", name)
    clean = clean.replace("_", " ").strip()
    return clean or name


def sanitize_segment(name: str) -> str:
    """Replaces spaces and special characters with single underscores. Vault paths are built from this."""
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(name))
    clean = re.sub(r"_+", "_", clean)
    return clean.strip("_") or "unknown"


def sweep_leftovers(parent: Path, pattern: str) -> None:
    """Removes the work folders a crash left, which no cleanup path or audit would ever reach."""
    if not parent.is_dir():
        return
    cutoff = time.time() - LEFTOVER_AGE_SECONDS
    for folder in parent.glob(pattern):
        if folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


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
