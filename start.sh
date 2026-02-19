#!/bin/bash
# Start the Risk Management Platform in the background.
# Works on any server (VPS, container, etc.) — no systemd needed.
#
# Usage:
#   bash start.sh       # Start the platform
#   bash stop.sh        # Stop the platform
#   bash status.sh      # Check if running
#
# Logs: tail -f platform.log

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="${WORK_DIR}/platform.pid"
LOGFILE="${WORK_DIR}/platform.log"
PYTHON="${WORK_DIR}/venv/bin/python"

# Check if already running
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Platform is already running (PID $OLD_PID)"
        echo "Use: bash stop.sh to stop it first"
        exit 1
    else
        rm -f "$PIDFILE"
    fi
fi

# Ensure venv exists
if [ ! -f "$PYTHON" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "${WORK_DIR}/venv"
    "${WORK_DIR}/venv/bin/pip" install -r "${WORK_DIR}/requirements.txt" --quiet
fi

cd "$WORK_DIR"

echo "Starting Risk Management Platform..."

# Run in background with nohup
nohup "$PYTHON" -u main.py >> "$LOGFILE" 2>&1 &
PID=$!
echo $PID > "$PIDFILE"

# Wait a moment and verify it started
sleep 2
if kill -0 "$PID" 2>/dev/null; then
    echo ""
    echo "============================================"
    echo "  Platform Started Successfully!"
    echo "============================================"
    echo ""
    echo "  PID:        $PID"
    echo "  Dashboard:  http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'YOUR_SERVER_IP'):5555"
    echo "  Logs:       tail -f $LOGFILE"
    echo ""
    echo "  Stop:       bash ${WORK_DIR}/stop.sh"
    echo "  Status:     bash ${WORK_DIR}/status.sh"
    echo ""
    echo "You can close the terminal — the platform keeps running."
    echo ""
else
    echo "ERROR: Platform failed to start. Check logs:"
    echo "  tail -20 $LOGFILE"
    rm -f "$PIDFILE"
    exit 1
fi
