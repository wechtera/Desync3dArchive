#!/usr/bin/env python3
"""Fails on npm advisories at moderate or above, except packages reviewed in npm_audit_allowed.json."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_PATH = REPO_ROOT / "npm_audit_allowed.json"
BLOCKING = {"moderate", "high", "critical"}


def blocked(report: dict, allowed: dict[str, str]) -> list[str]:
    return sorted(
        f"{name} ({details['severity']})"
        for name, details in report.get("vulnerabilities", {}).items()
        if details["severity"] in BLOCKING and name not in allowed
    )


def main() -> int:
    package_dir = sys.argv[1]
    result = subprocess.run(
        ["npm", "--prefix", package_dir, "audit", "--json"],
        check=False, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    try:
        report = json.loads(result.stdout)
    except ValueError:
        print(f"npm audit returned no report: {result.stderr[:200]}")
        return 1
    allowed = json.loads(ALLOWED_PATH.read_text(encoding="utf-8"))
    problems = blocked(report, allowed.get(package_dir, {}))
    counts = report.get("metadata", {}).get("vulnerabilities", {})
    print(f"{package_dir}: {counts.get('total', 0)} advisories, {len(problems)} not reviewed")
    for line in problems:
        print(f"  new: {line}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
