"""
services/external_indexer.py - In-Place External Library Indexer & Zero-Copy Reconciler.

STRICT INVARIANT: Read-only on source storage. Never deletes, moves, overwrites, or modifies
any file or directory on external storage. All persistent previews are written to the local
previews/ cache, and all database records are registered in library.db.
"""

import logging
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from desync_archival import settings as app_config
from desync_archival.services.mesh_renderer import MeshRenderer

try:
    from desync_archival.services.logger import log_layer, logger
except ImportError:
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

try:
    from desync_archival.services.archive_peeker import ArchivePeeker
except ImportError:
    ArchivePeeker = None

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
        conn.execute("""
            UPDATE releases SET is_active = 0
            WHERE id LIKE 'external\\_%' ESCAPE '\\' AND NOT EXISTS (
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
        self.mesh_renderer = MeshRenderer()

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 45000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _find_model_directories(self, release_dir: Path) -> List[Tuple[Path, str]]:
        try:
            subdirs = [d for d in release_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        except (PermissionError, OSError):
            return []

        if not subdirs:
            return [(release_dir, "Miniatures")]

        if any(d.name.lower() in SCALE_SUPPORT_DIR_NAMES for d in subdirs):
            return [(release_dir, "Miniatures")]

        model_dirs: List[Tuple[Path, str]] = []
        for d in subdirs:
            d_name_low = d.name.lower()
            is_category = (
                any(token in d_name_low for token in CATEGORY_DIR_TOKENS)
                or bool(re.match(r"^\d{1,2}\s*[-_]", d_name_low))
            )

            if is_category:
                cat_clean = sanitize_title(d.name)
                try:
                    cat_children = [c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")]
                    if cat_children:
                        for c in cat_children:
                            if any(c.rglob("*.stl")) or any(c.rglob("*.zip")):
                                model_dirs.append((c, cat_clean))
                    elif any(d.rglob("*.stl")):
                        model_dirs.append((d, cat_clean))
                except (PermissionError, OSError):
                    continue
            else:
                try:
                    if any(d.rglob("*.stl")) or any(d.rglob("*.zip")):
                        model_dirs.append((d, "Miniatures"))
                except (PermissionError, OSError):
                    continue

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

    def _find_model_artwork(self, model_dir: Path, release_dir: Path) -> Optional[Path]:
        candidate_images: List[Tuple[int, Path]] = []
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

        release_images: List[Tuple[int, Path]] = []
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

    def scan_and_reconcile_library(
        self,
        library_id: int,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        log = log_cb or (lambda _: None)
        progress = progress_cb or (lambda _pct, _lbl: None)
        is_cancelled = cancel_cb or (lambda: False)

        with self._get_db() as conn:
            lib_row = conn.execute("SELECT * FROM external_libraries WHERE id = ? AND is_active = 1;", (library_id,)).fetchone()

        if not lib_row:
            return {"success": False, "error": f"External library #{library_id} not found."}

        library_name = lib_row["name"]
        library_root = Path(lib_row["path"]).resolve()
        default_creator = lib_row["default_creator"] or library_name

        if not library_root.exists() or not library_root.is_dir():
            return {"success": False, "error": f"Storage path not accessible: {library_root}"}

        log(f"Starting zero-copy, read-only sweep for '{library_name}' at {library_root}")
        progress(0.0, "Analyzing library folder structure...")

        release_dirs: List[Path] = []
        try:
            for entry in sorted(library_root.iterdir(), key=lambda e: e.name):
                if entry.is_dir() and not entry.name.startswith("."):
                    release_dirs.append(entry)
        except (PermissionError, OSError) as e:
            return {"success": False, "error": f"Failed reading library root: {e}"}

        total_releases = len(release_dirs)
        models_indexed = 0
        files_indexed = 0
        archives_peeked = 0
        blender_renders_run = 0
        total_scanned_bytes = 0

        peeker = ArchivePeeker(self.db_path) if ArchivePeeker else None

        for r_idx, rel_dir in enumerate(release_dirs, 1):
            if is_cancelled():
                log("Crawl cancelled by operator.")
                break

            release_name = rel_dir.name
            month_match = re.search(r"(\d{4}[-_]\d{2})", release_name)
            release_month = month_match.group(1).replace("_", "-") if month_match else "Unassigned"

            model_dirs = self._find_model_directories(rel_dir)

            for m_dir, m_category in model_dirs:
                if is_cancelled():
                    break

                models_indexed += 1
                model_name = sanitize_title(m_dir.name)
                pct = min(99.0, (r_idx / max(1, total_releases)) * 100.0)
                progress(pct, f"[{r_idx}/{total_releases}] {release_name[:20]} / {model_name[:25]}")

                discovered_files: List[Path] = []
                try:
                    for f in m_dir.rglob("*"):
                        if f.is_file() and f.suffix.lower() in SUPPORTED_FILE_EXTENSIONS and not f.name.startswith("."):
                            discovered_files.append(f)
                except (PermissionError, OSError):
                    pass

                has_presupported = 0
                scales_detected: Set[str] = set()
                for df in discovered_files:
                    df_low = str(df).lower()
                    if "supported" in df_low or "presupported" in df_low or "(supported)" in df_low:
                        has_presupported = 1
                    for sc in ("32mm", "75mm", "28mm", "54mm"):
                        if sc in df_low:
                            scales_detected.add(sc)

                # 1. Idempotent Model ID Resolution
                with self._get_db() as conn:
                    existing_model = conn.execute(
                        "SELECT id FROM models WHERE source = 'external' AND vault_path = ?;", (str(m_dir),)
                    ).fetchone()

                    if existing_model:
                        model_id = existing_model["id"]
                        with conn:
                            conn.execute("""
                                UPDATE models
                                SET name = ?, designer_name = ?, primary_category = ?,
                                    pre_supported = ?, release_month = ?, is_active = 1, library_id = ?,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?;
                            """, (model_name, default_creator, m_category, has_presupported, release_month, library_id, model_id))
                    else:
                        with conn:
                            cursor = conn.execute("""
                                INSERT INTO models (
                                    library_id, name, designer_name, designer_username, primary_category,
                                    pre_supported, release_month, vault_path, source, raw_backup_path, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'external', '', CURRENT_TIMESTAMP)
                            """, (library_id, model_name, default_creator, default_creator, m_category, has_presupported, release_month, str(m_dir)))
                        model_id = cursor.lastrowid

                # 2. Populate Model Tags
                tags_to_insert = {default_creator.lower(), sanitize_title(release_name).lower(), m_category.lower(), "external"}
                if has_presupported:
                    tags_to_insert.add("pre-supported")
                else:
                    tags_to_insert.add("unsupported")
                for sc in scales_detected:
                    tags_to_insert.add(sc)

                with self._get_db() as conn:
                    with conn:
                        conn.execute("DELETE FROM model_tags WHERE model_id = ?;", (model_id,))
                        conn.executemany(
                            "INSERT OR IGNORE INTO model_tags (model_id, tag) VALUES (?, ?);",
                            [(model_id, t.strip()) for t in tags_to_insert if t.strip()]
                        )

                # 3. Artwork Resolution Hierarchy
                preview_dir = self.previews_root / str(model_id)
                preview_dir.mkdir(parents=True, exist_ok=True)
                cover_dest = preview_dir / "cover.jpg"

                if not cover_dest.exists() or cover_dest.stat().st_size == 0:
                    artwork_path = self._find_model_artwork(m_dir, rel_dir)
                    if artwork_path:
                        try:
                            shutil.copy2(str(artwork_path), str(cover_dest))
                            log_layer("storage", logging.DEBUG, f"Model #{model_id}: Copied local art {artwork_path.name} -> cover.jpg")
                        except Exception as e:
                            log(f"[WARN] Failed copying artwork for Model #{model_id}: {e}")
                    elif self.mesh_renderer.is_available():
                        best_stl = self.mesh_renderer.select_best_render_candidate(m_dir)
                        if best_stl:
                            log(f"Rendering thumbnail with Blender for Model #{model_id} ({best_stl.name})...")
                            rendered = self.mesh_renderer.render_primary_thumbnail(best_stl, cover_dest)
                            if rendered:
                                blender_renders_run += 1
                                log_layer("storage", logging.INFO, f"Model #{model_id}: Procedurally rendered cover.jpg from {best_stl.name}")

                if cover_dest.exists() and cover_dest.stat().st_size > 0:
                    with self._get_db() as conn:
                        with conn:
                            conn.execute(
                                "UPDATE models SET primary_image_url = ? WHERE id = ?;",
                                (f"/previews/{model_id}/cover.jpg", model_id)
                            )

                # 4. Index Component Files
                model_file_bytes = 0

                for df in discovered_files:
                    files_indexed += 1
                    ext = df.suffix.lower()
                    try:
                        sz = df.stat().st_size
                    except OSError:
                        sz = 0

                    model_file_bytes += sz
                    total_scanned_bytes += sz

                    with self._get_db() as conn:
                        existing_file = conn.execute(
                            "SELECT file_id FROM downloaded_files WHERE vault_path = ?;", (str(df),)
                        ).fetchone()

                        if existing_file:
                            file_id = existing_file["file_id"]
                            with conn:
                                conn.execute(
                                    "UPDATE model_files SET size_bytes = ? WHERE file_id = ?;", (sz, file_id)
                                )
                                conn.execute("""
                                    UPDATE downloaded_files
                                    SET actual_bytes = ?, expected_bytes = ?
                                    WHERE file_id = ?;
                                """, (sz, sz, file_id))
                        else:
                            with conn:
                                cursor = conn.execute("""
                                    INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source)
                                    VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, 'external');
                                """, (model_id, df.name, sz))
                                file_id = cursor.lastrowid

                                conn.execute("""
                                    INSERT INTO downloaded_files (
                                        file_id, model_id, original_filename, cleaned_filename,
                                        local_relative_path, expected_bytes, actual_bytes,
                                        file_extension, storage_tier, vault_path, downloaded_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, CURRENT_TIMESTAMP);
                                """, (file_id, model_id, df.name, df.name, f"files/{df.name}", sz, sz, ext, str(df)))

                    # 5. Trigger ArchivePeeker on discovered archives
                    if ext in (".zip", ".rar", ".7z") and peeker:
                        with self._get_db() as conn:
                            has_submodels = conn.execute(
                                "SELECT 1 FROM file_archive_submodels WHERE file_id = ? LIMIT 1;", (file_id,)
                            ).fetchone()

                        if not has_submodels:
                            try:
                                peek_res = peeker.inspect_and_record(model_id, file_id, df)
                                if peek_res.get("success"):
                                    archives_peeked += 1
                            except Exception as pe:
                                log_layer("archive", logging.WARNING, f"Archive peek exception on {df.name}: {pe}")

                # 6. Record Model Completion in download_jobs
                with self._get_db() as conn:
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
                    if "download_jobs" in tables:
                        with conn:
                            existing_job = conn.execute("SELECT id FROM download_jobs WHERE model_id = ?;", (model_id,)).fetchone()
                            if existing_job:
                                conn.execute("""
                                    UPDATE download_jobs
                                    SET status = 'completed', total_files = ?, downloaded_files = ?,
                                        total_bytes = ?, downloaded_bytes = ?, destination_path = ?,
                                        completed_at = CURRENT_TIMESTAMP
                                    WHERE model_id = ?;
                                """, (len(discovered_files), len(discovered_files), model_file_bytes, model_file_bytes, str(m_dir), model_id))
                            else:
                                conn.execute("""
                                    INSERT INTO download_jobs (
                                        model_id, status, total_files, downloaded_files,
                                        total_bytes, downloaded_bytes, destination_path,
                                        enqueued_at, completed_at
                                    ) VALUES (?, 'completed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                                """, (model_id, len(discovered_files), len(discovered_files), model_file_bytes, model_file_bytes, str(m_dir)))

            # 7. Record Release Drop in releases table
            with self._get_db() as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
                if "releases" in tables:
                    with conn:
                        # Counted, not added, so a rescan can't inflate it.
                        model_count = conn.execute("""
                            SELECT COUNT(*) FROM models
                            WHERE source = 'external' AND is_active = 1 AND designer_name = ? AND release_month = ?;
                        """, (default_creator, release_month)).fetchone()[0]
                        release_id = f"external_{default_creator}_{release_month}"
                        updated = conn.execute(
                            "UPDATE releases SET model_count = ?, is_active = 1 WHERE id = ?;", (model_count, release_id)
                        )
                        if not updated.rowcount:
                            conn.execute("""
                                INSERT INTO releases (
                                    id, designer_name, designer_id, release_month,
                                    model_count, total_files, total_size_bytes
                                ) VALUES (?, ?, 0, ?, ?, 0, 0);
                            """, (release_id, default_creator, release_month, model_count))

        # 8. Update External Library Telemetry in SQLite
        with self._get_db() as conn:
            with conn:
                conn.execute("""
                    UPDATE external_libraries
                    SET total_models = ?,
                        total_files = ?,
                        disk_bytes = ?,
                        last_scanned_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (models_indexed, files_indexed, total_scanned_bytes, library_id))

        summary = {
            "success": True,
            "library_id": library_id,
            "library_name": library_name,
            "releases_scanned": total_releases,
            "models_indexed": models_indexed,
            "files_indexed": files_indexed,
            "archives_peeked": archives_peeked,
            "blender_renders_run": blender_renders_run,
            "total_bytes": total_scanned_bytes,
            "completed_at": datetime.now().isoformat(),
        }

        progress(100.0, f"Reconciliation finished: {models_indexed} models, {files_indexed} files.")
        log(f"In-place library reconciliation completed for '{library_name}'! Indexed {models_indexed} models, {files_indexed} files.")
        return summary

    def unlink_library(self, library_id: int) -> Dict[str, Any]:
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

    scan_and_index_library = scan_and_reconcile_library
