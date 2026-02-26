#!/bin/bash
# Stop DOM & Bookmap Analyzer

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/.dom-bookmap.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "Not running (no PID file)"
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping DOM Bookmap (PID $PID)..."
    kill "$PID"
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null
    fi
    echo "Stopped."
else
    echo "Process $PID not running."
fi

rm -f "$PID_FILE"
