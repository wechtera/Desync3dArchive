"""
services/stash_exporter.py - Slicer Stash Exporter Engine.
Materializes curated stashes from NAS archives to local workstation print queues.

Enforces:
1. READ-ONLY NAS ACCESS: Archives are opened strictly with read flags ("rb", zipfile.ZipFile(..., "r")).
2. LOCAL SCRATCH ONLY: Decompression executes inside <download_work_path>/.stash_scratch/<job_uuid>/.
3. GUARANTEED SCRATCH PURGE: Scratch directories are cleaned up in a try...finally block.
4. SURGICAL EXTRACTION: Extracts only stashed submodels/members without unpacking surrounding archives.
5. SLICER LAYOUT: YYYY-MM-DD_HH-MM-SS_<stash_name>/[Creator] Model/Submodel/preview.jpg + meshes.
"""

import json
import logging
import re
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from desync_archival import external
from desync_archival import settings as app_config
from desync_archival.db.queries import stash_exporter as queries
from desync_archival.services.paths import is_supported, sweep_leftovers

try:
    from desync_archival.services.logger import log_layer, logger
except ImportError:
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

DEFAULT_STASH_EXPORT_PATH = Path.home() / "Downloads" / "DESYNC_Print_Queue"
PARTIAL_SUFFIX = ".partial"
MODEL_EXTENSIONS = {".stl", ".obj", ".lys", ".ctb", ".ags", ".3mf", ".chitubox", ".prusa"}


def sanitize_folder_name(name: str) -> str:
    """Sanitizes directory names across macOS, Linux, and Windows."""
    if not name:
        return "Unknown"
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    clean = re.sub(r"\s+", " ", clean).rstrip(". ")
    return clean or "Unknown"


def free_path(path: Path) -> Path:
    """Finds a name nothing is using yet, because an export must never overwrite a file it just wrote."""
    number = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        number += 1
    return candidate


