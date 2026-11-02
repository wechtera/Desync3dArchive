"""
services/mesh_renderer.py - Headless Blender Mesh Thumbnail Engine & Ephemeral Pipeline.
Automates high-quality isometric matcap rendering of STL files.
Supports persistent cover generation and on-demand ephemeral rendering with zero disk footprint.
"""

import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import List, Optional, Union

from desync_archival import external
from desync_archival import settings as app_config
from desync_archival.db.queries import mesh_renderer as queries

SCRIPT_PATH = app_config.blender_script()

try:
    from desync_archival.services.logger import log_layer, logger
except ImportError:
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

MODEL_PRIORITY_TOKENS = ["fullbody", "complete", "onepiece", "body", "character"]
AVOID_TOKENS = ["base", "supported", "churras", "weapon", "shield", "wing", "arm", "head", "tail", "leg"]
VALID_VIEWS = {"front_left", "front_right", "back_right", "back_left", "top_down", "bottom_up"}


class MeshRenderer:
    def __init__(self, blender_bin: Optional[str] = None):
        self.blender_bin = blender_bin or self.detect_blender_binary()

        self._bin_unrar = external.find_unrar()
        self._bin_7z = external.find_7zip()

    @staticmethod
    def detect_blender_binary() -> Optional[str]:
        return external.find_blender()

    def is_available(self) -> bool:
        return self.blender_bin is not None and Path(self.blender_bin).exists()

    def select_best_render_candidate(self, directory: Path) -> Optional[Path]:
        """
        Selects the best un-supported body mesh from loose files on disk.
        """
        return self.best_of(directory.rglob("*"))

    def best_of(self, paths: Iterable[Path]) -> Path | None:
        """The best mesh from a given set of files. Callers decide which files are in play."""
        all_stls: List[Path] = []
        supported_stls: list[Path] = []
        for f in paths:
            if not f.is_file() or f.suffix.lower() != ".stl":
                continue
            (supported_stls if re.search(r"(?<!un)supported", str(f), re.IGNORECASE) else all_stls).append(f)

        # A render with supports beats no cover at all.
        all_stls = all_stls or supported_stls
        if not all_stls:
            return None

        scored = []
        for p in all_stls:
            name_lower = p.stem.lower()
            score = 0

            if "32mm" in str(p).lower():
                score += 15

            for token in MODEL_PRIORITY_TOKENS:
                if token in name_lower:
                    score += 50
                    break

            for token in AVOID_TOKENS:
                if token in name_lower:
                    score -= 40

            try:
                size_mb = p.stat().st_size / (1024 * 1024)
                score += min(size_mb, 25)
            except OSError:
                pass

            scored.append((score, p))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def render_view(
        self,
        stl_path: Union[str, Path],
        view: str,
        output_image_path: Union[str, Path],
        rot_x: float = 0.0,
        rot_y: float = 0.0,
        rot_z: float = 0.0,
        resolution: int = 800,
    ) -> bool:
        """
        Invokes Blender in headless background mode to render one of the 6 camera angles.
        """
        if not self.is_available():
            log_layer("storage", logging.WARNING, "Blender executable not found. Cannot render STL.")
            return False

        if not SCRIPT_PATH.exists():
            log_layer("storage", logging.ERROR, f"Blender render script missing: {SCRIPT_PATH}")
            return False

        view_clean = str(view).lower().strip()
        if view_clean not in VALID_VIEWS:
            view_clean = "front_left"

        stl = Path(stl_path).resolve()
        if not stl.exists() or not stl.is_file():
            log_layer("storage", logging.ERROR, f"Input STL file does not exist: {stl}")
            return False

        out = Path(output_image_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.blender_bin,
            "-b",
            "-P",
            str(SCRIPT_PATH),
            "--",
            "--input",
            str(stl),
            "--output",
            str(out),
            "--view",
            view_clean,
            "--rot-x",
            str(rot_x),
            "--rot-y",
            str(rot_y),
            "--rot-z",
            str(rot_z),
            "--resolution",
            str(resolution),
        ]

        try:
            log_layer("storage", logging.DEBUG, f"Blender rendering view '{view_clean}' for {stl.name} -> {out.name}")
            proc = external.run(cmd, 120.0)
            if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
                log_layer("storage", logging.INFO, f"Generated view '{view_clean}' for {stl.name}")
                return True
            else:
                err = (proc.stderr or proc.stdout or "").strip()
                log_layer("storage", logging.WARNING, f"Blender render failed (code {proc.returncode}): {err[-300:]}")
                return False
        except subprocess.TimeoutExpired:
            log_layer("storage", logging.ERROR, f"Blender render timed out on {stl.name}")
            return False
        except Exception as e:
            log_layer("storage", logging.ERROR, f"Blender execution exception: {e}")
            return False

    def render_primary_thumbnail(
        self,
        stl_path: Union[str, Path],
        output_path: Union[str, Path],
        rot_x: float = 0.0,
        rot_y: float = 0.0,
        rot_z: float = 0.0,
    ) -> bool:
        """Generates front_left view to persistent cover.jpg."""
        return self.render_view(stl_path, "front_left", output_path, rot_x, rot_y, rot_z)

    def render_ephemeral(
        self,
        conn: sqlite3.Connection,
        source_type: str,
        source_id: int,
        view: str = "front_left",
        resolution: int = 800,
    ) -> bytes:
        """
        Renders an ephemeral thumbnail on-the-fly and returns raw JPEG bytes.
        Supports source_type == 'file' (loose STL or container archive) and 'archive_member'.
        Guarantees zero persistent files saved in previews/.
        """
        stype = str(source_type).lower().strip()
        if stype not in ("file", "archive_member", "model"):
            raise ValueError(f"Invalid source_type '{source_type}'. Must be 'file', 'archive_member', or 'model'.")

        vname = str(view).lower().strip()
        if vname not in VALID_VIEWS:
            vname = "front_left"

        if not self.is_available():
            raise RuntimeError("Blender executable not found on host machine.")

        scratch_uuid = uuid.uuid4().hex[:12]
        scratch_dir = Path(tempfile.gettempdir()) / f".desync_ephemeral_{scratch_uuid}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        temp_img = scratch_dir / f"render_{vname}.jpg"

        try:
            if stype == "archive_member":
                m_row = conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_ID, (source_id,)).fetchone()
                if not m_row:
                    raise FileNotFoundError(f"Archive member #{source_id} not found in catalog.")

                file_id = m_row["file_id"]
                internal_path = m_row["internal_path"]
                filename = m_row["filename"]

                arc_row = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID, (file_id,)).fetchone()
                if not arc_row:
                    raise FileNotFoundError(f"Parent archive for member #{source_id} not found in downloaded_files.")

                archive_path = None
                for p_str in (arc_row["vault_path"], arc_row["staging_path"]):
                    if p_str and Path(p_str).exists() and Path(p_str).is_file():
                        archive_path = Path(p_str)
                        break

                if not archive_path:
                    raise FileNotFoundError(f"Parent archive on disk not found for member #{source_id}.")

                extracted_stl = scratch_dir / filename
                self._surgical_extract_single_member(archive_path, internal_path, scratch_dir, extracted_stl)
                success = self.render_view(extracted_stl, vname, temp_img, resolution=resolution)

            else:
                # Resolve source_type == 'file' or 'model'
                target_stl = None

                if stype == "file":
                    row = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID_V2, (source_id,)).fetchone()
                    if not row:
                        row = conn.execute(queries.SELECT_MODEL_FILES_BY_FILE_ID, (source_id,)).fetchone()

                    if not row:
                        raise FileNotFoundError(f"File #{source_id} not found in database.")

                    ext = str(row["file_extension"] or Path(row["cleaned_filename"] or "").suffix).lower()
                    file_disk_path = None
                    for p_str in (row["vault_path"], row["staging_path"]):
                        if p_str and Path(p_str).exists() and Path(p_str).is_file():
                            file_disk_path = Path(p_str)
                            break

                    if not file_disk_path:
                        raise FileNotFoundError(f"Physical file #{source_id} not found on storage.")

                    if ext == ".stl":
                        target_stl = file_disk_path
                        success = self.render_view(target_stl, vname, temp_img, resolution=resolution)
                    elif ext in (".zip", ".rar", ".7z"):
                        # Extract best candidate from within this archive
                        extracted = self._extract_best_stl_from_archive(conn, row["file_id"], file_disk_path, scratch_dir)
                        if not extracted:
                            raise FileNotFoundError(f"No STL meshes found inside archive {file_disk_path.name}.")
                        success = self.render_view(extracted, vname, temp_img, resolution=resolution)
                    else:
                        raise ValueError(f"File #{source_id} is '{ext}', which cannot be rendered as 3D geometry.")

                elif stype == "model":
                    model_id = source_id
                    extracted = self._resolve_model_stl_candidate(conn, model_id, scratch_dir)
                    if not extracted:
                        raise FileNotFoundError(f"No STL meshes found for Model #{model_id}.")
                    success = self.render_view(extracted, vname, temp_img, resolution=resolution)

            if not success or not temp_img.exists() or temp_img.stat().st_size == 0:
                raise RuntimeError(f"Blender failed to render ephemeral view '{vname}' for {stype} #{source_id}.")

            return temp_img.read_bytes()

        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    def _extract_best_stl_from_archive(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        archive_path: Path,
        scratch_dir: Path,
    ) -> Optional[Path]:
        """Inspects archive members for a file, peeking if needed, and extracts the top candidate STL."""
        members = conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID, (file_id,)).fetchall()

        if not members:
            # Auto-peek archive if not yet indexed
            try:
                from desync_archival.services.archive_peeker import ArchivePeeker
                db_path = Path(conn.execute("PRAGMA database_list;").fetchone()[2]).resolve()
                peeker = ArchivePeeker(db_path)
                m_row = conn.execute(queries.SELECT_DOWNLOADED_FILES_MODEL_ID_BY_FILE_ID, (file_id,)).fetchone()
                mid = m_row[0] if m_row else 0
                peeker.inspect_and_record(mid, file_id, archive_path)
                members = conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID_LOWER, (file_id,)).fetchall()
            except Exception:
                pass

        if not members:
            return None

        scored = []
        for m in members:
            fn_low = m["filename"].lower()
            is_supp = bool(m["is_supported"]) or "supported" in fn_low
            score = -100 if is_supp else 0
            if "32mm" in fn_low:
                score += 15
            for token in MODEL_PRIORITY_TOKENS:
                if token in fn_low:
                    score += 50
                    break
            for token in AVOID_TOKENS:
                if token in fn_low:
                    score -= 40
            sz = m["size_bytes"] or 0
            score += min(sz / (1024 * 1024), 25)
            scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        winner = scored[0][1]

        target_file = scratch_dir / winner["filename"]
        self._surgical_extract_single_member(archive_path, winner["internal_path"], scratch_dir, target_file)
        return target_file if target_file.exists() else None

    def _resolve_model_stl_candidate(
        self,
        conn: sqlite3.Connection,
        model_id: int,
        scratch_dir: Path,
    ) -> Optional[Path]:
        """Resolves the best STL candidate for a model from loose vault files or internal archive members."""
        model = conn.execute(queries.SELECT_COLLECTIONS_VAULT_PATH_BY_ID, (model_id,)).fetchone()
        if model and model["vault_path"]:
            vp = Path(model["vault_path"])
            if vp.exists() and vp.is_dir():
                loose = self.select_best_render_candidate(vp)
                if loose:
                    return loose
            elif vp.exists() and vp.is_file() and vp.suffix.lower() == ".stl":
                return vp

        # Loose files before archives, because extracting costs real time on the NAS.
        loose_rows = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_MODEL_ID, (model_id,)).fetchall()

        for lr in loose_rows:
            for p_str in (lr["vault_path"], lr["staging_path"]):
                if p_str:
                    c = Path(p_str)
                    if c.exists() and c.is_file() and c.suffix.lower() == ".stl":
                        return c

        arc_rows = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_MODEL_ID_LOWER, (model_id,)).fetchall()

        for ar in arc_rows:
            for p_str in (ar["vault_path"], ar["staging_path"]):
                if p_str and Path(p_str).exists() and Path(p_str).is_file():
                    extracted = self._extract_best_stl_from_archive(conn, ar["file_id"], Path(p_str), scratch_dir)
                    if extracted:
                        return extracted

        return None

    @staticmethod
    def _on_disk(row: sqlite3.Row, label: str) -> Path:
        for stored in (row["vault_path"], row["staging_path"]):
            if stored and Path(stored).is_file():
                return Path(stored)
        raise FileNotFoundError(f"{label} isn't reachable. The NAS may be offline.")

    def locate_stl(self, conn: sqlite3.Connection, source_type: str, source_id: int, scratch_dir: Path) -> Path:
        """Finds one STL from the catalog, unpacking it into scratch if it sits in an archive.

        Raises:
            FileNotFoundError: It isn't in the catalog, or isn't reachable.
            ValueError: It isn't an STL.
        """
        if source_type == "archive_member":
            member = conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_ID, (source_id,)).fetchone()
            if not member:
                raise FileNotFoundError(f"Archive member #{source_id} isn't in the catalog.")
            if Path(member["filename"]).suffix.lower() != ".stl":
                raise ValueError(f"{member['filename']} isn't an STL.")
            parent = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID, (member["file_id"],)).fetchone()
            if not parent:
                raise FileNotFoundError(f"The archive holding {member['filename']} hasn't been downloaded.")
            target = scratch_dir / Path(member["filename"]).name
            self._surgical_extract_single_member(
                self._on_disk(parent, "Its archive"), member["internal_path"], scratch_dir, target,
            )
            return target

        row = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID_V2, (source_id,)).fetchone()
        if not row:
            raise FileNotFoundError(f"File #{source_id} hasn't been downloaded.")
        if Path(row["cleaned_filename"] or "").suffix.lower() != ".stl":
            raise ValueError(f"{row['cleaned_filename']} isn't an STL.")
        return self._on_disk(row, "This file")

    def _surgical_extract_single_member(
        self,
        archive_path: Path,
        internal_path: str,
        scratch_dir: Path,
        dest_file: Path,
    ) -> int:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        pass_dir = scratch_dir / f"pass_{uuid.uuid4().hex[:8]}"
        pass_dir.mkdir(parents=True, exist_ok=True)
        try:
            found = external.extract_member(archive_path, internal_path, pass_dir, self._bin_unrar, self._bin_7z)
            sz = found.stat().st_size
            shutil.move(str(found), str(dest_file))
        finally:
            shutil.rmtree(pass_dir, ignore_errors=True)
        return sz
