"""The one place the app calls unrar, 7-Zip and Blender.

Archives come from third parties, so everything here treats their contents as hostile: commands
stop switch parsing before member names, every call has a timeout, and bulk extraction checks paths
and sizes both before and after unpacking.
"""

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from desync_archival import settings as app_config

# The services package imports this module, so this uses the logger by name.
logger = logging.getLogger("vault")

LIST_TIMEOUT = 120
MEMBER_TIMEOUT = 300
BULK_TIMEOUT = 3600
MAX_EXTRACTED_BYTES = 64 * 1024**3
MAX_EXTRACTED_FILES = 200_000

TOOLS_DIR = app_config.REPO_ROOT / "tools"
UNRAR_NAMES = ("unrar",)
SEVEN_ZIP_NAMES = ("7zz", "7z", "7za")
GHOSTSCRIPT_NAMES = ("gs", "gswin64c")
SYSTEM_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
BLENDER_APPS = "/Applications/Blender*.app/Contents/MacOS/Blender"


class ExtractionRefused(Exception):
    """An archive broke a safety rule, so nothing from it was kept."""


@dataclass(frozen=True)
class ArchiveMember:
    """One file listed in an archive. Size is declared by the archive, so it can lie."""
    path: str
    size: int


def _find(names: tuple[str, ...], setting: str) -> str | None:
    """Looks in tools/ symlinks, then PATH, then a setting, then common install folders."""
    candidates = [TOOLS_DIR / name for name in names]
    candidates += [Path(found) for name in names if (found := shutil.which(name))]
    if os.environ.get(setting):
        candidates.append(Path(os.environ[setting]))
    candidates += [Path(folder) / name for folder in SYSTEM_DIRS for name in names]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_unrar() -> str | None:
    return _find(UNRAR_NAMES, "DESYNC_UNRAR_BIN")


def find_7zip() -> str | None:
    return _find(SEVEN_ZIP_NAMES, "DESYNC_7ZIP_BIN")


def find_ghostscript() -> str | None:
    return _find(GHOSTSCRIPT_NAMES, "DESYNC_GHOSTSCRIPT_BIN")


def pdf_page_command(source: Path, target: Path) -> list[str] | None:
    """Draws the first page. -dSAFER matters here, because these PDFs come from anywhere."""
    found = find_ghostscript()
    if not found:
        return None
    return [
        found, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-dFirstPage=1", "-dLastPage=1", "-sDEVICE=jpeg",
        "-dJPEGQ=80", "-r50", f"-sOutputFile={target}", str(source),
    ]


def find_blender() -> str | None:
    found = _find(("blender",), "DESYNC_BLENDER_BIN")
    if found:
        return str(Path(found).resolve())
    for app in sorted(Path("/").glob(BLENDER_APPS.lstrip("/")), reverse=True):
        if os.access(app, os.X_OK):
            return str(app.resolve())
    return None


def run(cmd: list[str], timeout: float, check: bool = False) -> subprocess.CompletedProcess:
    """Runs a tool with a timeout. Output is decoded leniently, because names can be any bytes."""
    return subprocess.run(
        cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=check,
    )


def unrar_extract_member(unrar: str, archive: Path, member: str, dest: Path, recurse: bool = False) -> list[str]:
    """Builds an unrar command. -- stops a hostile member name reading as a switch."""
    flags = ["-y", "-r"] if recurse else ["-y"]
    return [unrar, "e", *flags, "--", str(archive), member, f"{dest}/"]


def seven_zip_extract_member(seven_zip: str, archive: Path, member: str, dest: Path, recurse: bool = False) -> list[str]:
    """Builds a 7-Zip command. -- stops a hostile member name reading as a switch."""
    flags = ["-r", "-y"] if recurse else ["-y"]
    return [seven_zip, "e", str(archive), f"-o{dest}", *flags, "--", member]


def member_commands(
    extension: str, unrar: str | None, seven_zip: str | None, archive: Path, internal_path: str, filename: str, dest: Path
) -> list[list[str]]:
    """Extraction attempts for one member, in order. Archive tools disagree on separators and paths."""
    backslashed = internal_path.replace("/", "\\")
    forward = internal_path.replace("\\", "/")
    unrar = unrar if unrar and Path(unrar).exists() else None
    seven_zip = seven_zip if seven_zip and Path(seven_zip).exists() else None
    commands = []

    if extension == ".rar" and unrar:
        commands += [
            # FIXME(R-03): a wildcard matches same-named files in other folders, and `e` flattens them. Try the exact path first.
            unrar_extract_member(unrar, archive, f"*{filename}", dest, recurse=True),
            unrar_extract_member(unrar, archive, filename, dest, recurse=True),
            unrar_extract_member(unrar, archive, backslashed, dest),
            unrar_extract_member(unrar, archive, forward, dest),
        ]
    if seven_zip:
        commands += [
            seven_zip_extract_member(seven_zip, archive, f"*{filename}", dest, recurse=True),
            seven_zip_extract_member(seven_zip, archive, internal_path, dest),
            seven_zip_extract_member(seven_zip, archive, backslashed, dest),
        ]
    if unrar and extension != ".rar":
        commands.append(unrar_extract_member(unrar, archive, f"*{filename}", dest, recurse=True))
    return commands


