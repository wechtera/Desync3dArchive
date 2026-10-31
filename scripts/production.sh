#!/usr/bin/env bash
# Runs a command with production settings. The file sets every value, so the dev .env fills no gaps.
# Example: scripts/production.sh python -m desync_archival.dbtool status
set -euo pipefail
ENV_FILE="${DESYNC_PRODUCTION_ENV:-$HOME/.config/desync-archival/production.env}"
if [ ! -f "$ENV_FILE" ]; then
    echo "No production settings at $ENV_FILE" >&2
    exit 2
fi
set -a
. "$ENV_FILE"
set +a
cd "$(dirname "$0")/.."
exec uv run "$@"
