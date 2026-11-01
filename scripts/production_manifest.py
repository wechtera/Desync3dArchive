#!/usr/bin/env python3
"""Records and compares a manifest of production paths.

It stores each entry's relative path, size and modified time. It never opens a file, so it can't
read contents or change anything in production.

    production_manifest.py record --output FILE --root PATH [--root PATH ...]
    production_manifest.py compare --manifest FILE
"""

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

# The Finder writes these on its own, so a change here says nothing about our work.
IGNORED_NAMES = {".DS_Store"}


def scan_root(root: Path) -> dict[str, Any]:
    """Records size and modified time for a tree, without opening any file."""
    if not root.exists():
        return {"exists": False, "entries": {}, "errors": []}
    if not root.is_dir():
        info = root.stat()
        return {"exists": True, "entries": {".": [info.st_size, info.st_mtime_ns]}, "errors": []}

    entries: dict[str, list[int]] = {}
    errors: list[str] = []
    stack = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                children = list(iterator)
        except OSError as e:
            errors.append(f"{current}: {e.strerror}")
            continue

        for child in children:
            if child.name in IGNORED_NAMES:
                continue
            info = child.stat(follow_symlinks=False)
            relative = os.path.relpath(child.path, root)
            if stat.S_ISDIR(info.st_mode):
                entries[relative + "/"] = [0, info.st_mtime_ns]
                stack.append(Path(child.path))
            else:
                entries[relative] = [info.st_size, info.st_mtime_ns]

    return {"exists": True, "entries": entries, "errors": sorted(errors)}


def record_manifest(roots: list[Path]) -> dict[str, Any]:
    return {str(root): scan_root(root) for root in roots}


def compare_manifests(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    differences: list[str] = []
    for root, old in before.items():
        new = after.get(root, {"exists": False, "entries": {}})
        if old["exists"] != new["exists"]:
            differences.append(f"{root}: exists changed from {old['exists']} to {new['exists']}")
        for path in sorted(old["entries"].keys() - new["entries"].keys()):
            differences.append(f"{root}: removed {path}")
        for path in sorted(new["entries"].keys() - old["entries"].keys()):
            differences.append(f"{root}: added {path}")
        for path in sorted(old["entries"].keys() & new["entries"].keys()):
            if old["entries"][path] != new["entries"][path]:
                differences.append(f"{root}: changed {path}")
    return differences


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser("record")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--root", type=Path, action="append", required=True)

    compare = commands.add_parser("compare")
    compare.add_argument("--manifest", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "record":
        manifest = record_manifest([root.resolve() for root in args.root])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        unreadable = 0
        for root, result in manifest.items():
            print(f"{root}: {len(result['entries'])} entries, {len(result['errors'])} unreadable")
            unreadable += len(result["errors"])
        return 1 if unreadable else 0

    before = json.loads(args.manifest.read_text(encoding="utf-8"))
    differences = compare_manifests(before, record_manifest([Path(root) for root in before]))
    for line in differences[:200]:
        print(line)
    print(f"{len(differences)} differences")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
