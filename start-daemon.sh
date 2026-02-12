#!/usr/bin/env bash
#
# Start WhatsApp Bridge + Claude Daemon
# Usage: ./start-daemon.sh
#

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

trap cleanup SIGINT SIGTERM

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

# Kill any existing processes on our ports
echo "[start-daemon] Cleaning up existing processes..."
lsof -ti:8080 2>/dev/null | xargs kill 2>/dev/null || true
lsof -ti:9090 2>/dev/null | xargs kill 2>/dev/null || true
sleep 1

# Start Go bridge
echo "[start-daemon] Starting WhatsApp bridge..."
(cd "$BRIDGE_DIR" && exec ./whatsapp-bridge) &
BRIDGE_PID=$!
echo "[start-daemon] Bridge PID: $BRIDGE_PID"

# Wait for bridge REST API to be ready (check that OUR bridge is responding)
echo "[start-daemon] Waiting for bridge API (port 8080)..."
for i in $(seq 1 30); do
    # Check the bridge process is still alive
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "[start-daemon] ERROR: Bridge process died"
        exit 1
    fi
    if curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/send -X POST -H 'Content-Type: application/json' -d '{"recipient":"test","message":"ping"}' 2>/dev/null | grep -q '500\|200\|400'; then
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

# Verify daemon started
sleep 1
if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    echo "[start-daemon] ERROR: Daemon failed to start"
    cleanup
    exit 1
fi

echo ""
echo "========================================="
echo "  WhatsApp + Claude Daemon Running"
echo "  Bridge PID: $BRIDGE_PID"
echo "  Daemon PID: $DAEMON_PID"
echo "  Press Ctrl+C to stop all"
echo "========================================="
echo ""

# Monitor both processes - if either dies, shut down
while true; do
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "[start-daemon] Bridge process exited"
        cleanup
    fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[start-daemon] Daemon process exited"
        cleanup
    fi
    sleep 2
done
