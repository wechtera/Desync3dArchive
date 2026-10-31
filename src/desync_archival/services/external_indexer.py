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
from desync_archival.services import inactive_log

try:
    from desync_archival.services.logger import log_layer, logger
except ImportError:
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

SUPPORTED_FILE_EXTENSIONS = {".stl", ".lys", ".ctb", ".chitubox", ".zip", ".rar", ".7z"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

SCALE_SUPPORT_DIR_NAMES = {
    "32mm", "75mm", "28mm", "54mm", "scale", "supported", "unsupported",
    "no supports", "supports", "presupported", "pre-supported", "files", "stl"
}

CATEGORY_DIR_TOKENS = {
    "heroes", "monsters", "enemies", "props", "npc", "terrain", "creatures",
    "minis", "bundle", "characters", "bonus", "bosses", "allies"
}


# More of a library than this going missing at once points at the mount, not at real deletions.
MASS_MISSING_MIN = 10
MASS_MISSING_SHARE = 0.2

COVER = "cover"
PEEK = "peek"
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}
SCALE_TOKENS = ("32mm", "75mm", "28mm", "54mm")

PREVIEW = "preview"
ADD = "add"
FULL = "full"
SCAN_MODES = (PREVIEW, ADD, FULL)

PROPOSED = "proposed"
APPROVED = "approved"
SKIPPED = "skipped"
ADDED = "added"
GONE = "gone"
EMPTY = "empty"


class ScanReadError(RuntimeError):
    """Storage could not be read. The scan stops, and nothing is concluded from the failed read."""


def _read_failed(error: OSError) -> None:
    raise ScanReadError(f"{error.filename or 'storage'}: {error.strerror or error}") from error


def list_dirs(path: Path) -> list[Path]:
    """Visible subfolders. A read error raises, so a mount drop never looks like an empty folder."""
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
    """Every visible file under a folder, with its size and modified time."""
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


def has_model_files(path: Path, loose_only: bool = False) -> bool:
    """Stops at the first match, because listing a whole model over SMB is the slow part."""
    wanted = (".stl",) if loose_only else (".stl", ".zip")
    for _, _, filenames in os.walk(path, onerror=_read_failed):
        if any(name.lower().endswith(wanted) and not name.startswith(".") for name in filenames):
            return True
    return False


def release_month_of(release_name: str) -> str:
    match = re.search(r"(\d{4}[-_]\d{2})", release_name)
    return match.group(1).replace("_", "-") if match else "Unassigned"


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
    model_ids = [row[0] for row in conn.execute("SELECT id FROM models WHERE library_id = ?;", (library_id,)).fetchall()]

    with conn:
        conn.executemany("UPDATE models SET is_active = 0 WHERE id = ?;", [(mid,) for mid in model_ids])
        conn.execute("UPDATE external_libraries SET is_active = 0 WHERE id = ?;", (library_id,))
        names = conn.execute("SELECT id, name FROM models WHERE library_id = ?;", (library_id,)).fetchall()
        for model_id, name in names:
            inactive_log.record(
                conn, inactive_log.MODEL, model_id, name, inactive_log.LIBRARY_UNLINK, inactive_log.LIBRARY_UNLINKED,
            )
        conn.execute("""
            UPDATE releases SET is_active = 0
            WHERE release_key LIKE 'external\\_%' ESCAPE '\\' AND NOT EXISTS (
                SELECT 1 FROM models m
                WHERE m.source = 'external' AND m.is_active = 1
                  AND m.designer_name = releases.designer_name AND m.release_month = releases.release_month
            );
        """)
    return model_ids


