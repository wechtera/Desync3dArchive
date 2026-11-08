#!/usr/bin/env bash
# Starts, stops, restarts or reports on the production server.
# Usage: scripts/server.sh start|stop|restart|status
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${DESYNC_PRODUCTION_ENV:-$HOME/.config/desync-archival/production.env}"
SERVER_MODULE="desync_archival.api.server"
WAIT_SECONDS=30
OPEN_FILES=4096

usage() {
    echo "Usage: scripts/server.sh start|stop|restart|status" >&2
    exit 2
}

[ $# -eq 1 ] || usage
case "$1" in start|stop|restart|status) ;; *) usage ;; esac

if [ ! -f "$ENV_FILE" ]; then
    echo "No production settings at $ENV_FILE" >&2
    exit 2
fi
set -a
. "$ENV_FILE"
set +a
PORT="${DESYNC_PORT:-8080}"
LOG="$DESYNC_DATA_DIR/logs/server.out"

listener() {
    # -b skips stat() on mounts. Without it a busy NAS held one call for 29 seconds. -w drops the warnings -b brings.
    lsof -b -w -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true
}

is_our_server() {
    ps -o command= -p "$1" 2>/dev/null | grep -q "$SERVER_MODULE"
}

address() {
    local scheme=http
    [ -n "${DESYNC_TLS_CERT:-}" ] && scheme=https
    echo "$scheme://${DESYNC_HOST:-127.0.0.1}:$PORT"
}

wait_for_port() { # free or busy
    local i
    for ((i = 0; i < WAIT_SECONDS * 2; i++)); do
        if [ "$1" = free ] && [ -z "$(listener)" ]; then return 0; fi
        if [ "$1" = busy ] && [ -n "$(listener)" ]; then return 0; fi
        perl -e 'select(undef, undef, undef, 0.5)'
    done
    return 1
}

status() {
    local pid
    pid="$(listener)"
    if [ -z "$pid" ]; then
        echo "Stopped"
        return 3
    fi
    if is_our_server "$pid"; then
        echo "Running at $(address), process $pid"
        return 0
    fi
    echo "Port $PORT is held by another program, process $pid" >&2
    return 1
}

start() {
    local pid
    pid="$(listener)"
    if [ -n "$pid" ]; then
        status || return 1
        echo "Already running, nothing to do"
        return 0
    fi
    mkdir -p "$(dirname "$LOG")"
    # The app's own logs rotate, and this one only grows. One old copy stays for whatever a crash left.
    if [ -s "$LOG" ]; then
        mv "$LOG" "$LOG.1"
    fi
    echo "Starting. Output goes to $LOG"
    # macOS starts processes at 256 open files. Reaching that stops the server taking requests (E48).
    ulimit -n "$OPEN_FILES" 2>/dev/null || echo "Could not raise the open file limit, staying at $(ulimit -n)"
    nohup "$ROOT/scripts/production.sh" python -m "$SERVER_MODULE" >> "$LOG" 2>&1 &
    disown
    if wait_for_port busy; then
        status
        return $?
    fi
    echo "The server did not start within $WAIT_SECONDS seconds. Last lines of $LOG:" >&2
    tail -15 "$LOG" >&2
    return 1
}

stop() {
    local pid
    pid="$(listener)"
    if [ -z "$pid" ]; then
        echo "Already stopped"
        return 0
    fi
    if ! is_our_server "$pid"; then
        echo "Not stopping process $pid on port $PORT: it is not this server" >&2
        return 1
    fi
    echo "Stopping process $pid. Workers get time to finish cleanly"
    kill "$pid"
    if wait_for_port free; then
        echo "Stopped"
        return 0
    fi
    echo "Process $pid did not stop within $WAIT_SECONDS seconds. Nothing was forced" >&2
    return 1
}

case "$1" in
    start) start ;;
    stop) stop ;;
    restart) stop && start ;;
    status) status ;;
esac
