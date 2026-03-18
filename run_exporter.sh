#!/usr/bin/env bash
# Simple wrapper to launch the lightweight metrics exporter as a background daemon
# It writes a pid file and redirects output to a log file.

# Resolve the directory of this script (handles symlinks)
SCRIPT_DIR="$(pwd)"
PYTHON=${PYTHON:-python3}
LOGFILE="${SCRIPT_DIR}/exporter.log"
PIDFILE="${SCRIPT_DIR}/exporter.pid"

# If a PID file exists and the process is still running, do not start another instance
if [ -f "$PIDFILE" ]; then
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Exporter already running with PID $(cat "$PIDFILE")"
        exit 0
    else
        # Stale PID file – remove it
        rm -f "$PIDFILE"
    fi
fi

# Start the exporter in the background, detach from terminal
nohup "$PYTHON" "${SCRIPT_DIR}/exporter.py" >"$LOGFILE" 2>&1 &
# Record its PID
echo $! > "$PIDFILE"

echo "Exporter started with PID $(cat "$PIDFILE")"