def regular_files(folder: Path) -> list[Path]:
    """Files in a folder, skipping symlinks, which an archive could aim outside the folder."""
    return [path for path in folder.rglob("*") if path.is_file() and not path.is_symlink()]


def is_safe_member(name: str) -> bool:
    """Rejects absolute paths, drive letters and parent references under both path styles."""
    if not name or "\0" in name:
        return False
    for path in (PurePosixPath(name), PureWindowsPath(name)):
        if path.is_absolute() or path.drive or path.root or ".." in path.parts:
            return False
    return True


def list_members(archive: Path, unrar: str | None, seven_zip: str | None) -> list[ArchiveMember]:
    """Lists file members with sizes, without extracting anything."""
    if seven_zip and Path(seven_zip).exists():
        result = run([seven_zip, "l", "-slt", "--", str(archive)], LIST_TIMEOUT)
        if result.returncode == 0:
            return _parse_7zip_listing(result.stdout)
    if unrar and Path(unrar).exists():
        result = run([unrar, "lt", "--", str(archive)], LIST_TIMEOUT)
        if result.returncode == 0:
            return _parse_unrar_listing(result.stdout)
    raise ExtractionRefused(f"Could not list the contents of {archive.name}")


def _parse_7zip_listing(output: str) -> list[ArchiveMember]:
    members = []
    # The first block describes the archive itself.
    for block in output.split("----------\n", 1)[-1].split("\n\n"):
        path = re.search(r"^Path = (.+)$", block, re.MULTILINE)
        if not path or re.search(r"^Folder = \+$", block, re.MULTILINE):
            continue
        size = re.search(r"^Size = (\d+)$", block, re.MULTILINE)
        members.append(ArchiveMember(path.group(1).strip(), int(size.group(1)) if size else 0))
    return members


def _parse_unrar_listing(output: str) -> list[ArchiveMember]:
    members = []
    for block in re.split(r"\n\s*\n", output):
        path = re.search(r"^\s*Name: (.+)$", block, re.MULTILINE)
        kind = re.search(r"^\s*Type: (.+)$", block, re.MULTILINE)
        if not path or (kind and "File" not in kind.group(1)):
            continue
        size = re.search(r"^\s*Size: (\d+)$", block, re.MULTILINE)
        members.append(ArchiveMember(path.group(1).strip(), int(size.group(1)) if size else 0))
    return members


def check_members(members: list[ArchiveMember], archive_name: str) -> None:
    """Applies the path, count and size limits before anything is unpacked.

    Raises:
        ExtractionRefused: The archive broke a safety rule or no tool could read it.
    """
    unsafe = [member.path for member in members if not is_safe_member(member.path)]
    if unsafe:
        raise ExtractionRefused(f"{archive_name} has unsafe member paths: {unsafe[:3]}")
    if len(members) > MAX_EXTRACTED_FILES:
        raise ExtractionRefused(f"{archive_name} has {len(members)} files, over the limit")
    # Declared sizes can lie. _check_extracted re-measures after unpacking.
    if sum(member.size for member in members) > MAX_EXTRACTED_BYTES:
        raise ExtractionRefused(f"{archive_name} would unpack past the size limit")


def _check_extracted(dest: Path, archive_name: str) -> None:
    """Checks what actually landed, because declared sizes and paths can lie."""
    root = dest.resolve()
    total = 0
    count = 0
    for path in dest.rglob("*"):
        if path.is_symlink():
            path.unlink()
            logger.warning(f"Removed a symlink unpacked from {archive_name}")
            continue
        if not path.resolve().is_relative_to(root):
            raise ExtractionRefused(f"{archive_name} wrote outside its folder")
        if path.is_file():
            count += 1
            total += path.stat().st_size
    if count > MAX_EXTRACTED_FILES or total > MAX_EXTRACTED_BYTES:
        raise ExtractionRefused(f"{archive_name} unpacked past the limits")


def extract_all(extension: str, archive: Path, dest: Path, unrar: str | None, seven_zip: str | None) -> None:
    """Unpacks a whole archive into dest, or leaves dest empty and raises ExtractionRefused."""
    check_members(list_members(archive, unrar, seven_zip), archive.name)

    if extension == ".rar" and unrar and Path(unrar).exists():
        command = [unrar, "x", "-y", "--", str(archive), f"{dest}/"]
    elif seven_zip and Path(seven_zip).exists():
        command = [seven_zip, "x", f"-o{dest}", "-y", "--", str(archive)]
    else:
        raise ExtractionRefused(f"No tool available to unpack {archive.name}")

    try:
        run(command, BULK_TIMEOUT, check=True)
        _check_extracted(dest, archive.name)
    except (subprocess.SubprocessError, ExtractionRefused):
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        raise
