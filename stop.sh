#!/bin/bash
# Stop the Risk Management Platform.

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="${WORK_DIR}/platform.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "Platform is not running (no PID file found)"
    exit 0
fi

PID=$(cat "$PIDFILE")

if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping platform (PID $PID)..."
    kill "$PID"
    sleep 2
    # Force kill if still running
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
        sleep 1
    fi
    rm -f "$PIDFILE"
    echo "Platform stopped."
else
    echo "Platform was not running (stale PID file). Cleaning up."
    rm -f "$PIDFILE"
fi
