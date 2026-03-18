#!/usr/bin/env bash
# Script to gracefully stop the metrics exporter daemon using the PID file
# Requires sudo permissions to kill the process

SCRIPT_DIR="$(pwd)"
PIDFILE="${SCRIPT_DIR}/exporter.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "No PID file found at $PIDFILE. Exporter may not be running."
    exit 1
fi

PID=$(cat "$PIDFILE")

# Check if the PID is still running
if ! kill -0 "$PID" 2>/dev/null; then
    echo "PID $PID from $PIDFILE is not a running process. Removing stale PID file."
    rm -f "$PIDFILE"
    exit 0
fi

echo "Stopping exporter daemon with PID $PID"
if sudo kill -TERM "$PID" 2>/dev/null; then
    echo "Exporter stopped successfully."
    # Wait a bit for graceful shutdown
    sleep 2
    # Verify it's dead
    if ! kill -0 "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "PID file cleaned up."
    else
        echo "Process may not have stopped gracefully. Force killing..."
        sudo kill -KILL "$PID"
        rm -f "$PIDFILE"
    fi
else
    echo "Failed to send TERM signal. Attempting force kill..."
    if sudo kill -KILL "$PID" 2>/dev/null; then
        rm -f "$PIDFILE"
        echo "Exporter force-killed."
    else
        echo "Failed to kill exporter. Check permissions or PID validity."
        exit 1
    fi
fi