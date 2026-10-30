#!/usr/bin/env python3
"""Fails on new ruff or pyright findings, without requiring a mass-fix of existing ones.

quality_baseline.json holds the finding count per file. A file may stay at its count or go lower,
and a file that isn't listed must have none. Use --update after removing findings.
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "quality_baseline.json"


def relative(path: str) -> str:
    return str(Path(path).resolve().relative_to(REPO_ROOT))


def ruff_findings() -> Counter:
    result = subprocess.run(
        ["ruff", "check", "src", "tests", "scripts", "--output-format=json", "--exit-zero"],
        check=True, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return Counter(relative(item["filename"]) for item in json.loads(result.stdout))


def pyright_findings() -> Counter:
    result = subprocess.run(
        ["pyright", "src", "scripts", "--outputjson"],
        check=False, capture_output=True, text=True, cwd=REPO_ROOT,
    )
    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
    return Counter(relative(item["file"]) for item in diagnostics if item["severity"] == "error")


def regressions(baseline: dict[str, int], current: Counter) -> list[str]:
    return [
        f"{path}: {count} findings, baseline {baseline.get(path, 0)}"
        for path, count in sorted(current.items())
        if count > baseline.get(path, 0)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Rewrite the baseline")
    args = parser.parse_args()

    current = {"ruff": ruff_findings(), "pyright": pyright_findings()}
    if args.update:
        data = {tool: dict(sorted(counts.items())) for tool, counts in current.items()}
        BASELINE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    failed = False
    for tool, counts in current.items():
        print(f"{tool}: {sum(counts.values())} findings, baseline {sum(baseline[tool].values())}")
        for line in regressions(baseline[tool], counts):
            print(f"  new: {line}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
