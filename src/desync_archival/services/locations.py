"""Every place the app keeps something on this machine, and why, for Settings > Directories.

Paths only, never file contents. A missing path is reported and not treated as a fault, because the
NAS can be offline and some folders only appear on first use.
"""

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import locations as queries
from desync_archival.services.paths import IMPORT_FOLDER, PENDING_DELETION_FOLDER

CONFIG = "Configuration"
DATA = "App data"
STORAGE = "Files and libraries"
BACKUPS = "Backups"
TEMPORARY = "Temporary"
IN_SETTINGS = "Settings > Directories"
IN_BACKUPS = "Settings > Backups"
FIXED = "Fixed"


def _entry(group: str, name: str, path: Path | str | None, purpose: str, change: str) -> dict[str, Any]:
    if path in (None, ""):
        return {"group": group, "name": name, "path": "", "purpose": purpose, "change": change, "state": "not set"}
    resolved = Path(path).expanduser()
    try:
        found = resolved.exists()
    except OSError:
        found = False
    return {"group": group, "name": name, "path": str(resolved), "purpose": purpose, "change": change,
            "state": "found" if found else "not found"}


def _configuration() -> list[dict[str, Any]]:
    tls = app_config.tls_files()
    return [
        _entry(CONFIG, "Settings file", app_config.settings_file(),
               "Server settings read at startup: data folder, allowed folders, LAN mode. See Help.",
               "Edit the file, then restart"),
        _entry(CONFIG, "Secrets file", app_config.secrets_file(),
               "API keys, cookies and the login password hash. Readable by your account only.", "DESYNC_SECRETS_FILE"),
        _entry(CONFIG, "TLS certificate", tls[0] if tls else None, "Lets other devices reach the app over HTTPS.", "DESYNC_TLS_CERT"),
        _entry(CONFIG, "TLS key", tls[1] if tls else None, "Private half of the certificate.", "DESYNC_TLS_KEY"),
        _entry(CONFIG, "App code", app_config.REPO_ROOT,
               "The running checkout. Web files apply on save, Python on restart.", FIXED),
        _entry(CONFIG, "External tools", app_config.REPO_ROOT / "tools",
               "Links to Blender, unrar and similar programs installed elsewhere.", "DESYNC_BLENDER_BIN, DESYNC_UNRAR_BIN"),
    ]


def _app_data(stored: dict[str, str]) -> list[dict[str, Any]]:
    data = app_config.data_dir()
    return [
        _entry(DATA, "Data folder", data, "Everything below lives here. Keep it off the NAS so the app works offline.", "DESYNC_DATA_DIR"),
        _entry(DATA, "Catalog database", app_config.db_path(), "The catalog, tags, reviews, stashes, tasks and audit history.", FIXED),
        _entry(DATA, "Metadata", app_config.resolve_data_path(stored.get("metadata_backup_path") or app_config.metadata_dir()),
               "Raw MMF metadata for each collection, kept so the catalog can be rebuilt.", FIXED),
        _entry(DATA, "Previews", app_config.previews_dir(), "Cover images and renders shown in the library.", FIXED),
        _entry(DATA, "Scrapes", app_config.scrapes_dir(), "Results of each MMF scrape.", FIXED),
        _entry(DATA, "PDF cache", data / "document_cache", "PDFs opened from inside an archive, so the next open skips the NAS.", FIXED),
        _entry(DATA, "Logs", app_config.logs_dir(), "The app log. Rotated, and old files can be purged in Settings.", FIXED),
        _entry(DATA, "Backup choices", app_config.backup_paths_file(), "Remembers the folders chosen in Settings > Backups.", IN_BACKUPS),
    ]


def _storage(conn: sqlite3.Connection, stored: dict[str, str]) -> list[dict[str, Any]]:
    vault = stored.get("download_root_path")
    entries = [
        _entry(STORAGE, "Vault", vault, "Permanent home of downloaded MMF collections, in creator folders.", IN_SETTINGS),
        _entry(STORAGE, "Ad-hoc imports", Path(vault) / IMPORT_FOLDER if vault else None,
               "Everything added with Import File. Always here, apart from the creator folders.", "Fixed, follows the vault"),
        _entry(STORAGE, "Pending deletion", Path(vault) / PENDING_DELETION_FOLDER if vault else None,
               "Hidden. Files deleted from duplicate results, held for 14 days before they can be purged.", "Fixed, follows the vault"),
        _entry(STORAGE, "Staging", app_config.resolve_data_path(stored["download_work_path"]) if stored.get("download_work_path") else app_config.staging_dir(),
               "Fast local holding area for downloads before they move to the vault.", IN_SETTINGS),
        _entry(STORAGE, "Print queue", stored.get("stash_download_path"), "Where stashes are exported for the slicer.", IN_SETTINGS),
    ]
    entries += [
        _entry(STORAGE, f"Linked library: {name}", path, "Indexed where it sits. The app never changes these files.", "Settings > Libraries")
        for name, path in conn.execute(queries.SELECT_ACTIVE_LIBRARIES)
    ]
    return entries


def _backups() -> list[dict[str, Any]]:
    try:
        main = app_config.backup_dir()
    except app_config.SettingsError:
        main = None
    return [
        _entry(BACKUPS, "Backups", main, "Monthly and manual backups, on a different disk from the data folder.", IN_BACKUPS),
        _entry(BACKUPS, "Backup mirror", app_config.backup_mirror_dir(), "A second copy, usually on the NAS. Secrets aren't copied here.", IN_BACKUPS),
    ]


def listing(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Lists every location with its purpose, reporting a missing path instead of failing."""
    stored = dict(conn.execute(queries.SELECT_PATH_SETTINGS).fetchall())
    return [
        *_configuration(),
        *_app_data(stored),
        *_storage(conn, stored),
        *_backups(),
        _entry(TEMPORARY, "System temp folder", tempfile.gettempdir(),
               "Short-lived work while rendering previews. Cleaned up after each use.", "Set by macOS"),
    ]
