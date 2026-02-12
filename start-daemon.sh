#!/usr/bin/env bash
#
# Start WhatsApp Bridge + Claude Daemon
# Usage: ./start-daemon.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/whatsapp-bridge"
DAEMON_SCRIPT="$SCRIPT_DIR/whatsapp-daemon.py"

# PIDs for cleanup
BRIDGE_PID=""
DAEMON_PID=""

cleanup() {
    echo ""
    echo "[start-daemon] Shutting down..."

    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[start-daemon] Stopping daemon (PID $DAEMON_PID)..."
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi

    if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "[start-daemon] Stopping bridge (PID $BRIDGE_PID)..."
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi

    echo "[start-daemon] All processes stopped."
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# Check prerequisites
if [ ! -f "$BRIDGE_DIR/whatsapp-bridge" ]; then
    echo "[start-daemon] Building Go bridge..."
    (cd "$BRIDGE_DIR" && go build -o whatsapp-bridge .)
fi

if ! command -v python3 &>/dev/null; then
    echo "[start-daemon] ERROR: python3 not found"
    exit 1
fi

if ! command -v claude &>/dev/null; then
    echo "[start-daemon] ERROR: claude CLI not found"
    exit 1
fi

# Check if requests module is available
python3 -c "import requests" 2>/dev/null || {
    echo "[start-daemon] Installing requests module..."
    pip3 install requests
}

# Start Go bridge
echo "[start-daemon] Starting WhatsApp bridge..."
(cd "$BRIDGE_DIR" && ./whatsapp-bridge) &
BRIDGE_PID=$!
echo "[start-daemon] Bridge PID: $BRIDGE_PID"

# Wait for bridge REST API to be ready
echo "[start-daemon] Waiting for bridge API (port 8080)..."
for i in $(seq 1 30); do
    if curl -s http://localhost:8080/api/send -X POST -d '{}' >/dev/null 2>&1; then
        echo "[start-daemon] Bridge API is ready."
        break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo "[start-daemon] WARNING: Bridge API not responding after 30s, starting daemon anyway..."
    fi
done

# Start Python daemon
echo "[start-daemon] Starting Claude daemon..."
python3 "$DAEMON_SCRIPT" &
DAEMON_PID=$!
echo "[start-daemon] Daemon PID: $DAEMON_PID"

echo ""
echo "========================================="
echo "  WhatsApp + Claude Daemon Running"
echo "  Bridge PID: $BRIDGE_PID"
echo "  Daemon PID: $DAEMON_PID"
echo "  Press Ctrl+C to stop all"
echo "========================================="
echo ""

# Wait for either process to exit
wait -n "$BRIDGE_PID" "$DAEMON_PID" 2>/dev/null || true
echo "[start-daemon] A process exited, shutting down..."
cleanup
