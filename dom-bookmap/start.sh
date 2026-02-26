#!/bin/bash
# Start DOM & Bookmap Analyzer
# Uses the parent Risk-Management/.env for Dhan credentials

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/.dom-bookmap.pid"

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "DOM Bookmap already running (PID $PID)"
        exit 1
    fi
    rm -f "$PID_FILE"
fi

# Use parent venv if it exists, otherwise local
if [ -d "$SCRIPT_DIR/../venv" ]; then
    VENV="$SCRIPT_DIR/../venv"
elif [ -d "$SCRIPT_DIR/venv" ]; then
    VENV="$SCRIPT_DIR/venv"
else
    echo "No virtualenv found. Create one with: python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

source "$VENV/bin/activate"

echo "Starting DOM & Bookmap Analyzer..."
nohup python3 app.py > "$SCRIPT_DIR/dom-bookmap.log" 2>&1 &
echo $! > "$PID_FILE"
echo "Started (PID $(cat "$PID_FILE")). Log: $SCRIPT_DIR/dom-bookmap.log"

# Quick health check
sleep 2
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    PORT=$(grep DOM_PORT "$SCRIPT_DIR/../.env" 2>/dev/null | cut -d= -f2)
    PORT=${PORT:-5556}
    echo "Running on port $PORT"
else
    echo "FAILED to start — check dom-bookmap.log"
    rm -f "$PID_FILE"
    exit 1
fi
