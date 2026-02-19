#!/bin/bash
# Check if the Risk Management Platform is running.

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="${WORK_DIR}/platform.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "Platform is NOT running (no PID file)"
    exit 1
fi

PID=$(cat "$PIDFILE")

if kill -0 "$PID" 2>/dev/null; then
    echo "Platform is RUNNING (PID $PID)"
    echo ""
    echo "  Dashboard:  http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'YOUR_SERVER_IP'):5555"
    echo "  Logs:       tail -f ${WORK_DIR}/platform.log"
    echo ""
    exit 0
else
    echo "Platform is NOT running (stale PID file)"
    rm -f "$PIDFILE"
    exit 1
fi