class StashExporter:
    """Materializes a stash into a slicer-ready folder, using local scratch only."""
    def __init__(
        self,
        db_path: Optional[Union[Path, str]] = None,
        export_root: Optional[Union[Path, str]] = None,
        work_path: Optional[Union[Path, str]] = None,
        previews_root: Optional[Union[Path, str]] = None,
    ):
        self.db_path = Path(db_path).resolve() if db_path else app_config.db_path().resolve()
        self._custom_export_root = Path(export_root).resolve() if export_root else None
        self._custom_work_path = Path(work_path).resolve() if work_path else None
        self._previews_root = Path(previews_root).resolve() if previews_root else app_config.previews_dir().resolve()

        self._bin_unrar = external.find_unrar()
        self._bin_7z = external.find_7zip()

    def _get_connection(self, conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
        if conn is not None:
            conn.row_factory = sqlite3.Row
            return conn
        c = sqlite3.connect(str(self.db_path), timeout=45.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 45000;")
        return c

    def _get_setting(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute(queries.SELECT_APP_SETTINGS_VALUE_BY_KEY, (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def resolve_paths(self, conn: sqlite3.Connection) -> Tuple[Path, Path, Path]:
        if self._custom_export_root:
            export_root = self._custom_export_root
        else:
            val = self._get_setting(conn, "stash_download_path")
            export_root = Path(val).expanduser().resolve() if val else DEFAULT_STASH_EXPORT_PATH.resolve()

        if self._custom_work_path:
            work_path = self._custom_work_path
        else:
            val = self._get_setting(conn, "download_work_path")
            work_path = Path(val).expanduser().resolve() if val else app_config.staging_dir().resolve()

        export_root.mkdir(parents=True, exist_ok=True)
        work_path.mkdir(parents=True, exist_ok=True)
        return export_root, work_path, self._previews_root

    def export_stash(
        self,
        stash_id: int,
        filter_mode: str = "all",
        conn: Optional[sqlite3.Connection] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        Exports a curated stash into a timestamped, slicer-ready folder.
        Uses local scratch space for all intermediate decompression and preserves NAS immutability.
        """
        c = self._get_connection(conn)
        should_close = conn is None
        try:
            stash = c.execute(queries.SELECT_STASHES_BY_ID, (stash_id,)).fetchone()
            if not stash:
                raise ValueError(f"Stash #{stash_id} does not exist.")

            stash_name = stash["name"]
            items = c.execute(queries.SELECT_STASH_ITEMS_BY_STASH_ID, (stash_id,)).fetchall()
            if not items:
                raise ValueError(f"Stash '{stash_name}' (ID #{stash_id}) has no items to export.")

            export_root, work_path, previews_root = self.resolve_paths(c)
            self._check_free_space(self._estimated_bytes(c, items), export_root, work_path)

            sweep_leftovers(export_root, f"*{PARTIAL_SUFFIX}")

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            folder_name = f"{timestamp}_{sanitize_folder_name(stash_name)}"
            final_export_dir = export_root / folder_name
            # Built under another name, so a folder in the print queue is always a whole export.
            target_export_dir = export_root / f"{folder_name}{PARTIAL_SUFFIX}"
            target_export_dir.mkdir(parents=True, exist_ok=True)

            job_uuid = uuid.uuid4().hex[:8]
            scratch_dir = work_path / ".stash_scratch" / job_uuid
            scratch_dir.mkdir(parents=True, exist_ok=True)

            init_msg = f"Starting Slicer Export for Stash #{stash_id} ('{stash_name}') -> {final_export_dir}"
            log_layer("storage", logging.INFO, init_msg)
            if log_cb:
                log_cb(init_msg)

            total_items = len(items)
            total_files_exported = 0
            total_bytes_exported = 0
            manifest_items = []
            skipped_inactive = 0

            try:
                for idx, item in enumerate(items, start=1):
                    if cancel_cb and cancel_cb():
                        raise InterruptedError("Export cancelled by operator.")

                    # Again here, because an unpeeked archive's real size is only known as it unpacks.
                    self._check_free_space(self._estimated_bytes(c, [item]), target_export_dir, scratch_dir)

                    if progress_cb:
                        pct = round(((idx - 1) / total_items) * 90.0, 1)
                        progress_cb(pct, f"Extracting item {idx}/{total_items}...")

                    inactive = c.execute(
                        queries.SELECT_COLLECTIONS_1_BY_ID, (item["model_id"],)
                    ).fetchone()
                    if inactive:
                        skipped_inactive += 1
                        log_layer("storage", logging.WARNING, f"Stash #{stash_id}: Skipped inactive Model #{item['model_id']}")
                        if log_cb:
                            log_cb(f"[{idx}/{total_items}] Skipped Model #{item['model_id']}: its library is unlinked.")
                        continue

                    res = self._export_single_item(
                        conn=c,
                        item=dict(item),
                        target_export_dir=target_export_dir,
                        scratch_dir=scratch_dir,
                        previews_root=previews_root,
                        filter_mode=filter_mode,
                    )
                    total_files_exported += res["files_count"]
                    total_bytes_exported += res["bytes_count"]
                    manifest_items.append(res["manifest_entry"])

                    if log_cb:
                        log_cb(f"[{idx}/{total_items}] Extracted {res['files_count']} files ({res['bytes_count']} B)")

                manifest_data = {
                    "stash_id": stash_id,
                    "stash_name": stash_name,
                    "description": stash["description"],
                    "exported_at": datetime.now().isoformat(),
                    "folder_name": folder_name,
                    "export_path": str(final_export_dir),
                    "filter_mode": filter_mode,
                    "total_files": total_files_exported,
                    "total_bytes": total_bytes_exported,
                    "skipped_inactive_items": skipped_inactive,
                    "items": manifest_items,
                }
                (target_export_dir / "manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
                target_export_dir.rename(final_export_dir)
                manifest_file = final_export_dir / "manifest.json"

                if progress_cb:
                    progress_cb(100.0, f"Export complete: {total_files_exported} files staged.")

                done_msg = f"Export complete: {total_files_exported} files ({total_bytes_exported} bytes) staged to {final_export_dir}"
                log_layer("storage", logging.INFO, done_msg)
                if log_cb:
                    log_cb(done_msg)

                return {
                    "success": True,
                    "stash_id": stash_id,
                    "stash_name": stash_name,
                    "export_path": str(final_export_dir),
                    "folder_name": folder_name,
                    "total_files": total_files_exported,
                    "total_bytes": total_bytes_exported,
                    "manifest_path": str(manifest_file),
                }

            finally:
                # Gone already after the rename above, so this only ever removes an unfinished build.
                shutil.rmtree(target_export_dir, ignore_errors=True)
                if scratch_dir.exists():
                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    log_layer("storage", logging.DEBUG, f"Cleaned up local scratch directory: {scratch_dir}")

        finally:
            if should_close:
                c.close()

    def _estimated_bytes(self, conn: sqlite3.Connection, items: List[Any]) -> int:
        """Bytes a stash could unpack to. An unpeeked archive counts at its own size, the best known."""
        total = 0
        for item in items:
            kind = item["item_type"]
            if kind == "submodel":
                row = conn.execute(queries.SELECT_SUBMODEL_BYTES, (item["submodel_id"],)).fetchone()
            elif kind == "archive_member":
                row = conn.execute(queries.SELECT_MEMBER_BYTES, (item["archive_member_id"],)).fetchone()
            elif kind == "file":
                row = conn.execute(queries.SELECT_FILE_UNPACKED_BYTES, (item["file_id"],)).fetchone()
            else:
                row = conn.execute(queries.SELECT_MODEL_UNPACKED_BYTES, (item["model_id"],)).fetchone()
            total += (row[0] or 0) if row else 0
        return total

    def _check_free_space(self, needed: int, *folders: Path) -> None:
        for folder in folders:
            usable = shutil.disk_usage(folder).free - external.FREE_SPACE_RESERVE
            if needed > usable:
                raise ValueError(
                    f"This stash needs about {needed / 1024**3:.1f} GB, and {folder} has "
                    f"{max(usable, 0) / 1024**3:.1f} GB free after the safety margin."
                )

    def _export_single_item(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        target_export_dir: Path,
        scratch_dir: Path,
        previews_root: Path,
        filter_mode: str,
    ) -> Dict[str, Any]:
        item_type = item["item_type"]
        model_id = item["model_id"]

        model_row = conn.execute(queries.SELECT_COLLECTIONS_BY_ID, (model_id,)).fetchone()
        if not model_row or not model_row["vault_path"]:
            raise ValueError(f"Model #{model_id} has no vault directory. Cannot export.")

        model_name = model_row["name"] or f"Model_{model_id}"
        creator_name = model_row["designer_name"] or "Unknown_Creator"

        model_folder_name = sanitize_folder_name(f"[{creator_name}] {model_name}")
        model_dir = target_export_dir / model_folder_name
        model_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_model_preview(previews_root, model_id, model_dir)

        if item_type == "submodel":
            return self._export_submodel(conn, item, model_dir, scratch_dir, previews_root, filter_mode, creator_name, model_name)
        elif item_type == "archive_member":
            return self._export_archive_member(conn, item, model_dir, scratch_dir, previews_root, creator_name, model_name)
        elif item_type == "file":
            return self._export_file(conn, item, model_dir, scratch_dir, filter_mode, creator_name, model_name)
        elif item_type == "model":
            return self._export_whole_model(conn, model_id, model_dir, scratch_dir, filter_mode, creator_name, model_name)
        else:
            raise ValueError(f"Unknown item_type '{item_type}'.")

    def _export_submodel(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        model_dir: Path,
        scratch_dir: Path,
        previews_root: Path,
        filter_mode: str,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        submodel_id = item["submodel_id"]
        sm_row = conn.execute(queries.SELECT_FILE_ARCHIVE_SUBMODELS_BY_ID, (submodel_id,)).fetchone()
        if not sm_row:
            raise ValueError(f"Submodel #{submodel_id} not found in catalog.")

        submodel_name = sm_row["name"] or f"Submodel_{submodel_id}"
        file_id = sm_row["file_id"]

        archive_row = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID, (file_id,)).fetchone()
        if not archive_row:
            raise ValueError(f"Parent archive for submodel #{submodel_id} not found in downloaded_files.")

        archive_path = self._resolve_archive_disk_path(archive_row)
        # Old peeks stored 7-Zip's listing header, the archive's own disk path, as a member. 20 such
        # rows exist in production. They aren't files in the archive, so there's nothing to export.
        members = [
            m for m in conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_SUBMODEL_ID, (submodel_id,)).fetchall()
            if not Path(m["internal_path"]).is_absolute()
        ]

        submodel_dir = model_dir / sanitize_folder_name(submodel_name)
        submodel_dir.mkdir(parents=True, exist_ok=True)

        self._copy_submodel_preview(previews_root, sm_row["model_id"], file_id, sm_row["preview_image_path"], submodel_dir)

        extracted_files = []
        bytes_count = 0

        targets = []
        for m in members:
            fn = m["filename"]
            is_supp = bool(m["is_supported"]) or is_supported(fn)
            if filter_mode == "presupported" and not is_supp:
                continue
            if filter_mode == "unsupported" and is_supp:
                continue
            targets.append(m)

        if not targets:
            # Business rule: a filter that matches nothing exports everything, so a submodel is never empty.
            targets = list(members)

        # The peeker drops format folders from submodels, so Supported/Head.stl and Unsupported/Head.stl
        # can share one, and each needs its own place here.
        placed = external.export_paths([m["internal_path"] for m in targets])
        for m in targets:
            relative = placed[m["internal_path"]]
            sz = self._surgical_extract_single_member(
                archive_path, m["internal_path"], scratch_dir, submodel_dir / relative, m["size_bytes"] or 0,
            )
            bytes_count += sz
            extracted_files.append(relative)

        return {
            "files_count": len(extracted_files),
            "bytes_count": bytes_count,
            "manifest_entry": {
                "item_type": "submodel",
                "submodel_id": submodel_id,
                "submodel_name": submodel_name,
                "model_id": sm_row["model_id"],
                "model_name": model_name,
                "creator_name": creator_name,
                "destination_folder": str(submodel_dir.relative_to(model_dir.parent)),
                "files": extracted_files,
                "bytes": bytes_count,
            },
        }

    def _export_archive_member(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        model_dir: Path,
        scratch_dir: Path,
        previews_root: Path,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        member_id = item["archive_member_id"]
        m_row = conn.execute(queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_ID, (member_id,)).fetchone()
        if not m_row:
            raise ValueError(f"Archive member #{member_id} not found in catalog.")

        file_id = m_row["file_id"]
        archive_row = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID, (file_id,)).fetchone()
        if not archive_row:
            raise ValueError(f"Parent archive for member #{member_id} not found in downloaded_files.")

        archive_path = self._resolve_archive_disk_path(archive_row)

        target_dir = model_dir
        if m_row["submodel_id"]:
            sm = conn.execute(queries.SELECT_FILE_ARCHIVE_SUBMODELS_BY_ID_V2, (m_row["submodel_id"],)).fetchone()
            if sm:
                target_dir = model_dir / sanitize_folder_name(sm["name"])
                target_dir.mkdir(parents=True, exist_ok=True)
                self._copy_submodel_preview(previews_root, sm["model_id"], file_id, sm["preview_image_path"], target_dir)

        if not external.is_safe_member(m_row["internal_path"]):
            raise external.ExtractionRefused(f"Unsafe member path: {m_row['internal_path']!r}")
        dest_file = free_path(target_dir / m_row["filename"])
        sz = self._surgical_extract_single_member(
            archive_path, m_row["internal_path"], scratch_dir, dest_file, m_row["size_bytes"] or 0,
        )

        return {
            "files_count": 1,
            "bytes_count": sz,
            "manifest_entry": {
                "item_type": "archive_member",
                "archive_member_id": member_id,
                "filename": m_row["filename"],
                "model_id": item["model_id"],
                "model_name": model_name,
                "creator_name": creator_name,
                "destination_file": str(dest_file.relative_to(model_dir.parent)),
                "bytes": sz,
            },
        }

    def _export_file(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        model_dir: Path,
        scratch_dir: Path,
        filter_mode: str,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        file_id = item["file_id"]
        archive_row = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_FILE_ID_V2, (file_id,)).fetchone()
        if not archive_row:
            raise ValueError(f"File #{file_id} not found in downloaded_files.")

        src_path = self._resolve_archive_disk_path(archive_row)
        ext = src_path.suffix.lower()

        extracted_files = []
        bytes_count = 0

        if ext in (".zip", ".rar", ".7z"):
            files, b_count = self._unpack_archive_to_dest(src_path, model_dir, scratch_dir, filter_mode)
            extracted_files.extend(files)
            bytes_count += b_count
        else:
            dest_file = model_dir / src_path.name
            sz = self._copy_file_via_scratch(src_path, dest_file, scratch_dir)
            extracted_files.append(dest_file.name)
            bytes_count += sz

        return {
            "files_count": len(extracted_files),
            "bytes_count": bytes_count,
            "manifest_entry": {
                "item_type": "file",
                "file_id": file_id,
                "model_id": item["model_id"],
                "model_name": model_name,
                "creator_name": creator_name,
                "files": extracted_files,
                "bytes": bytes_count,
            },
        }

    def _export_whole_model(
        self,
        conn: sqlite3.Connection,
        model_id: int,
        model_dir: Path,
        scratch_dir: Path,
        filter_mode: str,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        file_rows = conn.execute(queries.SELECT_DOWNLOADED_FILES_BY_MODEL_ID, (model_id,)).fetchall()
        if not file_rows:
            raise ValueError(f"Model #{model_id} has no downloaded files recorded.")

        all_extracted = []
        total_bytes = 0

        for fr in file_rows:
            src_path = self._resolve_archive_disk_path(fr)
            ext = src_path.suffix.lower()
            if ext in (".zip", ".rar", ".7z"):
                files, b_count = self._unpack_archive_to_dest(src_path, model_dir, scratch_dir, filter_mode)
                all_extracted.extend(files)
                total_bytes += b_count
            else:
                dest_file = model_dir / src_path.name
                sz = self._copy_file_via_scratch(src_path, dest_file, scratch_dir)
                all_extracted.append(dest_file.name)
                total_bytes += sz

        return {
            "files_count": len(all_extracted),
            "bytes_count": total_bytes,
            "manifest_entry": {
                "item_type": "model",
                "model_id": model_id,
                "model_name": model_name,
                "creator_name": creator_name,
                "files": all_extracted,
                "bytes": total_bytes,
            },
        }

    def _resolve_archive_disk_path(self, row: sqlite3.Row) -> Path:
        v_path = row["vault_path"]
        s_path = row["staging_path"]
        target = Path(v_path) if v_path else (Path(s_path) if s_path else None)

        if not target or not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Vault archive file does not exist on storage: {target}")
        return target

    # FIXME(P-05): reopens the archive per member, and each RAR member is a new process.
    def _surgical_extract_single_member(
        self,
        archive_path: Path,
        internal_path: str,
        scratch_dir: Path,
        dest_file: Path,
        expected_size: int = 0,
    ) -> int:
        """Extracts one entry through local scratch. An existing file is an error, never overwritten."""
        if dest_file.exists():
            raise FileExistsError(f"Refusing to overwrite {dest_file.name} in the export folder")
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        pass_dir = scratch_dir / f"pass_{uuid.uuid4().hex[:8]}"
        pass_dir.mkdir(parents=True, exist_ok=True)
        try:
            found = external.extract_member(
                archive_path, internal_path, pass_dir, self._bin_unrar, self._bin_7z, expected_size,
            )
            sz = found.stat().st_size
            shutil.move(str(found), str(dest_file))
        finally:
            shutil.rmtree(pass_dir, ignore_errors=True)
        return sz

    def _unpack_archive_to_dest(
        self,
        archive_path: Path,
        dest_dir: Path,
        scratch_dir: Path,
        filter_mode: str,
    ) -> Tuple[List[str], int]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = archive_path.suffix.lower()
        extracted_names = []
        total_bytes = 0

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if name.startswith("._") or "__macosx" in name.lower() or Path(name).name.lower() in (".ds_store", "thumbs.db"):
                        continue

                    p_ext = Path(name).suffix.lower()
                    if p_ext not in MODEL_EXTENSIONS:
                        continue

                    is_supp = is_supported(name)
                    if filter_mode == "presupported" and not is_supp:
                        continue
                    if filter_mode == "unsupported" and is_supp:
                        continue

                    clean_name = Path(name).name
                    temp_scratch = scratch_dir / f"unpack_{uuid.uuid4().hex[:8]}_{clean_name}"
                    with zf.open(info, "r") as src:
                        external.copy_limited(src, temp_scratch, external.member_limit(scratch_dir))

                    out_file = dest_dir / clean_name
                    if out_file.exists():
                        out_file = free_path(dest_dir / f"{Path(name).parent.name}_{clean_name}")

                    sz = temp_scratch.stat().st_size
                    shutil.move(str(temp_scratch), str(out_file))
                    extracted_names.append(out_file.name)
                    total_bytes += sz
        else:
            temp_extract_folder = scratch_dir / f"unpack_{uuid.uuid4().hex[:8]}"
            temp_extract_folder.mkdir(parents=True, exist_ok=True)

            external.extract_all(ext, archive_path, temp_extract_folder, self._bin_unrar, self._bin_7z)

            for item in external.regular_files(temp_extract_folder):
                if item.suffix.lower() in MODEL_EXTENSIONS:
                    is_supp = is_supported(str(item.relative_to(temp_extract_folder)))
                    if filter_mode == "presupported" and not is_supp:
                        continue
                    if filter_mode == "unsupported" and is_supp:
                        continue

                    out_file = dest_dir / item.name
                    if out_file.exists():
                        out_file = free_path(dest_dir / f"{item.parent.name}_{item.name}")
                    sz = item.stat().st_size
                    shutil.move(str(item), str(out_file))
                    extracted_names.append(out_file.name)
                    total_bytes += sz
            shutil.rmtree(temp_extract_folder, ignore_errors=True)

        return extracted_names, total_bytes

    def _copy_file_via_scratch(self, src_file: Path, dest_file: Path, scratch_dir: Path) -> int:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        temp_scratch = scratch_dir / f"copy_{uuid.uuid4().hex[:8]}_{dest_file.name}"

        with open(src_file, "rb") as f_in, open(temp_scratch, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        sz = temp_scratch.stat().st_size
        shutil.move(str(temp_scratch), str(dest_file))
        return sz

    def _ensure_model_preview(self, previews_root: Path, model_id: int, model_dir: Path):
        target = model_dir / "preview.jpg"
        if target.exists():
            return

        cover = previews_root / str(model_id) / "cover.jpg"
        if cover.exists() and cover.is_file():
            shutil.copy2(str(cover), str(target))
            return

        model_prev_dir = previews_root / str(model_id)
        if model_prev_dir.exists() and model_prev_dir.is_dir():
            for f in model_prev_dir.glob("*"):
                if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    shutil.copy2(str(f), str(target))
                    return

    def _copy_submodel_preview(
        self,
        previews_root: Path,
        model_id: int,
        file_id: Optional[int],
        preview_image_path: Optional[str],
        submodel_dir: Path,
    ):
        target = submodel_dir / "preview.jpg"
        if target.exists():
            return

        candidates = []
        if preview_image_path:
            clean_rel = preview_image_path.lstrip("/")
            candidates.append(app_config.data_dir() / clean_rel)
            candidates.append(previews_root / clean_rel)
            if file_id:
                candidates.append(previews_root / str(model_id) / str(file_id) / Path(clean_rel).name)
            candidates.append(previews_root / str(model_id) / Path(clean_rel).name)

        candidates.append(previews_root / str(model_id) / "cover.jpg")

        for c in candidates:
            if c and c.exists() and c.is_file():
                shutil.copy2(str(c), str(target))
                return
