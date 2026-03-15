#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:1
export USER=root
export HOME=/root
PASSWORD_REQUIRED="${PASSWORD_REQUIRED:-true}"
export AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
export BROWSER_AGENT_PORT="${BROWSER_AGENT_PORT:-8001}"
export DESKTOP_AGENT_PORT="${DESKTOP_AGENT_PORT:-8002}"

echo "Preparing environment..."

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
    echo "Missing API credentials. Set GEMINI_API_KEY or GOOGLE_API_KEY before starting the container." >&2
    exit 1
fi

mkdir -p /root/.vnc
mkdir -p /run/dbus
mkdir -p /tmp/.X11-unix
mkdir -p /tmp/chrome-profile

# -----------------------------
# Configure VNC auth
# -----------------------------
VNC_SECURITY_ARGS=()
if [ "${PASSWORD_REQUIRED}" = "true" ]; then
    if [ ! -f /root/.vnc/passwd ]; then
        echo "Creating VNC password"
        original_umask=$(umask)
        umask 177
        echo "changeme" | tigervncpasswd -f > /root/.vnc/passwd
        umask "$original_umask"
        chmod 600 /root/.vnc/passwd
    fi
    VNC_SECURITY_ARGS=(-rfbauth /root/.vnc/passwd)
else
    VNC_SECURITY_ARGS=(--I-KNOW-THIS-IS-INSECURE -SecurityTypes None)
fi

# -----------------------------
# Create xstartup file
# -----------------------------
sed -i 's/\r$//' /app/xstartup
install -m 0755 /app/xstartup /root/.vnc/xstartup

# -----------------------------
# Start DBUS
# -----------------------------
echo "Starting dbus..."
dbus-daemon --system --fork || true

# -----------------------------
# Cleanup old X locks
# -----------------------------
rm -rf /tmp/.X1-lock || true
rm -rf /tmp/.X11-unix/X1 || true

# -----------------------------
# Start VNC server
# -----------------------------
echo "Starting VNC server..."

vncserver :1 \
    -geometry 1280x800 \
    -depth 24 \
    -localhost no \
    "${VNC_SECURITY_ARGS[@]}"

# -----------------------------
# Start noVNC
# -----------------------------
echo "Starting noVNC..."

websockify \
    --web /usr/share/novnc/ \
    6080 \
    localhost:5901 \
    > /tmp/novnc.log 2>&1 &

sleep 4

# -----------------------------
# Start Chromium
# -----------------------------
echo "Starting Chromium..."

DISPLAY=:1 chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-software-rasterizer \
    --no-first-run \
    --no-default-browser-check \
    --start-maximized \
    --window-size=1280,800 \
    --remote-debugging-address=0.0.0.0 \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-profile \
    about:blank \
    > /tmp/chromium.log 2>&1 &

sleep 3

# -----------------------------
# Start Agents
# -----------------------------
echo "Starting agents..."

cd /app
uv run uisurf_agent run browser_agent \
    --mode a2a \
    --host "${AGENT_HOST}" \
    --port "${BROWSER_AGENT_PORT}" \
    > /tmp/browser-agent.log 2>&1 &

uv run uisurf_agent run desktop_agent \
    --mode a2a \
    --host "${AGENT_HOST}" \
    --port "${DESKTOP_AGENT_PORT}" \
    > /tmp/desktop-agent.log 2>&1 &

# -----------------------------
# Display info
# -----------------------------
echo ""
echo "===================================="
echo "Container ready"
echo ""
echo "noVNC UI:"
echo "http://localhost:6080/vnc.html"
echo ""
echo "Chrome DevTools:"
echo "http://localhost:9222/json/version"
echo ""
echo "Browser A2A server:"
echo "http://localhost:${BROWSER_AGENT_PORT}"
echo ""
echo "Desktop A2A server:"
echo "http://localhost:${DESKTOP_AGENT_PORT}"
echo "===================================="
echo ""

# -----------------------------
# Keep container alive
# -----------------------------
tail -f \
    /tmp/chromium.log \
    /tmp/novnc.log \
    /tmp/browser-agent.log \
    /tmp/desktop-agent.log
