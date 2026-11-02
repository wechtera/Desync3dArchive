"""The one place the app calls unrar, 7-Zip and Blender.

Archives come from third parties, so everything here treats their contents as hostile: commands
stop switch parsing before member names, every call has a timeout, and bulk extraction checks paths
and sizes both before and after unpacking.
"""

import contextlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO

from desync_archival import settings as app_config

# The services package imports this module, so this uses the logger by name.
logger = logging.getLogger("vault")

LIST_TIMEOUT = 120
MEMBER_TIMEOUT = 300
BULK_TIMEOUT = 3600
MAX_EXTRACTED_BYTES = 64 * 1024**3
MAX_EXTRACTED_FILES = 200_000
# Unpacking runs on the boot disk with the database, which needs room to keep working.
FREE_SPACE_RESERVE = 2 * 1024**3
CAP_POLL_SECONDS = 0.5

TOOLS_DIR = app_config.REPO_ROOT / "tools"
UNRAR_NAMES = ("unrar",)
SEVEN_ZIP_NAMES = ("7zz", "7z", "7za")
GHOSTSCRIPT_NAMES = ("gs", "gswin64c")
SYSTEM_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
BLENDER_APPS = "/Applications/Blender*.app/Contents/MacOS/Blender"


class ExtractionRefused(Exception):
    """An archive broke a safety rule, so nothing from it was kept."""


class ExtractionStopped(ExtractionRefused):
    """The task running this was cancelled, so the next tool mustn't be tried either."""


_worker = threading.local()


def stop_when(check: Callable[[], bool]) -> None:
    """Lets a task stop tools run from its own thread. Peeks during downloads run in other threads."""
    _worker.stop = check


def _stop_requested() -> bool:
    check = getattr(_worker, "stop", None)
    return bool(check and check())


@dataclass(frozen=True)
class ArchiveMember:
    """One file listed in an archive. Size is declared by the archive, so it can lie."""
    path: str
    size: int


def _find(names: tuple[str, ...], setting: str) -> str | None:
    """Looks at a setting first, because it's an explicit choice, then tools/ symlinks, PATH and common folders."""
    candidates = [Path(os.environ[setting])] if os.environ.get(setting) else []
    candidates += [TOOLS_DIR / name for name in names]
    candidates += [Path(found) for name in names if (found := shutil.which(name))]
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


def _bytes_in(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        # A tool renames and removes its files as it works.
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    return total


def _run_capped(cmd: list[str], timeout: float, folder: Path, limit: int) -> subprocess.CompletedProcess:
    """Stops a tool once a folder holds more than `limit` bytes, or when its task is cancelled.

    unrar and 7-Zip have no byte cap of their own, and an archive's declared sizes can lie, so
    the only real limit is watching what lands.

    Raises:
        ExtractionRefused: The tool wrote past the limit. The caller removes what it left.
        ExtractionStopped: The task was cancelled.
    """
    if _stop_requested():
        raise ExtractionStopped(f"{Path(cmd[0]).name} not started, because its task was cancelled")
    process = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=min(CAP_POLL_SECONDS, timeout))
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if _stop_requested():
                    raise ExtractionStopped(f"{Path(cmd[0]).name} stopped because its task was cancelled") from None
                if _bytes_in(folder) > limit:
                    raise ExtractionRefused(f"{Path(cmd[0]).name} unpacked past the limit of {limit} bytes") from None
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def run(
    cmd: list[str], timeout: float, check: bool = False, cap: tuple[Path, int] | None = None,
) -> subprocess.CompletedProcess:
    """Runs a tool with a timeout. Output is decoded leniently, because names can be any bytes.

    `cap` is a folder and a byte limit, for tools that unpack into it.
    """
    if cap:
        result = _run_capped(cmd, timeout, *cap)
        if check:
            result.check_returncode()
        return result
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
    extension: str, unrar: str | None, seven_zip: str | None, archive: Path, internal_path: str, filename: str, dest: Path,
    fuzzy: bool = False,
) -> list[list[str]]:
    """Extraction attempts for one member, in order. Archive tools disagree on separators and paths.

    fuzzy matches by name in any folder, so only use it once the name is known to be unique. `e`
    flattens folders, so same-named files overwrite each other and one of them comes back at random.
    """
    backslashed = internal_path.replace("/", "\\")
    forward = internal_path.replace("\\", "/")
    unrar = unrar if unrar and Path(unrar).exists() else None
    seven_zip = seven_zip if seven_zip and Path(seven_zip).exists() else None
    commands = []

    if extension == ".rar" and unrar:
        commands += [
            unrar_extract_member(unrar, archive, f"*{filename}", dest, recurse=True),
            unrar_extract_member(unrar, archive, filename, dest, recurse=True),
        ] if fuzzy else [
            unrar_extract_member(unrar, archive, forward, dest),
            unrar_extract_member(unrar, archive, backslashed, dest),
        ]
    if seven_zip:
        commands += [
            seven_zip_extract_member(seven_zip, archive, f"*{filename}", dest, recurse=True),
        ] if fuzzy else [
            seven_zip_extract_member(seven_zip, archive, internal_path, dest),
            seven_zip_extract_member(seven_zip, archive, backslashed, dest),
        ]
    if fuzzy and unrar and extension != ".rar":
        commands.append(unrar_extract_member(unrar, archive, f"*{filename}", dest, recurse=True))
    return commands


