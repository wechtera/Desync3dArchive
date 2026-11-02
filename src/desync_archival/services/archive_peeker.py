"""
services/archive_peeker.py - Bidirectional anchor inspection, submodel discovery, and thumbnail correlation engine.
Instrumented with dynamic topic-based subsystem logging ('archive', 'http', 'sql').
"""

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from desync_archival import external
from desync_archival import settings as app_config
from desync_archival.db.queries import archive_peeker as queries
from desync_archival.services import catalog_levels, paths
from desync_archival.services.db import get_db_connection, scoped_connection
from desync_archival.services.logger import log_layer
from desync_archival.services.paths import MODEL_EXTENSIONS

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Folders indicating packaging/format layers that should be peeled off for grouping
FORMAT_ROOT_FOLDERS = {
    "files", "stls", "original stl", "unsupported_stl", "unsupported",
    "supported", "presupported", "pre-supported", "presupported_stl",
    "presuported_stl", "lychee", "support stls", "separated"
}

# Recognized tabletop scale patterns
SCALE_PATTERN = re.compile(r"\b(28mm|32mm|54mm|75mm|100mm)\b", re.IGNORECASE)

# Common studio aliases mapping renders to submodel concepts
STUDIO_ALIASES = {
    "hero 1": ["archibald"],
    "hero 2": ["octavius"],
    "pinup": ["botticella", "venus"],
    "epic": ["leviathan", "stormtide"],
    "cavalry wild": ["saddleless", "no_saddle", "riders w"],
    "cavalry": ["moray rider", "rider"],
    "unit": ["lunatic", "guard", "soldier", "infantry"],
}


@dataclass
class ArchiveMemberInfo:
    internal_path: str
    filename: str
    size_bytes: int
    file_extension: str
    is_supported: bool = False
    scale: Optional[str] = None


@dataclass
class DiscoveredSubmodel:
    name: str
    relative_dir: str
    members: List[ArchiveMemberInfo] = field(default_factory=list)
    preview_image_path: Optional[str] = None
    scales: Set[str] = field(default_factory=set)
    formats: Set[str] = field(default_factory=set)
    has_presupported: bool = False
    has_unsupported: bool = False
    has_slicer_project: bool = False
    total_bytes: int = 0


