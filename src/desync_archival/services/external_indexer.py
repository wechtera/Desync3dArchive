"""
services/external_indexer.py - In-Place External Library Indexer & Zero-Copy Reconciler.

STRICT INVARIANT: Read-only on source storage. Never deletes, moves, overwrites, or modifies
any file or directory on external storage. All persistent previews are written to the local
previews/ cache, and all database records are registered in library.db.
"""

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from desync_archival import settings as app_config
from desync_archival.db.queries import external_indexer as queries
from desync_archival.services import (
    catalog_levels,
    inactive_log,
    library_review,
    model_resolver,
)
from desync_archival.services.library_review import (
    ADDED,
    APPROVED,
    GONE,
    PROPOSED,
    SKIPPED,
)
from desync_archival.services.model_resolver import primary_folder_rank

try:
    from desync_archival.services.logger import log_layer, logger
except ImportError:
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

SUPPORTED_FILE_EXTENSIONS = {".stl", ".lys", ".ctb", ".chitubox", ".zip", ".rar", ".7z"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


# Nobody deletes a fifth of a library by hand. That many missing means a flaky mount, so hold it.
MASS_MISSING_MIN = 10
MASS_MISSING_SHARE = 0.2

MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
COVER = "cover"
PEEK = "peek"
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
SCALE_TOKENS = ("32mm", "75mm", "28mm", "54mm")

PREVIEW = "preview"
ADD = "add"
FULL = "full"
SCAN_MODES = (PREVIEW, ADD, FULL)

EMPTY = "empty"


class ScanReadError(RuntimeError):
    """A failed read proves nothing about the files, so the scan stops instead of guessing."""


def _read_failed(error: OSError) -> None:
    raise ScanReadError(f"{error.filename or 'storage'}: {error.strerror or error}") from error


def list_dirs(path: Path) -> list[Path]:
    """Lists visible folders, and raises on a failed read instead of reporting none."""
    # Not Path.iterdir in a try/except: a dropped mount must never read as an empty folder.
    try:
        with os.scandir(path) as entries:
            return sorted(
                (Path(e.path) for e in entries if e.is_dir() and not e.name.startswith(".")),
                key=lambda p: p.name,
            )
    except OSError as e:
        _read_failed(e)
        raise


def walk_files(root: Path) -> Iterator[tuple[Path, int, int]]:
    """Yields files with size and mtime, and raises on a failed read."""
    # A review card can own a single file.
    if root.is_file():
        info = root.stat()
        yield root, info.st_size, info.st_mtime_ns
        return
    # NOTE: rglob swallows OSError, which is how the old scan retagged models during a drop.
    for current, dirnames, filenames in os.walk(root, onerror=_read_failed):
        dirnames.sort()
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(current) / name
            try:
                info = path.stat()
            except OSError as e:
                _read_failed(e)
                raise
            yield path, info.st_size, info.st_mtime_ns


def release_month_of(release_name: str) -> str:
    match = re.search(r"(\d{4}[-_]\d{2})", release_name)
    if match:
        return match.group(1).replace("_", "-")
    # From 2022 on, releases are named "April 2022".
    named = re.match(r"([A-Za-z]+)\s+(\d{4})\b", release_name.strip())
    if named and named.group(1).lower() in MONTHS:
        return f"{named.group(2)}-{MONTHS.index(named.group(1).lower()) + 1:02d}"
    return "Unassigned"


def is_presupported(path_text: str) -> bool:
    # Lookbehind because "unsupported" contains "supported". Lychee and ready-to-slice ship supported.
    # Lookbehind skips `unsupported`. Lychee and ready-to-slice exports always ship supported.
    return bool(re.search(r"(?<!un)supported|lychee|readytoslice", path_text, re.IGNORECASE))


# Library ID is in the key because two libraries can have the same folder name.
def key_for(library_id: int, rel_path: str) -> str:
    return f"{library_id}/{rel_path}"


def sanitize_title(name: str) -> str:
    if not name:
        return "Untitled Model"
    clean = re.sub(r"^[0-9]+[_\-\s]+", "", name)
    clean = clean.replace("_", " ").strip()
    return clean or name


def deactivate_library(conn: sqlite3.Connection, library_id: int) -> list[int]:
    """Flags a library and its contents inactive and deletes nothing. Relinking reactivates them."""
    model_ids = [row[0] for row in conn.execute(queries.SELECT_COLLECTIONS_ID_BY_LIBRARY_ID, (library_id,)).fetchall()]

    with conn:
        conn.executemany(queries.UPDATE_COLLECTIONS_IS_ACTIVE_BY_ID, [(mid,) for mid in model_ids])
        conn.execute(queries.UPDATE_EXTERNAL_LIBRARIES_IS_ACTIVE_BY_ID, (library_id,))
        names = conn.execute(queries.SELECT_COLLECTIONS_BY_LIBRARY_ID, (library_id,)).fetchall()
        for model_id, name in names:
            inactive_log.record(
                conn, inactive_log.MODEL, model_id, name, inactive_log.LIBRARY_UNLINK, inactive_log.LIBRARY_UNLINKED,
            )
        conn.execute(queries.UPDATE_RELEASES_IS_ACTIVE_BY_RELEASE_KEY)
    return model_ids


class ExternalLibraryIndexer:
    """Indexes a linked library in place. Reads source storage only."""
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.previews_root = app_config.previews_dir().resolve()
        self.previews_root.mkdir(parents=True, exist_ok=True)

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 45000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _read_release(self, release_dir: Path, root: Path) -> tuple[list[str], list[str], list[str]]:
        """Folders to learn from, the topmost ones holding model files, and every model file."""
        folders: list[str] = []
        holding: list[str] = []
        files: list[str] = []
        sealed = ""
        for current, dirnames, filenames in os.walk(release_dir, onerror=_read_failed):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            folder = Path(current)
            rel_path = str(folder.relative_to(root))
            held = sorted(
                name for name in filenames
                if Path(name).suffix.lower() in SUPPORTED_FILE_EXTENSIONS and not name.startswith(".")
            )
            files.extend(f"{rel_path}/{name}" for name in held)
            # Below a model the walk only collects files for the review screen. Learning from these
            # would change every guess.
            if sealed and rel_path.startswith(f"{sealed}/"):
                continue
            folders.append(rel_path)
            # Stat blocks and magazines sit at the top of a release, so that level never counts.
            if folder != release_dir and held:
                holding.append(rel_path)
                # A lone zip doesn't make a folder a model: Busts/ holds Bust_Bases.zip beside the busts.
                if any(Path(name).suffix.lower() not in ARCHIVE_EXTENSIONS for name in held):
                    sealed = rel_path
        return folders, holding, files

    @staticmethod
    def _within(folder: str, claimed: set[str]) -> bool:
        path = Path(folder)
        return any(str(parent) in claimed for parent in (path, *path.parents))

    def _score_image(self, img_path: Path, model_name: str) -> int:
        stem_low = img_path.stem.lower()
        score = 10
        if "concept" in stem_low: score += 50
        if "render" in stem_low: score += 45
        if "cover" in stem_low or "preview" in stem_low or "promo" in stem_low: score += 35
        if model_name.lower() in stem_low or stem_low in model_name.lower(): score += 40
        try:
            sz = img_path.stat().st_size
            if sz > 40 * 1024: score += 10
        except OSError:
            pass
        return score

    def _find_model_artwork(self, model_dir: Path, release_dir: Path) -> Path | None:
        candidate_images: list[tuple[int, Path]] = []
        try:
            for item in model_dir.rglob("*"):
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS and not item.name.startswith("."):
                    score = self._score_image(item, model_dir.name)
                    candidate_images.append((score, item))
        except (PermissionError, OSError):
            pass

        if candidate_images:
            candidate_images.sort(key=lambda x: x[0], reverse=True)
            return candidate_images[0][1]

        release_images: list[tuple[int, Path]] = []
        try:
            for item in release_dir.iterdir():
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS and not item.name.startswith("."):
                    stem_low = item.stem.lower()
                    score = 20
                    if "enemy" in stem_low or "enemies" in stem_low: score += 30
                    if "hero" in stem_low or "heroes" in stem_low: score += 30
                    if "splash" in stem_low or "banner" in stem_low: score += 35
                    if "cover" in stem_low or "all" in stem_low: score += 25
                    release_images.append((score, item))
        except (PermissionError, OSError):
            pass

        if release_images:
            release_images.sort(key=lambda x: x[0], reverse=True)
            return release_images[0][1]

        return None

    # -------------------------------------------------------------------------
    # Shared by the passes
    # -------------------------------------------------------------------------

    def _library(self, conn: sqlite3.Connection, library_id: int) -> sqlite3.Row | None:
        return conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_BY_ID, (library_id,)).fetchone()

    def _check_root(self, root: Path) -> list[Path]:
        # An unmounted share can list as an empty folder, and that must not look like deleted models.
        releases = list_dirs(root)
        if not releases:
            raise ScanReadError(f"{root} has no folders, so the mount may be missing")
        return releases

    def _known_folders(self, conn: sqlite3.Connection, library_id: int) -> dict[str, set[str]]:
        held: dict[str, set[str]] = {}
        for row in conn.execute(queries.SELECT_LIBRARY_MODEL_FOLDERS_BY_LIBRARY_ID, (library_id,)).fetchall():
            held.setdefault(row[0].split("/", 1)[1], set()).add(row[1])
        return held

    def _folders_of(self, conn: sqlite3.Connection, model_id: int, rel_path: str) -> list[str]:
        rows = conn.execute(queries.SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_MODEL_ID, (model_id,)).fetchall()
        return [r[0] for r in rows] or [rel_path]

    def _excludes_of(self, conn: sqlite3.Connection, model_id: int) -> list[str]:
        rows = conn.execute(queries.SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_MODEL_ID_EXCLUDED, (model_id,)).fetchall()
        return [r[0] for r in rows]

    def _adopt_keyless_models(self, conn: sqlite3.Connection, library_id: int, root: Path) -> None:
        # Without a key these would show up as new and hit the duplicate path trigger.
        rows = conn.execute(
            queries.SELECT_COLLECTIONS_BY_SOURCE,
            (library_id,),
        ).fetchall()
        with conn:
            for row in rows:
                path = Path(row["vault_path"] or "")
                if path.is_relative_to(root):
                    key = key_for(library_id, str(path.relative_to(root)))
                    conn.execute(queries.UPDATE_COLLECTIONS_EXTERNAL_KEY_BY_ID, (key, row["id"]))

    def _open_run(self, conn: sqlite3.Connection, library_id: int, mode: str) -> int:
        # Reusing the row is what makes resume work: models stamped with this run ID get skipped.
        last = conn.execute(
            queries.SELECT_LIBRARY_SCAN_RUNS_BY_LIBRARY_ID,
            (library_id, mode),
        ).fetchone()
        with conn:
            if last and last["status"] != "completed":
                conn.execute(queries.UPDATE_LIBRARY_SCAN_RUNS_STATUS_BY_ID, (last["id"],))
                return last["id"]
            cursor = conn.execute(queries.INSERT_LIBRARY_SCAN_RUNS, (library_id, mode))
            return int(cursor.lastrowid or 0)

    def _close_run(self, conn: sqlite3.Connection, run_id: int, status: str, report: dict[str, Any]) -> None:
        with conn:
            conn.execute(
                queries.UPDATE_LIBRARY_SCAN_RUNS_STATUS_BY_ID_V2,
                (status, json.dumps(report), run_id),
            )

    def _refresh_release(self, conn: sqlite3.Connection, default_creator: str, release_month: str) -> None:
        # Counted, not added, so a rescan can't inflate it.
        model_count = conn.execute(queries.COUNT_COLLECTIONS_BY_SOURCE, (default_creator, release_month)).fetchone()[0]
        release_id = f"external_{default_creator}_{release_month}"
        updated = conn.execute(
            queries.UPDATE_RELEASES_MODEL_COUNT_BY_RELEASE_KEY, (model_count, release_id)
        )
        if not updated.rowcount:
            conn.execute(queries.INSERT_RELEASES, (release_id, default_creator, release_month, model_count))

    def _refresh_library_totals(self, conn: sqlite3.Connection, library_id: int, full_scan: bool) -> None:
        # Stopped or partial runs used to stamp last_scanned_at and overwrite totals with partial counts.
        models, files, size = conn.execute(queries.COUNT_COLLECTIONS_BY_LIBRARY_ID, (library_id,)).fetchone()
        with conn:
            conn.execute(
                queries.UPDATE_EXTERNAL_LIBRARIES_TOTAL_MODELS_BY_ID,
                (models, files, size, library_id),
            )
            if full_scan:
                conn.execute(queries.UPDATE_EXTERNAL_LIBRARIES_LAST_SCANNED_AT_BY_ID, (library_id,))

    def _sync_model(
        self,
        conn: sqlite3.Connection,
        library: sqlite3.Row,
        root: Path,
        model: sqlite3.Row | None,
        rel_path: str,
        category: str,
        run_id: int,
        name: str,
        folders: list[str],
        excludes: list[str] | None = None,
    ) -> tuple[int, bool]:
        # Unchanged models write only the run stamp. Rewrites here hit the search triggers, so skip them.
        library_id = library["id"]
        default_creator = library["default_creator"] or library["name"]
        model_dir = root / folders[0]
        release_name = Path(rel_path).parts[0]
        release_month = release_month_of(release_name)

        # Read everything first. A failed read raises here, before any row changes.
        excludes = excludes or []
        carved = set(excludes)
        found = [
            (path, size, modified)
            for folder in folders if (root / folder).exists()
            for path, size, modified in walk_files(root / folder)
            if path.suffix.lower() in SUPPORTED_FILE_EXTENSIONS and not self._within(str(path.relative_to(root)), carved)
        ]

        lowered = [str(path.relative_to(root)).lower() for path, _, _ in found]
        has_presupported = int(any(is_presupported(low) for low in lowered))
        scales = {scale for scale in SCALE_TOKENS for low in lowered if scale in low}

        with conn:
            if model is None:
                cursor = conn.execute(queries.INSERT_COLLECTIONS, (
                    library_id, name, default_creator, default_creator, category,
                    has_presupported, release_month, str(model_dir), key_for(library_id, rel_path), run_id,
                ))
                model_id = int(cursor.lastrowid or 0)
                changed = True
                image_url = None
            else:
                model_id = model["id"]
                image_url = model["primary_image_url"]
                changed = (
                    model["vault_path"] != str(model_dir) or model["pre_supported"] != has_presupported
                    or model["is_active"] != 1 or model["library_id"] != library_id
                )
                if changed:
                    conn.execute(queries.UPDATE_COLLECTIONS_VAULT_PATH_BY_ID, (str(model_dir), has_presupported, library_id, model_id))
                    inactive_log.resolve(conn, inactive_log.MODEL, model_id, inactive_log.FOLDER_MISSING)
                    inactive_log.resolve(conn, inactive_log.MODEL, model_id, inactive_log.LIBRARY_UNLINKED)
                conn.execute(queries.UPDATE_COLLECTIONS_LAST_SCAN_RUN_ID_BY_ID, (run_id, model_id))

            # A character's key isn't a real folder, so it needs rows even with only one folder.
            if folders != [rel_path] or excludes:
                conn.executemany(
                    queries.INSERT_LIBRARY_MODEL_FOLDERS,
                    [(model_id, folder) for folder in folders],
                )
                conn.executemany(
                    queries.INSERT_LIBRARY_MODEL_FOLDERS_V2,
                    [(model_id, path) for path in excludes],
                )
            files_changed = self._sync_files(conn, library_id, root, model_id, name, found)
            catalog_levels.sync_loose_artifacts(conn, model_id, name, root, found)

            if changed or files_changed:
                tags = {default_creator.lower(), sanitize_title(release_name).lower(), category.lower(), "external"}
                tags.add("pre-supported" if has_presupported else "unsupported")
                tags.update(scales)
                if category == "Busts":
                    tags.add("bust")
                # Add only. Triggers ignore a repeat, and anything a person took off.
                conn.executemany(
                    queries.INSERT_COLLECTION_TAGS,
                    [(model_id, catalog_levels.clean_tag(tag)) for tag in tags if tag.strip()],
                )
                self._record_job(conn, model_id, str(model_dir), len(found), sum(size for _, size, _ in found))

            cover = self.previews_root / str(model_id) / "cover.jpg"
            if not image_url or not cover.exists() or cover.stat().st_size == 0:
                conn.execute(queries.INSERT_LIBRARY_WORK_QUEUE, (COVER, model_id))

        return len(found), changed or files_changed

    def _sync_files(
        self,
        conn: sqlite3.Connection,
        library_id: int,
        root: Path,
        model_id: int,
        model_name: str,
        found: list[tuple[Path, int, int]],
    ) -> bool:
        known = {}
        for row in conn.execute(queries.SELECT_MODEL_FILES_BY_MODEL_ID, (model_id,)).fetchall():
            key = row["external_key"]
            if not key and row["vault_path"]:
                # A row from before keys existed is adopted, not added a second time.
                key = key_for(library_id, str(Path(row["vault_path"]).relative_to(root)))
                conn.execute(queries.UPDATE_MODEL_FILES_EXTERNAL_KEY_BY_FILE_ID, (key, row["file_id"]))
            known[key] = row

        changed = False
        seen = set()
        for path, size, modified in found:
            rel_path = str(path.relative_to(root))
            key = key_for(library_id, rel_path)
            seen.add(key)
            row = known.get(key)
            is_archive = path.suffix.lower() in ARCHIVE_EXTENSIONS

            if row is None:
                cursor = conn.execute(queries.INSERT_MODEL_FILES, (model_id, path.name, size, key))
                file_id = int(cursor.lastrowid or 0)
                conn.execute(queries.INSERT_DOWNLOADED_FILES, (file_id, model_id, path.name, path.name, f"files/{path.name}", size, size,
                      path.suffix.lower(), str(path), modified))
                changed = True
                if is_archive:
                    conn.execute(
                        queries.INSERT_LIBRARY_WORK_QUEUE_V2,
                        (PEEK, model_id, file_id),
                    )
                continue

            file_id = row["file_id"]
            content_changed = row["actual_bytes"] != size or (row["modified_ns"] not in (None, modified))
            if content_changed or row["vault_path"] != str(path) or row["modified_ns"] is None:
                conn.execute(queries.UPDATE_MODEL_FILES_SIZE_BYTES_BY_FILE_ID, (size, file_id))
                conn.execute(
                    queries.UPDATE_DOWNLOADED_FILES_ACTUAL_BYTES_BY_FILE_ID,
                    (size, size, str(path), modified, file_id),
                )
            if content_changed:
                changed = True
                if is_archive:
                    conn.execute(
                        queries.UPDATE_LIBRARY_WORK_QUEUE_STATUS_BY_KIND,
                        (PEEK, file_id),
                    )
            inactive_log.resolve(conn, inactive_log.FILE, file_id, inactive_log.FILE_MISSING)

        for key, row in known.items():
            if key not in seen:
                inactive_log.record(
                    conn, inactive_log.FILE, row["file_id"], row["filename"], inactive_log.LIBRARY_SCAN,
                    inactive_log.FILE_MISSING, f"Not found in {model_name}: {row['vault_path']}",
                )
                # FIXME: stays true on every rescan until the file returns, so models_changed reads high.
                changed = True
        return changed

    def _record_job(self, conn: sqlite3.Connection, model_id: int, path: str, files: int, size: int) -> None:
        updated = conn.execute(queries.UPDATE_DOWNLOAD_JOBS_STATUS_BY_MODEL_ID, (files, files, size, size, path, model_id))
        if not updated.rowcount:
            conn.execute(queries.INSERT_DOWNLOAD_JOBS, (model_id, files, files, size, size, path))

    # -------------------------------------------------------------------------
    # Preview: reads the library and writes no catalog rows
    # -------------------------------------------------------------------------

    def classify_library(
        self,
        root: Path,
        library_id: int,
        known_keys: set,
        known_folders: dict[str, set[str]] | None = None,
        progress: Callable[[float, str], None] = lambda _pct, _lbl: None,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        """Reads the whole tree and proposes models, writing nothing."""
        # Known folders win over the classifier. One new "32mm" folder can flip a release to a single model.
        known_rel = {key.split("/", 1)[1] for key in known_keys}
        known_folders = known_folders or {}
        release_dirs = self._check_root(root)

        # Read the whole tree first: the resolver learns from all of it, so a preview can't be partial.
        all_folders: list[str] = []
        file_folders: dict[str, list[str]] = {}
        trees: dict[str, dict[str, list[str]]] = {}
        for index, release_dir in enumerate(release_dirs, 1):
            if is_cancelled():
                return {"releases": [], "complete": False, "totals": {}, "trees": {}}
            progress(index / len(release_dirs) * 95.0, f"[{index}/{len(release_dirs)}] Reading {release_dir.name[:40]}")
            all_folders_here, holding, files = self._read_release(release_dir, root)
            all_folders.extend(all_folders_here)
            file_folders[release_dir.name] = holding
            # Parents of files, because a sealed model's subfolders aren't in the learning list.
            shown = set(all_folders_here) | {str(p) for f in files for p in Path(f).parents}
            trees[release_dir.name] = {"folders": sorted(shown - {release_dir.name, "."}), "files": files}

        variants = model_resolver.learn_variant_words(all_folders)
        parts = model_resolver.learn_part_words([f for held in file_folders.values() for f in held], variants)
        claimed = known_rel | {folder for held in known_folders.values() for folder in held}

        releases: list[dict[str, Any]] = []
        for release_dir in release_dirs:
            prefix = f"{release_dir.name}/"
            entry: dict[str, Any] = {
                "name": release_dir.name, "month": release_month_of(release_dir.name),
                "new": [], "known": 0, "overlaps": [], "empty": [], "low_confidence": [],
            }
            holding = file_folders[release_dir.name]
            if not holding:
                entry["empty"].append(release_dir.name)

            models = model_resolver.resolve([f[len(prefix):] for f in holding], variants=variants, parts=parts)
            inside = {
                f[len(prefix):]: sorted(
                    p[len(prefix):] for p in trees[release_dir.name]["folders"]
                    if p.startswith(f"{f}/") and p not in holding
                )
                for f in holding
            }
            model_resolver.flag_models(models, variants, parts, inside)
            model_resolver.group_models(models)
            carved = library_review.carve({m.key: [prefix + f for f in m.folders] for m in models}, claimed)
            for model in models:
                folders = sorted((prefix + f for f in model.folders), key=primary_folder_rank)
                rel_path = prefix + model.key
                # Known folders win, so a model from an earlier scan keeps its identity.
                inside_known = [f for f in folders if self._within(f, claimed)]
                if rel_path in known_folders:
                    if set(folders) - known_folders[rel_path]:
                        entry["new"].append({
                            "rel_path": rel_path, "name": model.name, "category": model.category or "Miniatures",
                            "folders": folders, "extends": True, "flags": model.flags, "exclude": carved[model.key],
                            "group": model.group, "tags": model.tags,
                        })
                    else:
                        entry["known"] += 1
                elif inside_known:
                    entry["known"] += 1
                elif any(other.startswith(f"{f}/") for f in folders for other in claimed):
                    entry["overlaps"].append(rel_path)
                else:
                    entry["new"].append({
                        "rel_path": rel_path, "name": model.name, "category": model.category or "Miniatures",
                        "folders": folders, "flags": model.flags, "exclude": carved[model.key],
                        "group": model.group, "tags": model.tags,
                    })
                    if model.confidence == "low":
                        entry["low_confidence"].append(f"{rel_path}: {model.reason}")
            releases.append(entry)

        complete = True
        return {
            "releases": releases,
            "complete": complete,
            "trees": trees,
            "totals": {
                "releases": len(releases),
                "new": sum(len(r["new"]) for r in releases),
                "known": sum(r["known"] for r in releases),
                "overlaps": sum(len(r["overlaps"]) for r in releases),
                "empty": sum(len(r["empty"]) for r in releases),
                "low_confidence": sum(len(r["low_confidence"]) for r in releases),
            },
        }

    def preview_library(
        self,
        library_id: int,
        progress_cb: Callable[[float, str], None] | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Stores a new preview and its candidates, keeping human reviews."""
        log = log_cb or (lambda _: None)
        with self._get_db() as conn:
            library = self._library(conn, library_id)
            if not library:
                return {"success": False, "error": f"External library #{library_id} not found."}
            root = Path(library["path"])
            self._adopt_keyless_models(conn, library_id, root)
            known = {
                r[0] for r in conn.execute(
                    queries.SELECT_COLLECTIONS_EXTERNAL_KEY_BY_LIBRARY_ID, (library_id,)
                ).fetchall()
            }
            run_id = self._open_run(conn, library_id, PREVIEW)
            log(f"Previewing '{library['name']}' at {root}. Nothing is added until the preview is approved.")
            try:
                report = self.classify_library(
                    root, library_id, known, self._known_folders(conn, library_id), progress_cb or (lambda _pct, _lbl: None), cancel_cb or (lambda: False),
                )
            except ScanReadError as e:
                self._close_run(conn, run_id, "failed", {"error": str(e)})
                return {"success": False, "error": f"Storage could not be read: {e}"}

            trees = report.pop("trees")
            if not report["complete"]:
                self._close_run(conn, run_id, "stopped", report)
                return {"success": True, "stopped": True, "run_id": run_id, "new": 0}

            with conn:
                conn.execute(queries.DELETE_LIBRARY_CANDIDATES_BY_LIBRARY_ID, (library_id, PROPOSED))
                for name, tree in trees.items():
                    library_review.save_tree(conn, library_id, run_id, name, tree)
                library_review.flag_renames(conn, library_id, report, trees)
                reviewed = library_review.merge_preview(conn, library_id, run_id, report, trees)
                decided = {
                    r[0] for r in conn.execute(
                        queries.SELECT_LIBRARY_CANDIDATES_REL_PATH_BY_LIBRARY_ID, (library_id,)
                    ).fetchall()
                }
                for release in report["releases"]:
                    if release["name"] in reviewed:
                        continue
                    for item in release["new"]:
                        folders = json.dumps(item["folders"])
                        if item["rel_path"] in decided:
                            # Only a later scale reopens a settled folder. A skipped one stays skipped.
                            if item.get("extends"):
                                conn.execute(
                                    queries.UPDATE_LIBRARY_CANDIDATES_STATUS_BY_LIBRARY_ID,
                                    (PROPOSED, folders, library_id, item["rel_path"]),
                                )
                            continue
                        conn.execute(
                            queries.INSERT_LIBRARY_CANDIDATES,
                            (library_id, run_id, item["rel_path"], release["name"], item["name"], item["category"], folders,
                             json.dumps(item.get("exclude") or []), PROPOSED, item.get("group") or "",
                             json.dumps(item.get("tags") or [])),
                        )
            self._close_run(conn, run_id, "completed", report)

        totals = report["totals"]
        log(f"Preview finished: {totals['new']} new, {totals['known']} known, {totals['overlaps']} overlapping, "
            f"{totals['empty']} empty.")
        return {"success": True, "run_id": run_id, **totals}

    def approve_candidates(self, conn: sqlite3.Connection, library_id: int, skip_releases: list[str]) -> dict[str, int]:
        marks = ",".join("?" * len(skip_releases))
        with conn:
            skipped = conn.execute(
                f"UPDATE library_candidates SET status = ? WHERE library_id = ? AND status = ? AND release_name IN ({marks});",
                (SKIPPED, library_id, PROPOSED, *skip_releases),
            ).rowcount if skip_releases else 0
            approved = conn.execute(
                queries.UPDATE_LIBRARY_CANDIDATES_STATUS_BY_LIBRARY_ID_V2,
                (APPROVED, library_id, PROPOSED),
            ).rowcount
        return {"approved": approved, "skipped": skipped}

    # -------------------------------------------------------------------------
    # Inventory: priority 1. Writes rows only, and never renders or opens archives
    # -------------------------------------------------------------------------

    def add_approved(
        self,
        library_id: int,
        progress_cb: Callable[[float, str], None] | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Adds approved folders one commit at a time, so a stop loses only the current folder."""
        # Each folder commits alone, so a stop or crash loses at most the one in progress.
        progress = progress_cb or (lambda _pct, _lbl: None)
        log = log_cb or (lambda _: None)
        is_cancelled = cancel_cb or (lambda: False)

        with self._get_db() as conn:
            library = self._library(conn, library_id)
            if not library:
                return {"success": False, "error": f"External library #{library_id} not found."}
            root = Path(library["path"])
            default_creator = library["default_creator"] or library["name"]
            run_id = self._open_run(conn, library_id, ADD)
            candidates = conn.execute(
                queries.SELECT_LIBRARY_CANDIDATES_BY_LIBRARY_ID,
                (library_id, APPROVED),
            ).fetchall()

            added = files = 0
            months = set()
            summary: dict[str, Any] = {"success": True, "library_id": library_id, "stopped": False}
            try:
                self._check_root(root)
                for index, candidate in enumerate(candidates, 1):
                    if is_cancelled():
                        summary["stopped"] = True
                        break
                    rel_path = candidate["rel_path"]
                    progress(index / max(1, len(candidates)) * 99.0, f"[{index}/{len(candidates)}] {rel_path[:60]}")

                    folders = json.loads(candidate["folders_json"])
                    if not any((root / folder).exists() for folder in folders):
                        self._check_root(root)
                        with conn:
                            conn.execute(queries.UPDATE_LIBRARY_CANDIDATES_STATUS_BY_ID, (GONE, candidate["id"]))
                        continue

                    # A folder can become known after its preview, and it must not be added twice.
                    key = key_for(library_id, rel_path)
                    existing = conn.execute(
                        queries.SELECT_COLLECTIONS_BY_SOURCE_VAULT_PATH,
                        (key, str(root / folders[0])),
                    ).fetchone()
                    if candidate["relink_id"]:
                        # A confirmed rename: same model, same ID, new home.
                        existing = conn.execute(queries.SELECT_COLLECTION_BY_ID, (candidate["relink_id"],)).fetchone()
                        with conn:
                            conn.execute(queries.DELETE_MODEL_FOLDERS, (candidate["relink_id"],))
                    if existing and existing["external_key"] != key:
                        with conn:
                            conn.execute(queries.UPDATE_COLLECTIONS_EXTERNAL_KEY_BY_ID, (key, existing["id"]))
                    count, _ = self._sync_model(
                        conn, library, root, existing, rel_path, candidate["category"], run_id, candidate["name"], folders,
                        json.loads(candidate["exclude_json"]),
                    )
                    model_id = conn.execute(queries.SELECT_COLLECTIONS_ID_BY_EXTERNAL_KEY, (key,)).fetchone()[0]
                    with conn:
                        conn.execute(queries.UPDATE_LIBRARY_CANDIDATES_STATUS_BY_ID, (ADDED, candidate["id"]))
                        catalog_levels.file_under_release(
                            conn, model_id, library_id, candidate["release_name"], candidate["group_name"],
                            json.loads(candidate["tags_json"]),
                        )
                    added += 1
                    files += count
                    months.add(release_month_of(Path(rel_path).parts[0]))
            except ScanReadError as e:
                self._finish_inventory(conn, library_id, default_creator, months, False)
                self._close_run(conn, run_id, "failed", {"error": str(e), "models_added": added})
                log(f"Stopped because storage could not be read: {e}")
                return {"success": False, "error": f"Storage could not be read: {e}", "models_added": added}

            self._finish_inventory(conn, library_id, default_creator, months, False)
            summary.update(models_added=added, files_added=files, remaining=len(candidates) - added)
            self._close_run(conn, run_id, "stopped" if summary["stopped"] else "completed", summary)

        progress(100.0, f"Added {added} models with {files} files.")
        log(f"Added {added} models with {files} files. Images follow in the background.")
        return summary

    def rescan_known(
        self,
        library_id: int,
        progress_cb: Callable[[float, str], None] | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Checks known models against storage, and holds the result if too many look missing."""
        # Never creates a model. New folders only come in through an approved preview.
        progress = progress_cb or (lambda _pct, _lbl: None)
        log = log_cb or (lambda _: None)
        is_cancelled = cancel_cb or (lambda: False)

        with self._get_db() as conn:
            library = self._library(conn, library_id)
            if not library:
                return {"success": False, "error": f"External library #{library_id} not found."}
            root = Path(library["path"])
            default_creator = library["default_creator"] or library["name"]
            self._adopt_keyless_models(conn, library_id, root)
            run_id = self._open_run(conn, library_id, FULL)
            models = conn.execute(queries.SELECT_COLLECTIONS_BY_LIBRARY_ID_SOURCE, (library_id, run_id)).fetchall()
            total_known = conn.execute(queries.COUNT_COLLECTIONS_BY_LIBRARY_ID_V2, (library_id,)).fetchone()[0]

            checked = changed = files = 0
            missing: list[sqlite3.Row] = []
            months = set()
            summary: dict[str, Any] = {"success": True, "library_id": library_id, "stopped": False}
            try:
                self._check_root(root)
                for index, model in enumerate(models, 1):
                    if is_cancelled():
                        summary["stopped"] = True
                        break
                    rel_path = model["external_key"].split("/", 1)[1]
                    progress(index / max(1, len(models)) * 99.0, f"[{index}/{len(models)}] {rel_path[:60]}")

                    folders = self._folders_of(conn, model["id"], rel_path)
                    if not any((root / folder).exists() for folder in folders):
                        self._check_root(root)
                        missing.append(model)
                        continue

                    count, was_changed = self._sync_model(
                        conn, library, root, model, rel_path, model["primary_category"] or "Miniatures", run_id,
                        model["name"], folders, self._excludes_of(conn, model["id"]),
                    )
                    checked += 1
                    files += count
                    changed += int(was_changed)
                    months.add(release_month_of(Path(rel_path).parts[0]))
            except ScanReadError as e:
                self._finish_inventory(conn, library_id, default_creator, months, False)
                self._close_run(conn, run_id, "failed", {"error": str(e), "models_checked": checked})
                log(f"Stopped because storage could not be read: {e}")
                return {"success": False, "error": f"Storage could not be read: {e}", "models_checked": checked}

            summary.update(models_checked=checked, models_changed=changed, files_checked=files)
            if summary["stopped"]:
                self._finish_inventory(conn, library_id, default_creator, months, False)
                self._close_run(conn, run_id, "stopped", summary)
                return summary

            newly_missing = [m for m in missing if m["is_active"] == 1]
            # Safety: past this, assume a flaky mount and flag nothing.
            limit = max(MASS_MISSING_MIN, int(total_known * MASS_MISSING_SHARE))
            if len(newly_missing) > limit:
                summary["missing_held"] = len(newly_missing)
                log(f"Held: {len(newly_missing)} folders look missing, which points at the mount. Nothing was flagged.")
            else:
                with conn:
                    for model in newly_missing:
                        conn.execute(queries.UPDATE_COLLECTIONS_IS_ACTIVE_BY_ID, (model["id"],))
                        inactive_log.record(
                            conn, inactive_log.MODEL, model["id"], model["name"], inactive_log.LIBRARY_SCAN,
                            inactive_log.FOLDER_MISSING, model["vault_path"],
                        )
                        months.add(model["release_month"])
                summary["models_missing"] = len(newly_missing)

            self._finish_inventory(conn, library_id, default_creator, months, "missing_held" not in summary)
            self._close_run(conn, run_id, "held" if "missing_held" in summary else "completed", summary)

        progress(100.0, f"Rescan finished: {checked} models checked, {changed} changed.")
        log(f"Rescan finished: {checked} models checked, {changed} changed, {files} files.")
        return summary

    def _finish_inventory(
        self, conn: sqlite3.Connection, library_id: int, default_creator: str, months: set, full_scan: bool,
    ) -> None:
        with conn:
            for month in months:
                self._refresh_release(conn, default_creator, month)
        self._refresh_library_totals(conn, library_id, full_scan)

    def scan_and_index_library(self, library_id: int, **callbacks: Any) -> dict[str, Any]:
        """Runs preview, approve, add and rescan with no human step. For scripts and tests only."""
        # Skips the human approval step, so keep this to scripts and tests.
        preview = self.preview_library(library_id, **callbacks)
        if not preview.get("success"):
            return preview
        with self._get_db() as conn:
            self.approve_candidates(conn, library_id, [])
        added = self.add_approved(library_id, **callbacks)
        if not added.get("success"):
            return added
        checked = self.rescan_known(library_id, **callbacks)
        return {**added, **checked, "models_indexed": checked.get("models_checked", 0), "files_indexed": checked.get("files_checked", 0)}

    def unlink_library(self, library_id: int) -> dict[str, Any]:
        """
        Unlinks an external library and cleans its associated SQLite metadata rows.
        INVARIANT: MUST NOT delete or touch any physical files on storage.
        """
        with self._get_db() as conn:
            lib_row = conn.execute(queries.SELECT_EXTERNAL_LIBRARIES_BY_ID_V2, (library_id,)).fetchone()
            if not lib_row:
                return {"success": False, "error": f"External library #{library_id} not found."}

            lib_name = lib_row["name"]
            lib_path = lib_row["path"]

            model_ids = deactivate_library(conn, library_id)

        log_layer("storage", logging.INFO, f"Unlinked library #{library_id} ('{lib_name}'): Flagged inactive {len(model_ids)} SQLite models. Source NAS files untouched.")
        return {
            "success": True,
            "library_id": library_id,
            "library_name": lib_name,
            "unlinked_models": len(model_ids),
            "message": f"Library '{lib_name}' unlinked. Flagged {len(model_ids)} catalog records inactive. Physical storage left untouched.",
        }