def _base_name(member_path: str) -> str:
    return PurePosixPath(member_path.replace("\\", "/")).name


def member_limit(folder: Path) -> int:
    """How many bytes may unpack here. Below zero means the disk is already too full."""
    while not folder.exists():
        folder = folder.parent
    return min(MAX_EXTRACTED_BYTES, shutil.disk_usage(folder).free - FREE_SPACE_RESERVE)


def copy_limited(source: IO[bytes], target: Path, limit: int | None = None) -> int:
    """Copies a zip member, counting bytes as they arrive because its declared size can lie.

    Raises:
        ExtractionRefused: The member ran past the size limit. The partial file is removed.
    """
    limit = MAX_EXTRACTED_BYTES if limit is None else limit
    copied = 0
    with open(target, "wb") as output:
        while chunk := source.read(1024 * 1024):
            copied += len(chunk)
            if copied > limit:
                output.close()
                target.unlink(missing_ok=True)
                raise ExtractionRefused(f"{target.name} unpacked past the size limit")
            output.write(chunk)
    return copied


def _zip_entry(archive: zipfile.ZipFile, internal_path: str, archive_name: str) -> str:
    def normalized(name: str) -> str:
        return name.replace("\\", "/").strip("/").lower()

    names = [name for name in archive.namelist() if not name.endswith("/")]
    wanted = normalized(internal_path)
    exact = [name for name in names if normalized(name) == wanted]
    if exact:
        return exact[0]
    same_name = [name for name in names if _base_name(name).lower() == _base_name(internal_path).lower()]
    if len(same_name) == 1:
        return same_name[0]
    if same_name:
        raise ExtractionRefused(f"{archive_name} has {len(same_name)} files named {_base_name(internal_path)}")
    raise FileNotFoundError(f"Could not find entry '{internal_path}' inside zip {archive_name}")


def _name_is_unique(archive: Path, filename: str, unrar: str | None, seven_zip: str | None) -> bool:
    try:
        members = list_members(archive, unrar, seven_zip)
    except (ExtractionRefused, OSError, subprocess.SubprocessError):
        return False
    return sum(_base_name(member.path).lower() == filename.lower() for member in members) == 1


