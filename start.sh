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
    if [ -f "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13" ]; then
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -m venv "${WORK_DIR}/venv"
    else
        python3 -m venv "${WORK_DIR}/venv"
    fi
fi

# Always sync dependencies (handles new/updated packages after git pull)
REQ_FILE="${WORK_DIR}/requirements.txt"
REQ_HASH_FILE="${WORK_DIR}/venv/.requirements.hash"
CURRENT_HASH=$(md5sum "$REQ_FILE" 2>/dev/null | awk '{print $1}')
CACHED_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null)
if [ "$CURRENT_HASH" != "$CACHED_HASH" ]; then
    echo "Installing/updating dependencies..."
    "${WORK_DIR}/venv/bin/pip" install -r "$REQ_FILE" --quiet
    echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
fi

cd "$WORK_DIR"

echo "Starting Risk Management Platform..."

# Run in background with nohup
nohup "$PYTHON" -u main.py > /dev/null 2>&1 &
PID=$!
echo $PID > "$PIDFILE"

# Wait for startup (token refresh + instrument loading can take ~15s)
echo "Waiting for platform to initialize (token refresh + instrument loading)..."
STARTED=false
for i in $(seq 1 30); do
    if ! kill -0 "$PID" 2>/dev/null; then
        # Process died during startup
        break
    fi
    # Check if dashboard is listening
    if command -v curl >/dev/null 2>&1; then
        if curl -s -o /dev/null -w '' --max-time 1 http://127.0.0.1:${DASHBOARD_PORT:-5555}/ 2>/dev/null; then
            STARTED=true
            break
        fi
    elif command -v wget >/dev/null 2>&1; then
        if wget -q -O /dev/null --timeout=1 http://127.0.0.1:${DASHBOARD_PORT:-5555}/ 2>/dev/null; then
            STARTED=true
            break
        fi
    else
        # No curl/wget, just wait and check if process is alive
        if [ "$i" -ge 15 ]; then
            STARTED=true
            break
        fi
    fi
    sleep 1
done

if [ "$STARTED" = true ] && kill -0 "$PID" 2>/dev/null; then
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
    echo "ERROR: Platform failed to start."
    echo ""
    echo "--- Last 20 lines of log ---"
    tail -20 "$LOGFILE"
    echo "----------------------------"
    rm -f "$PIDFILE"
    exit 1
fi
