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
run "npm audit" uv run python scripts/npm_audit_gate.py scraper
run "npm audit e2e" uv run python scripts/npm_audit_gate.py tests/e2e

if [ -f "${DESYNC_DEV_DIR:-$HOME/DESYNC_Data_dev}/old_app/library.db" ]; then
  # Kept on disk because a rare intermittent failure needs its output to diagnose.
  run "playwright" bash -c 'npm --prefix tests/e2e test > playwright-last.log 2>&1'
  grep -E "[0-9]+ (passed|failed)" playwright-last.log
else
  echo "--- playwright skipped: build dev data first with scripts/build_dev_data.py"
fi

echo
if [ "${#failures[@]}" -gt 0 ]; then
  echo "FAILED: ${failures[*]}"
  exit 1
fi
echo "All checks passed"