class ArchivePeeker:
    """Reads an archive's listing, pulls its images and records its contents. Never changes the archive."""
    def __init__(self, db_path: Path, previews_root: Optional[Path] = None):
        self.db_path = Path(db_path).resolve()
        self.previews_root = Path(previews_root).resolve() if previews_root else app_config.previews_dir().resolve()
        self.previews_root.mkdir(parents=True, exist_ok=True)

        self._bin_unrar = external.find_unrar()
        self._bin_7z = external.find_7zip()


    def _get_db(self) -> sqlite3.Connection:
        return scoped_connection(self.db_path, foreign_keys=False)

    def inspect_and_record(self, model_id: int, file_id: int, archive_path: Path, build_catalog: bool = True) -> Dict[str, Any]:
        """Entry point: inspects an archive, performs bidirectional segmentation, and records submodels.

        A batch passes `build_catalog=False` and calls `finish_batch` once at the end, because
        each build rereads every archive and relearns its words.
        """
        archive_path = Path(archive_path).resolve()
        if not archive_path.exists():
            log_layer("archive", logging.ERROR, f"Archive missing: {archive_path}")
            return {"success": False, "error": f"Archive file missing: {archive_path}"}

        try:
            if archive_path.stat().st_size == 0:
                log_layer("archive", logging.WARNING, f"Archive is 0 bytes: {archive_path.name}")
                return {"success": False, "error": "Archive file is 0 bytes (empty)"}
        except Exception as e:
            return {"success": False, "error": f"Cannot read archive stats: {e}"}

        ext = archive_path.suffix.lower()
        log_layer("archive", logging.DEBUG, f"Peeking into archive: {archive_path.name} (Format: {ext})")

        try:
            if ext == ".zip":
                member_list = self._inspect_zip(archive_path)
                archive_type = "zip"
            elif ext in (".rar", ".7z"):
                member_list = self._inspect_rar(archive_path)
                archive_type = "rar"
            else:
                log_layer("archive", logging.WARNING, f"Unsupported archive format: {ext}")
                return {"success": False, "error": f"Unsupported archive format: {ext}"}
        except Exception as e:
            log_layer("archive", logging.ERROR, f"Archive read exception on {archive_path.name}: {e}")
            return {"success": False, "error": f"Corrupt or unreadable archive: {e}"}

        if not member_list:
            log_layer("archive", logging.WARNING, f"No entries inside {archive_path.name}")
            return {"success": False, "error": "Archive contains zero readable entries"}

        # 1. Filter out OS junk
        clean_members = [
            m for m in member_list
            if not m.filename.startswith("._")
            and not m.filename.lower() in (".ds_store", "thumbs.db", "desktop.ini")
            and not "__macosx" in m.internal_path.lower()
        ]

        # 2. Extract image previews
        image_paths = [m.internal_path for m in clean_members if m.file_extension in IMAGE_EXTENSIONS]
        dest_dir = self.previews_root / str(model_id) / str(file_id)
        extracted_images = self._extract_images(archive_path, image_paths, dest_dir, archive_type)

        if not extracted_images:
            catalog_img = self._fetch_catalog_fallback_image(model_id, dest_dir)
            if catalog_img:
                extracted_images["[CATALOG_PRIMARY]"] = catalog_img

        # 3. Separate 3D print members
        printable_members = [m for m in clean_members if m.file_extension in MODEL_EXTENSIONS]

        if not printable_members:
            log_layer("archive", logging.INFO, f"Model #{model_id} File #{file_id} classified as document/visual pack.")
            submodels = self._handle_document_pack(clean_members, extracted_images, model_id, file_id)
            self._commit_to_database(file_id, model_id, submodels, clean_members, build_catalog)
            return {
                "success": True,
                "archive_type": archive_type,
                "is_document_pack": True,
                "submodels_count": len(submodels),
                "images_extracted": len(extracted_images)
            }

        # 4. Bidirectional segmentation
        submodels = self._segment_submodels(printable_members, extracted_images, model_id, file_id)

        # 5. Multi-tier thumbnail correlation
        self._correlate_thumbnails(submodels, extracted_images, model_id, file_id)

        # 6. Persist to SQLite
        self._commit_to_database(file_id, model_id, submodels, clean_members, build_catalog)

        log_layer("archive", logging.INFO, f"Model #{model_id} ({archive_path.name}): Discovered {len(submodels)} submodels, {len(extracted_images)} renders.")

        return {
            "success": True,
            "archive_type": archive_type,
            "submodels_count": len(submodels),
            "images_extracted": len(extracted_images),
            "members_indexed": len(printable_members),
        }

    def _inspect_zip(self, path: Path) -> List[ArchiveMemberInfo]:
        with zipfile.ZipFile(path, "r") as zf:
            return [self._member_info(info.filename, info.file_size) for info in zf.infolist() if not info.is_dir()]

    def _inspect_rar(self, path: Path) -> List[ArchiveMemberInfo]:
        return [
            self._member_info(member.path, member.size)
            for member in external.list_members(path, self._bin_unrar, self._bin_7z)
        ]

    def _member_info(self, member_path: str, size: int) -> ArchiveMemberInfo:
        p = Path(member_path)
        ext = p.suffix.lower()
        return ArchiveMemberInfo(
            internal_path=member_path,
            filename=p.name,
            size_bytes=size,
            file_extension=ext,
            is_supported=paths.is_supported(member_path),
            scale=self._detect_scale(member_path),
        )

    @staticmethod
    def _detect_scale(path_str: str) -> Optional[str]:
        m = SCALE_PATTERN.search(path_str)
        return m.group(1).lower() if m else None

    def _extract_images(
        self,
        archive_path: Path,
        image_paths: List[str],
        dest_dir: Path,
        archive_type: str
    ) -> Dict[str, Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        extracted: Dict[str, Path] = {}

        if not image_paths:
            return extracted

        filtered_targets = [
            p for p in image_paths
            if not p.startswith("._") and not "__macosx" in p.lower()
        ]

        log_layer("archive", logging.DEBUG, f"Extracting {len(filtered_targets)} preview images from {archive_path.name}")

        names = self._preview_names(filtered_targets)
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for img_path in filtered_targets:
                    try:
                        out_file = dest_dir / names[img_path]
                        with zf.open(img_path, "r") as src:
                            external.copy_limited(src, out_file, external.member_limit(dest_dir))
                        extracted[img_path] = out_file
                    except Exception as ze:
                        log_layer("archive", logging.WARNING, f"Image extract error on {img_path}: {ze}")
        else:
            extracted = self._extract_images_in_one_pass(archive_path, filtered_targets, dest_dir, names)
            for img_path in filtered_targets:
                if img_path in extracted:
                    continue
                out_file = self._extract_one_image(archive_path, img_path, dest_dir, names[img_path])
                if out_file:
                    extracted[img_path] = out_file

        return extracted

    def _extract_images_in_one_pass(
        self, archive_path: Path, image_paths: list[str], dest_dir: Path, names: dict[str, str],
    ) -> dict[str, Path]:
        scratch = dest_dir / f".extract_{uuid.uuid4().hex[:8]}"
        scratch.mkdir(parents=True, exist_ok=True)
        extracted: dict[str, Path] = {}
        try:
            members = [external.ArchiveMember(path, 0) for path in image_paths]
            unpacked = external.extract_members(archive_path, members, scratch, self._bin_unrar, self._bin_7z)
            for img_path, found in unpacked.items():
                extracted[img_path] = found.replace(dest_dir / names[img_path])
        except (external.ExtractionRefused, OSError) as e:
            log_layer("archive", logging.WARNING, f"One pass over the images in {archive_path.name} failed: {e}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return extracted

    @staticmethod
    def _preview_names(image_paths: list[str]) -> dict[str, str]:
        # Most names stay as they were, so a re-peek doesn't rename every preview. A repeated name gets
        # a hash of its path, or render.jpg from two folders would become one file and one picture.
        clean = {path: re.sub(r"[^\w\-_\.]", "_", Path(path).name) for path in image_paths}
        counts = Counter(name.lower() for name in clean.values())
        return {
            path: name if counts[name.lower()] == 1 else f"{hashlib.sha256(path.encode()).hexdigest()[:8]}_{name}"
            for path, name in clean.items()
        }

    def extract_member(self, archive_path: Path, member_path: str, dest_dir: Path) -> Optional[Path]:
        return self._extract_one_image(archive_path, member_path, dest_dir)

    def _extract_one_image(
        self, archive_path: Path, img_path: str, dest_dir: Path, final_name: str | None = None
    ) -> Path | None:
        scratch = dest_dir / f".extract_{uuid.uuid4().hex[:8]}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            found = external.extract_member(archive_path, img_path, scratch, self._bin_unrar, self._bin_7z)
            final = dest_dir / (final_name or re.sub(r"[^\w\-_\.]", "_", found.name))
            found.replace(final)
            return final
        except (external.ExtractionRefused, OSError, zipfile.BadZipFile) as e:
            log_layer("archive", logging.WARNING, f"No tool could extract {img_path} from {archive_path.name}: {e}")
            return None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _fetch_catalog_fallback_image(self, model_id: int, dest_dir: Path) -> Optional[Path]:
        """Gives an archive with no images the collection's cover.

        It's copied into the file's own folder, because stored preview paths are built from that
        folder and its filename. The copy is local: MMF is only asked when no cover is cached.
        """
        out_file = dest_dir / "catalog_primary.jpg"
        if out_file.exists() and out_file.stat().st_size > 0:
            return out_file
        cover = self._collection_cover(model_id)
        if not cover:
            return None
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cover, out_file)
        except OSError as e:
            # A missing fallback picture must not fail the peek.
            log_layer("archive", logging.WARNING, f"Fallback preview not copied for Model #{model_id}: {e}")
            return None
        return out_file

    def _collection_cover(self, model_id: int) -> Optional[Path]:
        cover = self.previews_root / str(model_id) / "cover.jpg"
        if cover.exists() and cover.stat().st_size > 0:
            return cover

        url = self._catalog_image_url(model_id)
        # External collections store the local path of their render here, which isn't fetchable.
        if not url or not url.startswith(("http://", "https://")):
            return None

        try:
            log_layer("http", logging.DEBUG, f"ArchivePeeker downloading fallback catalog preview: {url}")
            # NOTE: no browser signals here, by the owner's rule. A plain request works (S-15).
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    cover.parent.mkdir(parents=True, exist_ok=True)
                    temp = cover.with_suffix(".jpg.tmp")
                    temp.write_bytes(resp.content)
                    temp.replace(cover)
                    return cover
        except Exception as e:
            log_layer("http", logging.WARNING, f"ArchivePeeker fallback preview failed for Model #{model_id}: {e}")

        return None

    def _catalog_image_url(self, model_id: int) -> Optional[str]:
        url = None
        with self._get_db() as conn:
            row = conn.execute(queries.SELECT_COLLECTIONS_PRIMARY_IMAGE_URL_BY_ID, (model_id,)).fetchone()
            if row and row["primary_image_url"]:
                url = row["primary_image_url"]

        if not url:
            with self._get_db() as conn:
                key = conn.execute(queries.SELECT_COLLECTIONS_EXTERNAL_KEY_BY_ID, (model_id,)).fetchone()
            meta_path = app_config.metadata_dir() / f"model_{key[0] if key else None}.json"
            if meta_path.exists():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                    images = data.get("images") or []
                    if images and isinstance(images, list):
                        target = next((img for img in images if isinstance(img, dict) and img.get("is_primary")), images[0])
                        if isinstance(target, dict):
                            url = (
                                (target.get("large") or {}).get("url")
                                or (target.get("standard") or {}).get("url")
                                or (target.get("original") or {}).get("url")
                                or (target.get("thumbnail") or {}).get("url")
                            )
                except Exception:
                    pass
        return url

    def _handle_document_pack(
        self,
        members: List[ArchiveMemberInfo],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        total_sz = sum(m.size_bytes for m in members)
        sm = DiscoveredSubmodel(
            name="Documentation & Visual Assets",
            relative_dir="[Root Directory]",
            members=members,
            total_bytes=total_sz,
            formats={"pdf", "vtt", "images"}
        )
        if extracted_images:
            best_img = next(iter(extracted_images.values()))
            sm.preview_image_path = f"{self.previews_root.name}/{model_id}/{file_id}/{best_img.name}"
        return [sm]

    def _segment_submodels(
        self,
        printable_members: List[ArchiveMemberInfo],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        discrete_renders = [
            (ip, dp) for ip, dp in extracted_images.items()
            if not any(tag in dp.stem.lower() for tag in ["_cu", "_rear", "_scale", "_side", "_figure size", "_group", "_cover", "storyshot", "concept art", "all", "weapons"])
        ]

        if len(discrete_renders) >= 4 and self._is_comet_lord_style(printable_members, discrete_renders):
            log_layer("archive", logging.DEBUG, f"Model #{model_id}: Using image-driven segmentation (Comet Lord layout).")
            return self._segment_image_driven(printable_members, discrete_renders, extracted_images, model_id, file_id)

        log_layer("archive", logging.DEBUG, f"Model #{model_id}: Using structure-driven directory segmentation.")
        return self._segment_structure_driven(printable_members, extracted_images, model_id, file_id)

    @staticmethod
    def _is_comet_lord_style(members: List[ArchiveMemberInfo], discrete_renders: List[Tuple[str, Path]]) -> bool:
        render_stems = [dp.stem.lower() for _, dp in discrete_renders]
        has_poses = any("p1" in s or "p2" in s for s in render_stems)
        has_weapons = any(any(w in s for w in ["khopesh", "poleaxe", "sickle", "spear", "sword", "bow"]) for s in render_stems)
        return has_poses and has_weapons

    def _segment_image_driven(
        self,
        members: List[ArchiveMemberInfo],
        discrete_renders: List[Tuple[str, Path]],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        submodels_map: Dict[str, DiscoveredSubmodel] = {}

        for internal_path, disk_path in discrete_renders:
            raw_title = disk_path.stem
            clean_title = re.sub(r"^CometLordMinis_IMG_", "", raw_title)
            clean_title = clean_title.replace("_", " ")

            sm = DiscoveredSubmodel(
                name=clean_title,
                relative_dir=str(Path(internal_path).parent),
                preview_image_path=f"{self.previews_root.name}/{model_id}/{file_id}/{disk_path.name}"
            )
            submodels_map[raw_title] = sm

        unassigned_members = []
        for m in members:
            assigned = False
            fn_lower = m.filename.lower()

            if "mummy_base" in fn_lower or (fn_lower.startswith("base") and not any(p in fn_lower for p in ["p1", "p2", "p3", "p4"])):
                unassigned_members.append(m)
                continue

            for render_key, sm in submodels_map.items():
                r_lower = render_key.lower()

                pose_match = re.search(r"p[1-4]", r_lower)
                pose = pose_match.group(0) if pose_match else ""

                weapon_tokens = [w for w in ["khopesh", "poleaxe", "sickle", "spear", "bow", "sword", "axe"] if w in r_lower]

                if pose and pose in fn_lower:
                    if "main" in fn_lower:
                        self._add_member_to_submodel(sm, m)
                        assigned = True
                    elif any(w in fn_lower for w in weapon_tokens):
                        self._add_member_to_submodel(sm, m)
                        assigned = True

            if not assigned:
                unassigned_members.append(m)

        if unassigned_members:
            base_sm = DiscoveredSubmodel(
                name="Mummy Base",
                relative_dir="Files/",
                preview_image_path=None
            )
            for m in unassigned_members:
                self._add_member_to_submodel(base_sm, m)
            submodels_map["__SHARED_BASE__"] = base_sm

        return list(submodels_map.values())

    def _segment_structure_driven(
        self,
        members: List[ArchiveMemberInfo],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        submodels_map: Dict[str, DiscoveredSubmodel] = {}

        for m in members:
            key, display_name, rel_dir = self._derive_submodel_identity(m)

            if key not in submodels_map:
                submodels_map[key] = DiscoveredSubmodel(
                    name=display_name,
                    relative_dir=rel_dir
                )

            self._add_member_to_submodel(submodels_map[key], m)

        return list(submodels_map.values())

    def _derive_submodel_identity(self, member: ArchiveMemberInfo) -> Tuple[str, str, str]:
        raw_parts = [p for p in Path(member.internal_path).parts if p]
        filename = member.filename
        fn_stem = Path(filename).stem

        if len(raw_parts) > 1 and re.match(r"^(\d+\s+)?[A-Za-z0-9_\-\s,\.]+$", raw_parts[0]) and len(raw_parts) >= 3:
            raw_parts = raw_parts[1:]

        cleaned_parts = []
        for p in raw_parts[:-1]:
            p_clean = p.strip()
            p_lower = p_clean.lower()
            if p_lower in FORMAT_ROOT_FOLDERS or SCALE_PATTERN.match(p_lower):
                continue
            cleaned_parts.append(p_clean)

        rel_dir = "/".join(cleaned_parts) if cleaned_parts else "[Root Directory]"

        if "saddleless beast" in member.internal_path.lower() or "(no_saddle)" in filename.lower():
            parent = cleaned_parts[0] if cleaned_parts else "Mount"
            return f"{parent}_Saddleless", f"{parent} (Saddleless Beast)", rel_dir

        if "jurakin_attendant" in filename.lower():
            attendant_id = re.search(r"attendant_[a-z0-9]+", filename, re.IGNORECASE)
            att_name = attendant_id.group(0).replace("_", " ").title() if attendant_id else "Jurakin Attendant"
            return f"Jurakin_{att_name}", att_name, rel_dir

        # Matches a unit name plus one hex-style letter or digit, such as Ghul_A. The word list is creator-specific.
        unit_trooper_match = re.search(r"(?:^|_)((?:Ghul|Banshee|Ant|Beetle|Louse|Mosquito|Knight|Flea|Effigies|Gourdsouls?|Bitlings?|Smilers?|Weblings?)_?[A-F0-9])\b", fn_stem, re.IGNORECASE)
        if unit_trooper_match:
            trooper_token = unit_trooper_match.group(1).replace("_", " ").title()
            return f"Unit_{trooper_token}", trooper_token, rel_dir

        if "bases" in rel_dir.lower() or (filename.lower().startswith("base") and not cleaned_parts):
            return "Bases_Collection", "Bases & Scenics", rel_dir

        primary_folder = cleaned_parts[-1] if cleaned_parts else Path(filename).stem
        clean_name = re.sub(r"^[0-9]+[_\-\s]+", "", primary_folder).replace("_", " ").strip()
        return f"Dir_{primary_folder}", clean_name or "Standard Model", rel_dir

    def _add_member_to_submodel(self, sm: DiscoveredSubmodel, m: ArchiveMemberInfo):
        sm.members.append(m)
        sm.total_bytes += m.size_bytes
        sm.formats.add(m.file_extension.replace(".", "").lower())

        if m.scale:
            sm.scales.add(m.scale)

        if m.is_supported:
            sm.has_presupported = True
        else:
            sm.has_unsupported = True

        if m.file_extension in (".lys", ".ctb", ".chitubox", ".prusa", ".ags", ".agsplate"):
            sm.has_slicer_project = True

    def _correlate_thumbnails(
        self,
        submodels: List[DiscoveredSubmodel],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ):
        if not extracted_images:
            return

        preview_folder_name = self.previews_root.name

        def tokenize(text: str) -> Set[str]:
            s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
            words = re.findall(r"[a-zA-Z0-9]{2,}", s1.lower())
            noise = {"komv", "indpres", "avb", "render", "rectangular", "copy", "copia", "set", "complete", "images"}
            return {w for w in words if w not in noise}

        indexed_images = []
        for internal_path, disk_path in extracted_images.items():
            stem_lower = disk_path.stem.lower()
            is_square = "square" in internal_path.lower() or stem_lower.startswith("0sq")
            is_master = stem_lower in ("all", "0sq all copia", "weapons", "0sq weapons", "cover", "group")
            indexed_images.append({
                "internal_path": internal_path,
                "disk_path": disk_path,
                "stem": stem_lower,
                "is_square": is_square,
                "is_master": is_master,
                "tokens": tokenize(f"{internal_path} {stem_lower}")
            })

        for sm in submodels:
            if sm.preview_image_path:
                continue

            target_tokens = tokenize(f"{sm.name} {sm.relative_dir}")
            clean_target = re.sub(r"[^\w]", "", sm.name).lower()

            best_match: Optional[Path] = None

            # TIER 1: Exact Name Match
            t1_candidates = []
            for img in indexed_images:
                if img["is_master"]:
                    continue
                clean_stem = re.sub(r"[^\w]", "", img["stem"]).lower()
                if clean_target in clean_stem or clean_stem in clean_target:
                    t1_candidates.append(img)

            if t1_candidates:
                t1_candidates.sort(key=lambda x: (x["is_square"], -len(x["stem"])), reverse=True)
                best_match = t1_candidates[0]["disk_path"]

            # TIER 2: Studio Alias & Letter Enumeration Match
            if not best_match:
                letter_match = re.search(r"\b([a-f])\b", sm.name, re.IGNORECASE)
                target_letter = letter_match.group(1).lower() if letter_match else None

                for alias_key, alias_values in STUDIO_ALIASES.items():
                    if any(val in sm.name.lower() for val in alias_values):
                        for img in indexed_images:
                            if img["is_master"]:
                                continue
                            if alias_key in img["stem"]:
                                if target_letter:
                                    img_letter = re.search(r"\b([a-f])\b", img["stem"])
                                    if img_letter and img_letter.group(1).lower() == target_letter:
                                        best_match = img["disk_path"]
                                        break
                                else:
                                    best_match = img["disk_path"]
                                    break
                    if best_match:
                        break

            # TIER 3: Token Intersection Scoring
            if not best_match:
                max_score = 0
                for img in indexed_images:
                    if img["is_master"] and len(submodels) > 1:
                        continue
                    overlap = len(target_tokens.intersection(img["tokens"]))
                    if overlap > 0:
                        score = overlap + (0.5 if img["is_square"] else 0)
                        if score > max_score:
                            max_score = score
                            best_match = img["disk_path"]

            # TIER 4: Group Banner / Master Overview Fallback
            if not best_match:
                for img in indexed_images:
                    if "all" in img["stem"] or "group" in img["stem"] or img["internal_path"] == "[CATALOG_PRIMARY]":
                        best_match = img["disk_path"]
                        break

            if not best_match and extracted_images:
                best_match = next(iter(extracted_images.values()))

            if sm.name.lower() in ("mummy base", "bases & scenics") and not any("base" in img["stem"] for img in indexed_images):
                sm.preview_image_path = None
            elif best_match and best_match.exists():
                sm.preview_image_path = f"{preview_folder_name}/{model_id}/{file_id}/{best_match.name}"

    def _build_catalog(self, conn: sqlite3.Connection, model_id: int | None, subject: str) -> None:
        # A failure here must not undo a good peek. The next build picks these entries up.
        try:
            catalog_levels.build_archive_models(conn, model_id)
        except sqlite3.Error as e:
            log_layer("archive", logging.ERROR, f"Catalog levels not updated for {subject}: {e}")

    def finish_batch(self) -> None:
        """The one build after peeks that skipped their own. Learning from the whole pile also learns better."""
        with self._get_db() as conn:
            self._build_catalog(conn, None, "a batch of peeks")

    def _commit_to_database(
        self,
        file_id: int,
        model_id: int,
        submodels: List[DiscoveredSubmodel],
        all_clean_members: List[ArchiveMemberInfo],
        build_catalog: bool = True,
    ):
        # Updates in place. Stash items and artifacts cascade from these rows, so rewriting them with
        # new IDs used to delete whatever pointed into a re-peeked archive.
        with self._get_db() as conn:
            # Off by default on this connection, which left artifacts behind for deleted entries.
            conn.execute("PRAGMA foreign_keys = ON;")
            with conn:
                old_subs = {
                    (r[1], r[2]): r[0] for r in conn.execute(
                        queries.SELECT_FILE_ARCHIVE_SUBMODELS_BY_FILE_ID, (file_id,)
                    ).fetchall()
                }
                old_members = dict(conn.execute(
                    queries.SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID, (file_id,)
                ).fetchall())

                member_submodel_map: Dict[str, int] = {}
                for sm in submodels:
                    values = (
                        sm.preview_image_path, len(sm.members), sm.total_bytes, sm.has_presupported, sm.has_unsupported,
                        sm.has_slicer_project, json.dumps(sorted(list(sm.formats))), json.dumps(sorted(list(sm.scales))),
                    )
                    submodel_id = old_subs.pop((sm.name, sm.relative_dir), None)
                    if submodel_id is not None:
                        conn.execute(queries.UPDATE_FILE_ARCHIVE_SUBMODELS_PREVIEW_IMAGE_PATH_BY_ID, (*values, submodel_id))
                    else:
                        submodel_id = conn.execute(queries.INSERT_FILE_ARCHIVE_SUBMODELS, (file_id, model_id, sm.name, sm.relative_dir, *values)).lastrowid
                    for m in sm.members:
                        member_submodel_map[m.internal_path] = submodel_id

                for m in all_clean_members:
                    member_id = old_members.pop(m.internal_path, None)
                    row = (member_submodel_map.get(m.internal_path), m.filename, m.size_bytes, m.is_supported, m.file_extension)
                    if member_id is not None:
                        conn.execute(queries.UPDATE_FILE_ARCHIVE_MEMBERS_SUBMODEL_ID_BY_ID, (*row, member_id))
                    else:
                        conn.execute(queries.INSERT_FILE_ARCHIVE_MEMBERS, (*row, file_id, m.internal_path))

                # Whatever is left is really gone from the archive.
                conn.executemany(queries.DELETE_FILE_ARCHIVE_MEMBERS_BY_ID, [(i,) for i in old_members.values()])
                conn.executemany(queries.DELETE_FILE_ARCHIVE_SUBMODELS_BY_ID, [(i,) for i in old_subs.values()])

            with conn:
                conn.execute(queries.UPDATE_MODEL_FILES_PEEKED_AT_BY_FILE_ID, (file_id,))

            if build_catalog:
                self._build_catalog(conn, model_id, f"File #{file_id}")
        log_layer("sql", logging.DEBUG, f"Persisted {len(submodels)} submodels and {len(all_clean_members)} archive members for File #{file_id}")


def repeek_files(db_path: Path, file_ids: set[int]) -> dict[str, int]:
    """Peeks chosen archives again. Rows update in place, so nothing is lost."""
    conn = get_db_connection(db_path)
    marks = ",".join("?" * len(file_ids))
    rows = conn.execute(
        f"SELECT file_id, model_id, vault_path FROM downloaded_files WHERE file_id IN ({marks});", sorted(file_ids)
    ).fetchall() if file_ids else []
    conn.close()
    peeker = ArchivePeeker(db_path)
    summary = {"archives": len(rows), "repaired": 0, "failed": 0}
    for row in rows:
        result = peeker.inspect_and_record(row["model_id"], row["file_id"], Path(row["vault_path"] or ""), build_catalog=False)
        summary["repaired" if result.get("success") else "failed"] += 1
    if summary["repaired"]:
        peeker.finish_batch()
    log_layer("archive", logging.INFO, f"Repeek of {len(file_ids)} archives: {summary}")
    return summary


def repeek_broken(db_path: Path) -> dict[str, int]:
    """Peeks again any archive left with a 0 byte preview."""
    root = app_config.previews_dir().resolve()
    return repeek_files(db_path, {
        int(path.parent.name) for path in root.glob("*/*/*")
        if path.is_file() and path.parent.name.isdigit() and path.stat().st_size == 0
    })