class ExternalLibraryIndexer:
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

    def _find_model_directories(self, release_dir: Path) -> list[tuple[Path, str]]:
        subdirs = list_dirs(release_dir)

        if not subdirs:
            return [(release_dir, "Miniatures")]

        if any(d.name.lower() in SCALE_SUPPORT_DIR_NAMES for d in subdirs):
            return [(release_dir, "Miniatures")]

        model_dirs: list[tuple[Path, str]] = []
        for d in subdirs:
            d_name_low = d.name.lower()
            is_category = (
                any(token in d_name_low for token in CATEGORY_DIR_TOKENS)
                or bool(re.match(r"^\d{1,2}\s*[-_]", d_name_low))
            )

            if is_category:
                cat_clean = sanitize_title(d.name)
                cat_children = list_dirs(d)
                if cat_children:
                    model_dirs.extend((c, cat_clean) for c in cat_children if has_model_files(c))
                elif has_model_files(d, loose_only=True):
                    model_dirs.append((d, cat_clean))
            elif has_model_files(d):
                model_dirs.append((d, "Miniatures"))

        return model_dirs if model_dirs else [(release_dir, "Miniatures")]

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
        return conn.execute("SELECT * FROM external_libraries WHERE id = ? AND is_active = 1;", (library_id,)).fetchone()

    def _check_root(self, root: Path) -> list[Path]:
        """Proves the mount is really there. An empty root is treated as an unmounted one."""
        releases = list_dirs(root)
        if not releases:
            raise ScanReadError(f"{root} has no folders, so the mount may be missing")
        return releases

    def _adopt_keyless_models(self, conn: sqlite3.Connection, library_id: int, root: Path) -> None:
        """Gives rows from before keys existed their key, so they are matched and not added again."""
        rows = conn.execute(
            "SELECT id, vault_path FROM models WHERE source = 'external' AND library_id = ? AND external_key IS NULL;",
            (library_id,),
        ).fetchall()
        with conn:
            for row in rows:
                path = Path(row["vault_path"] or "")
                if path.is_relative_to(root):
                    key = key_for(library_id, str(path.relative_to(root)))
                    conn.execute("UPDATE models SET external_key = ? WHERE id = ?;", (key, row["id"]))

    def _open_run(self, conn: sqlite3.Connection, library_id: int, mode: str) -> int:
        """Resumes an unfinished run of this mode, so finished work isn't repeated."""
        last = conn.execute(
            "SELECT id, status FROM library_scan_runs WHERE library_id = ? AND mode = ? ORDER BY id DESC LIMIT 1;",
            (library_id, mode),
        ).fetchone()
        with conn:
            if last and last["status"] != "completed":
                conn.execute("UPDATE library_scan_runs SET status = 'running', finished_at = NULL WHERE id = ?;", (last["id"],))
                return last["id"]
            cursor = conn.execute("INSERT INTO library_scan_runs (library_id, mode) VALUES (?, ?);", (library_id, mode))
            return int(cursor.lastrowid or 0)

    def _close_run(self, conn: sqlite3.Connection, run_id: int, status: str, report: dict[str, Any]) -> None:
        with conn:
            conn.execute(
                "UPDATE library_scan_runs SET status = ?, report_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (status, json.dumps(report), run_id),
            )

    def _refresh_release(self, conn: sqlite3.Connection, default_creator: str, release_month: str) -> None:
        # Counted, not added, so a rescan can't inflate it.
        model_count = conn.execute("""
            SELECT COUNT(*) FROM models
            WHERE source = 'external' AND is_active = 1 AND designer_name = ? AND release_month = ?;
        """, (default_creator, release_month)).fetchone()[0]
        release_id = f"external_{default_creator}_{release_month}"
        updated = conn.execute(
            "UPDATE releases SET model_count = ?, is_active = 1 WHERE release_key = ?;", (model_count, release_id)
        )
        if not updated.rowcount:
            conn.execute("""
                INSERT INTO releases (
                    release_key, designer_name, designer_id, release_month,
                    model_count, total_files, total_size_bytes
                ) VALUES (?, ?, 0, ?, ?, 0, 0);
            """, (release_id, default_creator, release_month, model_count))

    def _refresh_library_totals(self, conn: sqlite3.Connection, library_id: int, full_scan: bool) -> None:
        """Counted from the catalog. Only a finished full rescan may claim the library was scanned."""
        models, files, size = conn.execute("""
            SELECT COUNT(DISTINCT m.id), COUNT(df.file_id), COALESCE(SUM(df.actual_bytes), 0)
            FROM models m LEFT JOIN downloaded_files df ON df.model_id = m.id
            WHERE m.library_id = ? AND m.is_active = 1;
        """, (library_id,)).fetchone()
        with conn:
            conn.execute(
                "UPDATE external_libraries SET total_models = ?, total_files = ?, disk_bytes = ? WHERE id = ?;",
                (models, files, size, library_id),
            )
            if full_scan:
                conn.execute("UPDATE external_libraries SET last_scanned_at = CURRENT_TIMESTAMP WHERE id = ?;", (library_id,))

    def _sync_model(
        self,
        conn: sqlite3.Connection,
        library: sqlite3.Row,
        root: Path,
        model: sqlite3.Row | None,
        rel_path: str,
        category: str,
        run_id: int,
    ) -> tuple[int, bool]:
        """Brings one model in line with its folder and writes only what changed."""
        library_id = library["id"]
        default_creator = library["default_creator"] or library["name"]
        model_dir = root / rel_path
        release_name = Path(rel_path).parts[0]
        release_month = release_month_of(release_name)

        # Read everything first. A failed read raises here, before any row changes.
        found = [
            (path, size, modified)
            for path, size, modified in walk_files(model_dir)
            if path.suffix.lower() in SUPPORTED_FILE_EXTENSIONS
        ]

        lowered = [str(path.relative_to(root)).lower() for path, _, _ in found]
        has_presupported = int(any("supported" in low for low in lowered))
        scales = {scale for scale in SCALE_TOKENS for low in lowered if scale in low}

        with conn:
            if model is None:
                cursor = conn.execute("""
                    INSERT INTO models (
                        library_id, name, designer_name, designer_username, primary_category, pre_supported,
                        release_month, vault_path, source, external_key, raw_backup_path, updated_at, last_scan_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'external', ?, '', CURRENT_TIMESTAMP, ?)
                """, (
                    library_id, sanitize_title(model_dir.name), default_creator, default_creator, category,
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
                    conn.execute("""
                        UPDATE models SET vault_path = ?, pre_supported = ?, is_active = 1, library_id = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?;
                    """, (str(model_dir), has_presupported, library_id, model_id))
                    inactive_log.resolve(conn, inactive_log.MODEL, model_id, inactive_log.FOLDER_MISSING)
                    inactive_log.resolve(conn, inactive_log.MODEL, model_id, inactive_log.LIBRARY_UNLINKED)
                conn.execute("UPDATE models SET last_scan_run_id = ? WHERE id = ?;", (run_id, model_id))

            files_changed = self._sync_files(conn, library_id, root, model_id, model_dir.name, found)

            if changed or files_changed:
                tags = {default_creator.lower(), sanitize_title(release_name).lower(), category.lower(), "external"}
                tags.add("pre-supported" if has_presupported else "unsupported")
                tags.update(scales)
                # Add only. A repeat is ignored by a trigger, and tags are never removed.
                conn.executemany(
                    "INSERT INTO model_tags (model_id, tag) VALUES (?, ?);",
                    [(model_id, tag.strip()) for tag in tags if tag.strip()],
                )
                self._record_job(conn, model_id, str(model_dir), len(found), sum(size for _, size, _ in found))

            cover = self.previews_root / str(model_id) / "cover.jpg"
            if not image_url or not cover.exists() or cover.stat().st_size == 0:
                conn.execute("INSERT INTO library_work_queue (kind, model_id) VALUES (?, ?);", (COVER, model_id))

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
        for row in conn.execute("""
            SELECT mf.file_id, mf.filename, mf.external_key, df.vault_path, df.actual_bytes, df.modified_ns
            FROM model_files mf LEFT JOIN downloaded_files df ON df.file_id = mf.file_id
            WHERE mf.model_id = ?;
        """, (model_id,)).fetchall():
            key = row["external_key"]
            if not key and row["vault_path"]:
                # A row from before keys existed is adopted, not added a second time.
                key = key_for(library_id, str(Path(row["vault_path"]).relative_to(root)))
                conn.execute("UPDATE model_files SET external_key = ? WHERE file_id = ?;", (key, row["file_id"]))
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
                cursor = conn.execute("""
                    INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
                    VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, 'external', ?);
                """, (model_id, path.name, size, key))
                file_id = int(cursor.lastrowid or 0)
                conn.execute("""
                    INSERT INTO downloaded_files (
                        file_id, model_id, original_filename, cleaned_filename, local_relative_path, expected_bytes,
                        actual_bytes, file_extension, storage_tier, vault_path, modified_ns, downloaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP);
                """, (file_id, model_id, path.name, path.name, f"files/{path.name}", size, size,
                      path.suffix.lower(), str(path), modified))
                changed = True
                if is_archive:
                    conn.execute(
                        "INSERT INTO library_work_queue (kind, model_id, file_id) VALUES (?, ?, ?);",
                        (PEEK, model_id, file_id),
                    )
                continue

            file_id = row["file_id"]
            content_changed = row["actual_bytes"] != size or (row["modified_ns"] not in (None, modified))
            if content_changed or row["vault_path"] != str(path) or row["modified_ns"] is None:
                conn.execute("UPDATE model_files SET size_bytes = ? WHERE file_id = ?;", (size, file_id))
                conn.execute(
                    "UPDATE downloaded_files SET actual_bytes = ?, expected_bytes = ?, vault_path = ?, modified_ns = ? WHERE file_id = ?;",
                    (size, size, str(path), modified, file_id),
                )
            if content_changed:
                changed = True
                if is_archive:
                    conn.execute(
                        "UPDATE library_work_queue SET status = 'pending', attempts = 0 WHERE kind = ? AND file_id = ?;",
                        (PEEK, file_id),
                    )
            inactive_log.resolve(conn, inactive_log.FILE, file_id, inactive_log.FILE_MISSING)

        for key, row in known.items():
            if key not in seen:
                inactive_log.record(
                    conn, inactive_log.FILE, row["file_id"], row["filename"], inactive_log.LIBRARY_SCAN,
                    inactive_log.FILE_MISSING, f"Not found in {model_name}: {row['vault_path']}",
                )
                changed = True
        return changed

    def _record_job(self, conn: sqlite3.Connection, model_id: int, path: str, files: int, size: int) -> None:
        updated = conn.execute("""
            UPDATE download_jobs
            SET status = 'completed', total_files = ?, downloaded_files = ?, total_bytes = ?, downloaded_bytes = ?,
                destination_path = ?, completed_at = CURRENT_TIMESTAMP
            WHERE model_id = ?;
        """, (files, files, size, size, path, model_id))
        if not updated.rowcount:
            conn.execute("""
                INSERT INTO download_jobs (
                    model_id, status, total_files, downloaded_files, total_bytes, downloaded_bytes,
                    destination_path, enqueued_at, completed_at
                ) VALUES (?, 'completed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """, (model_id, files, files, size, size, path))

    # -------------------------------------------------------------------------
    # Preview: reads the library and writes no catalog rows
    # -------------------------------------------------------------------------

    def classify_library(
        self,
        root: Path,
        library_id: int,
        known_keys: set,
        progress: Callable[[float, str], None] = lambda _pct, _lbl: None,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        """Sorts each release's folders. Known folders stay as they are and are never reclassified."""
        known_rel = {key.split("/", 1)[1] for key in known_keys}
        release_dirs = self._check_root(root)
        releases: list[dict[str, Any]] = []
        complete = True

        for index, release_dir in enumerate(release_dirs, 1):
            if is_cancelled():
                complete = False
                break
            progress(index / len(release_dirs) * 95.0, f"[{index}/{len(release_dirs)}] {release_dir.name[:40]}")

            entry: dict[str, Any] = {
                "name": release_dir.name, "month": release_month_of(release_dir.name),
                "new": [], "known": 0, "overlaps": [], "empty": [], "outside_rules": False,
            }
            model_dirs = self._find_model_directories(release_dir)
            prefix = f"{release_dir.name}/"
            known_here = {rel for rel in known_rel if rel == release_dir.name or rel.startswith(prefix)}

            for model_dir, category in model_dirs:
                rel_path = str(model_dir.relative_to(root))
                if rel_path in known_rel:
                    entry["known"] += 1
                elif any(rel_path.startswith(f"{other}/") or other.startswith(f"{rel_path}/") for other in known_here):
                    entry["overlaps"].append(rel_path)
                elif not has_model_files(model_dir):
                    entry["empty"].append(rel_path)
                else:
                    entry["new"].append({"rel_path": rel_path, "category": category})
                if model_dir == release_dir:
                    entry["outside_rules"] = True
            releases.append(entry)

        return {
            "releases": releases,
            "complete": complete,
            "totals": {
                "releases": len(releases),
                "new": sum(len(r["new"]) for r in releases),
                "known": sum(r["known"] for r in releases),
                "overlaps": sum(len(r["overlaps"]) for r in releases),
                "empty": sum(len(r["empty"]) for r in releases),
                "outside_rules": sum(1 for r in releases if r["outside_rules"]),
            },
        }

    def preview_library(
        self,
        library_id: int,
        progress_cb: Callable[[float, str], None] | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        log = log_cb or (lambda _: None)
        with self._get_db() as conn:
            library = self._library(conn, library_id)
            if not library:
                return {"success": False, "error": f"External library #{library_id} not found."}
            root = Path(library["path"])
            self._adopt_keyless_models(conn, library_id, root)
            known = {
                r[0] for r in conn.execute(
                    "SELECT external_key FROM models WHERE library_id = ? AND external_key IS NOT NULL;", (library_id,)
                ).fetchall()
            }
            run_id = self._open_run(conn, library_id, PREVIEW)
            log(f"Previewing '{library['name']}' at {root}. Nothing is added until the preview is approved.")
            try:
                report = self.classify_library(
                    root, library_id, known, progress_cb or (lambda _pct, _lbl: None), cancel_cb or (lambda: False),
                )
            except ScanReadError as e:
                self._close_run(conn, run_id, "failed", {"error": str(e)})
                return {"success": False, "error": f"Storage could not be read: {e}"}

            if not report["complete"]:
                self._close_run(conn, run_id, "stopped", report)
                return {"success": True, "stopped": True, "run_id": run_id, **report["totals"]}

            with conn:
                conn.execute("DELETE FROM library_candidates WHERE library_id = ? AND status = ?;", (library_id, PROPOSED))
                decided = {
                    r[0] for r in conn.execute(
                        "SELECT rel_path FROM library_candidates WHERE library_id = ?;", (library_id,)
                    ).fetchall()
                }
                conn.executemany(
                    "INSERT INTO library_candidates (library_id, run_id, rel_path, release_name, category, status)"
                    " VALUES (?, ?, ?, ?, ?, ?);",
                    [
                        (library_id, run_id, item["rel_path"], release["name"], item["category"], PROPOSED)
                        for release in report["releases"] for item in release["new"]
                        if item["rel_path"] not in decided
                    ],
                )
            self._close_run(conn, run_id, "completed", report)

        totals = report["totals"]
        log(f"Preview finished: {totals['new']} new, {totals['known']} known, {totals['overlaps']} overlapping, "
            f"{totals['empty']} empty.")
        return {"success": True, "run_id": run_id, **totals}

    def approve_candidates(self, conn: sqlite3.Connection, library_id: int, skip_releases: list[str]) -> dict[str, int]:
        """Approves what the preview proposed. Skipped releases stay out until a later approval."""
        marks = ",".join("?" * len(skip_releases))
        with conn:
            skipped = conn.execute(
                f"UPDATE library_candidates SET status = ? WHERE library_id = ? AND status = ? AND release_name IN ({marks});",
                (SKIPPED, library_id, PROPOSED, *skip_releases),
            ).rowcount if skip_releases else 0
            approved = conn.execute(
                "UPDATE library_candidates SET status = ? WHERE library_id = ? AND status = ?;",
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
        """Adds approved folders one at a time, each committed alone, so stopping loses nothing."""
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
                "SELECT id, rel_path, category FROM library_candidates WHERE library_id = ? AND status = ? ORDER BY rel_path;",
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

                    if not (root / rel_path).is_dir():
                        self._check_root(root)
                        with conn:
                            conn.execute("UPDATE library_candidates SET status = ? WHERE id = ?;", (GONE, candidate["id"]))
                        continue

                    # A folder can become known after its preview, and it must not be added twice.
                    key = key_for(library_id, rel_path)
                    existing = conn.execute(
                        "SELECT * FROM models WHERE source = 'external' AND (external_key = ? OR vault_path = ?);",
                        (key, str(root / rel_path)),
                    ).fetchone()
                    if existing and existing["external_key"] != key:
                        with conn:
                            conn.execute("UPDATE models SET external_key = ? WHERE id = ?;", (key, existing["id"]))
                    count, _ = self._sync_model(conn, library, root, existing, rel_path, candidate["category"], run_id)
                    with conn:
                        conn.execute("UPDATE library_candidates SET status = ? WHERE id = ?;", (ADDED, candidate["id"]))
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
        """Checks every known model against storage, skipping those this run already finished.

        It never creates a model: new folders come in through a preview. A missing folder on a
        healthy mount is flagged inactive. Too many at once is held for review, not applied.
        """
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
            models = conn.execute("""
                SELECT * FROM models
                WHERE library_id = ? AND source = 'external' AND external_key IS NOT NULL
                  AND COALESCE(last_scan_run_id, 0) != ?
                ORDER BY external_key;
            """, (library_id, run_id)).fetchall()
            total_known = conn.execute("SELECT COUNT(*) FROM models WHERE library_id = ?;", (library_id,)).fetchone()[0]

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

                    if not (root / rel_path).is_dir():
                        self._check_root(root)
                        missing.append(model)
                        continue

                    count, was_changed = self._sync_model(
                        conn, library, root, model, rel_path, model["primary_category"] or "Miniatures", run_id,
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
            limit = max(MASS_MISSING_MIN, int(total_known * MASS_MISSING_SHARE))
            if len(newly_missing) > limit:
                summary["missing_held"] = len(newly_missing)
                log(f"Held: {len(newly_missing)} folders look missing, which points at the mount. Nothing was flagged.")
            else:
                with conn:
                    for model in newly_missing:
                        conn.execute("UPDATE models SET is_active = 0 WHERE id = ?;", (model["id"],))
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
        """Preview, approve everything, add and rescan in one call, for scripts and tests."""
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
            lib_row = conn.execute("SELECT id, name, path FROM external_libraries WHERE id = ?;", (library_id,)).fetchone()
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

