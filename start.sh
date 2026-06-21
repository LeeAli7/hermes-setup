#!/bin/bash
# Hermes Proxy Manager — Linux startup script
# Usage: ./start.sh [start|stop|restart|status|logs]

set -e

HERMES_DIR="$HOME/hermes"
PYTHON="$HOME/hermes-agent/venv/bin/python3"
MANAGER="$HERMES_DIR/proxy_manager.py"
PIDFILE="$HERMES_DIR/proxy_manager.pid"
MANAGER_LOG="$HERMES_DIR/logs/proxy_manager.log"

mkdir -p "$HERMES_DIR/logs"

case "${1:-start}" in
    start)
        echo "Starting Hermes Proxy Manager..."
        if [ -f "$PIDFILE" ]; then
            pid=$(cat "$PIDFILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Already running (PID $pid)"
                exit 0
            fi
            rm -f "$PIDFILE"
        fi
        nohup "$PYTHON" "$MANAGER" > /dev/null 2>&1 &
        echo $! > "$PIDFILE"
        echo "Started PID $!"
        ;;
    stop)
        echo "Stopping..."
        if [ -f "$PIDFILE" ]; then
            pid=$(cat "$PIDFILE")
            kill "$pid" 2>/dev/null || true
            rm -f "$PIDFILE"
        fi
        pkill -f "proxy_manager.py" 2>/dev/null || true
        pkill -f "forwarder.py" 2>/dev/null || true
        echo "Stopped"
        ;;
    restart)
        $0 stop
        sleep 2
        $0 start
        ;;
    status)
        if [ -f "$PIDFILE" ]; then
            pid=$(cat "$PIDFILE")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Running (PID $pid)"
                exit 0
            fi
            echo "PID file exists but process dead"
            rm -f "$PIDFILE"
        else
            echo "Not running"
        fi
        pgrep -f "forwarder.py" > /dev/null && echo "Forwarder: OK" || echo "Forwarder: DEAD"
        pgrep -f "hermes.*gateway" > /dev/null && echo "Hermes: OK" || echo "Hermes: DEAD"
        ;;
    logs)
        tail -f "$MANAGER_LOG"
        ;;
    *)
        echo "Usage: $0 [start|stop|restart|status|logs]"
        exit 1
        ;;
esac
