#!/usr/bin/env bash
# Runs every quality check. All checks run, so one failure doesn't hide another.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
unset VIRTUAL_ENV

failures=()

run() {
  local name="$1"
  shift
  echo "--- $name"
  if ! "$@"; then
    failures+=("$name")
  fi
}

run "pytest" uv run pytest -q
run "snapshot secrets" uv run pytest -q tests/test_contract_snapshots.py
run "ruff and pyright" uv run python scripts/quality_ratchet.py
run "pip-audit" uv run pip-audit --skip-editable
run "npm audit" npm --prefix scraper audit --audit-level=moderate

echo
if [ "${#failures[@]}" -gt 0 ]; then
  echo "FAILED: ${failures[*]}"
  exit 1
fi
echo "All checks passed"