def _extract_with_tools(
    archive: Path, internal_path: str, folder: Path, unrar: str | None, seven_zip: str | None, limit: int,
) -> Path:
    filename = _base_name(internal_path)
    extension = archive.suffix.lower()
    last_error = ""
    for fuzzy in (False, True):
        if fuzzy and not _name_is_unique(archive, filename, unrar, seven_zip):
            break
        for cmd in member_commands(extension, unrar, seven_zip, archive, internal_path, filename, folder, fuzzy):
            try:
                result = run(cmd, MEMBER_TIMEOUT, cap=(folder, limit))
            except ExtractionRefused:
                for path in regular_files(folder):
                    path.unlink(missing_ok=True)
                raise
            except (OSError, subprocess.SubprocessError) as e:
                last_error = str(e)
                continue
            # Homebrew's 7zz has no RAR codec, and fails leaving a 0 byte file, so only bytes count.
            found = [
                path for path in regular_files(folder)
                if path.name.lower() == filename.lower() and path.stat().st_size > 0
            ]
            if found:
                return found[0]
            last_error = (result.stderr or result.stdout or "").strip() or last_error
    raise ExtractionRefused(
        f"Extraction failed for '{filename}' from {archive.name}. "
        f"Diagnostics: {last_error or 'File not found, or the name repeats in other folders'}"
    )


def extract_member(
    archive: Path, internal_path: str, folder: Path, unrar: str | None, seven_zip: str | None, expected_size: int = 0,
) -> Path:
    """Extracts one member into an empty folder the caller owns, and returns the file.

    The exact path is tried first. A match by name alone is allowed only when that name appears
    once in the archive, because the same filename in Supported and Unsupported folders is normal.

    Raises:
        ExtractionRefused: No tool produced the member, its name is ambiguous, or its size is wrong.
        FileNotFoundError: A zip has no entry with that path or name.
    """
    limit = member_limit(folder)
    if limit <= 0 or expected_size > limit:
        raise ExtractionRefused(
            f"Not enough free space to unpack {_base_name(internal_path)} from {archive.name}: "
            f"it needs {expected_size} bytes, and {max(limit, 0)} are usable."
        )
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive, "r") as handle:
            entry = _zip_entry(handle, internal_path, archive.name)
            found = folder / _base_name(entry)
            with handle.open(entry, "r") as source:
                copy_limited(source, found, limit)
    else:
        found = _extract_with_tools(archive, internal_path, folder, unrar, seven_zip, limit)

    size = found.stat().st_size
    if expected_size and size != expected_size:
        raise ExtractionRefused(f"{found.name} from {archive.name} is {size} bytes, and the catalog says {expected_size}")
    return found


def export_paths(internal_paths: list[str]) -> dict[str, str]:
    """Maps members to paths under one export folder, so no two of them land on the same file.

    A unique name stays flat, which is the layout slicers and existing exports expect. A name that
    repeats keeps the folders that tell its copies apart, such as Supported/Head.stl.

    Raises:
        ExtractionRefused: A member path could escape the export folder.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for path in internal_paths:
        if not is_safe_member(path):
            raise ExtractionRefused(f"Unsafe member path: {path!r}")
        groups[_base_name(path).lower()].append(path)

    placed = {}
    for paths in groups.values():
        if len(paths) == 1:
            placed[paths[0]] = _base_name(paths[0])
            continue
        folders = [PurePosixPath(path.replace("\\", "/")).parent.parts for path in paths]
        shared = 0
        while all(len(parts) > shared and parts[shared] == folders[0][shared] for parts in folders):
            shared += 1
        for path, parts in zip(paths, folders):
            placed[path] = "/".join((*parts[shared:], _base_name(path)))
    return placed


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
    limit = member_limit(dest)
    if limit <= 0:
        raise ExtractionRefused(f"Not enough free space to unpack {archive.name}.")
    members = list_members(archive, unrar, seven_zip)
    check_members(members, archive.name)
    if sum(member.size for member in members) > limit:
        raise ExtractionRefused(f"{archive.name} wouldn't fit in the free space left.")

    if extension == ".rar" and unrar and Path(unrar).exists():
        command = [unrar, "x", "-y", "--", str(archive), f"{dest}/"]
    elif seven_zip and Path(seven_zip).exists():
        command = [seven_zip, "x", f"-o{dest}", "-y", "--", str(archive)]
    else:
        raise ExtractionRefused(f"No tool available to unpack {archive.name}")

    try:
        run(command, BULK_TIMEOUT, check=True, cap=(dest, limit))
        _check_extracted(dest, archive.name)
    except (subprocess.SubprocessError, ExtractionRefused):
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        raise
