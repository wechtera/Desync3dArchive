"""Path name rules shared across the app."""

import re

# Fixed on purpose: ad-hoc imports always sit here under the vault root, with no setting to move them.
IMPORT_FOLDER = "ad-hoc"
# Hidden on purpose, and fixed for the same reason.
PENDING_DELETION_FOLDER = ".Pending-Deletion"


def sanitize_segment(name: str) -> str:
    """Replaces spaces and special characters with single underscores."""
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(name))
    clean = re.sub(r"_+", "_", clean)
    return clean.strip("_") or "unknown"
