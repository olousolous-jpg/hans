#!/bin/bash
# Launcher — starts the combined Hailo inference server (face + objects), then the app.

set -euo pipefail
cd "$(dirname "$0")"

SOCK=/tmp/hailo_scrfd.sock
SERVER_LOG=/tmp/hailo_server.log
CONFIG=config.json

# ── Probe camera sensor modes (before anything else opens the camera) ─────────
echo "[run.sh] Probing camera sensor modes..."
python3.13 scripts/camera_probe.py || echo "[run.sh] Camera probe failed (non-fatal)"

# ── Read server mode ──────────────────────────────────────────────────────────
SERVER_MODE=$(python3.13 -c "
import json, sys
try:
    cfg = json.load(open('$CONFIG'))
    print(cfg.get('hailo_server', {}).get('mode', 'scrfd'))
except Exception:
    print('scrfd')
" 2>/dev/null || echo "scrfd")

case "$SERVER_MODE" in
    personface)
        SERVER_SCRIPT="scripts/hailo_inference_server_personface.py"
        echo "[run.sh] Server mode: personface (yolov5s_personface + ArcFace)"
        ;;
    scrfd|*)
        SERVER_SCRIPT="scripts/hailo_inference_server.py"
        echo "[run.sh] Server mode: scrfd+objects (SCRFD + ArcFace + YOLOv8s)"
        ;;
esac

echo "[run.sh] Using: $SERVER_SCRIPT"

show_hailo_holders() {
    echo "[run.sh] Processes holding /dev/hailo* :"
    fuser /dev/hailo* 2>/dev/null | tr ' ' '\n' | while read pid; do
        [ -n "$pid" ] && echo "  PID $pid  $(cat /proc/$pid/cmdline 2>/dev/null | tr '\0' ' ')"
    done || echo "  (none)"
}

# ── Kill stale servers ────────────────────────────────────────────────────────
for PATTERN in "hailo_inference_server.py" \
               "hailo_inference_server_personface.py" \
               "hailo_inference_server_objects.py"; do
    STALE=$(pgrep -f "$PATTERN" 2>/dev/null || true)
    if [ -n "$STALE" ]; then
        echo "[run.sh] Killing stale $PATTERN: $STALE"
        kill $STALE 2>/dev/null || true; sleep 0.5
        STALE=$(pgrep -f "$PATTERN" 2>/dev/null || true)
        [ -n "$STALE" ] && kill -9 $STALE 2>/dev/null || true; sleep 0.3
    fi
done

# ── Kill hailortd ─────────────────────────────────────────────────────────────
if pgrep -x hailortd > /dev/null 2>&1; then
    echo "[run.sh] Stopping hailortd..."
    sudo systemctl stop hailortd 2>/dev/null || sudo pkill -x hailortd 2>/dev/null || true
    sleep 1
fi

# ── Release device ────────────────────────────────────────────────────────────
HAILO_USERS=$(fuser /dev/hailo* 2>/dev/null | tr -s ' ' '\n' | grep -v '^$' || true)
if [ -n "$HAILO_USERS" ]; then
    echo "[run.sh] Force-releasing Hailo device..."
    fuser -k /dev/hailo* 2>/dev/null || true; sleep 2
fi

REMAINING=$(fuser /dev/hailo* 2>/dev/null | tr -s ' ' '\n' | grep -v '^$' || true)
if [ -n "$REMAINING" ]; then
    echo "[run.sh] ERROR: Cannot free Hailo device"
    show_hailo_holders; exit 1
fi
echo "[run.sh] Hailo device is free"

rm -f "$SOCK"

# ── Start combined inference server ──────────────────────────────────────────
echo "[run.sh] Starting combined inference server..."
python3.13 "$SERVER_SCRIPT" > "$SERVER_LOG" 2>&1 &
HAILO_PID=$!
echo "[run.sh] Server PID: $HAILO_PID"

echo "[run.sh] Waiting for models to load (up to 60s)..."
for i in $(seq 1 120); do
    if [ -S "$SOCK" ]; then
        echo "[run.sh] Server ready (${i} × 0.5s)"
        break
    fi
    sleep 0.5
    if ! kill -0 $HAILO_PID 2>/dev/null; then
        echo "[run.sh] ERROR: Server died. Log:"
        cat "$SERVER_LOG"; exit 1
    fi
done

if [ ! -S "$SOCK" ]; then
    echo "[run.sh] ERROR: Socket not ready. Log:"
    cat "$SERVER_LOG"
    kill $HAILO_PID 2>/dev/null || true; exit 1
fi

# Počkej na gesture socket
for i in $(seq 1 20); do
    if [ -S "/tmp/gesture.sock" ]; then
        echo "[run.sh] Gesture socket ready"
        break
    fi
    sleep 0.3
done
if [ ! -S "/tmp/gesture.sock" ]; then
    echo "[run.sh] WARNING: Gesture socket not ready — gestures may not work"
fi

# ── Start main application ────────────────────────────────────────────────────
# venv je relikt — Hans běží na systémovém python3.13 (venv není na sys.path).
# Podmíněně, ať smazání venv neshodí run.sh (set -e).
[ -f venv/bin/activate ] && source venv/bin/activate || true
export DISPLAY=:0

# Check if running headless
HEADLESS=$(python3.13 -c "
import json
try:
    cfg = json.load(open('$CONFIG'))
    print('1' if cfg.get('display',{}).get('headless', False) else '0')
except:
    print('0')
" 2>/dev/null || echo "0")

if [ "$HEADLESS" = "1" ]; then

    echo "[run.sh] Headless mode — press S for settings, Q to quit"
    python3.13 main.py "$@" &
    MAIN_PID=$!

    # Terminal shortcut loop
    while kill -0 $MAIN_PID 2>/dev/null; do
        echo -n "[headless] command (s=settings, q=quit): "
        read -r CMD 2>/dev/null || break
        case "${CMD,,}" in
            s) echo "[run.sh] Opening settings..."
               python3.13 settings.py &
               ;;
            q) echo "[run.sh] Quitting..."
               kill $MAIN_PID 2>/dev/null
               break
               ;;
            *) echo "Unknown command. s=settings  q=quit" ;;
        esac
    done
    wait $MAIN_PID 2>/dev/null
    EXIT_CODE=$?
else
    python3 web_admin.py &
	WEB_PID=$!
    # ── Dual-eye display daemon (LEVÝ=tvář, PRAVÝ=pozornost) ──────────────────
    pkill -f "scripts.dual_display_daemon" 2>/dev/null || true; sleep 0.3
    python3.13 -m scripts.dual_display_daemon > /tmp/dual_daemon.log 2>&1 &
    DISPLAY_PID=$!
    echo "[run.sh] Dual-display daemon PID: $DISPLAY_PID"
    python3.13 main.py "$@"
    EXIT_CODE=$?
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────
echo "[run.sh] Stopping server (PID $HAILO_PID)..."
kill $WEB_PID 2>/dev/null || true
kill "${DISPLAY_PID:-}" 2>/dev/null || true
kill $HAILO_PID 2>/dev/null || true
rm -f "$SOCK"

exit $EXIT_CODE
