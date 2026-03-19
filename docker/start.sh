#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:1
export USER=root
export HOME=/root
PASSWORD_REQUIRED="${PASSWORD_REQUIRED:-false}"
export AGENT_HOST="${AGENT_HOST:-0.0.0.0}"
export BROWSER_AGENT_PORT="${BROWSER_AGENT_PORT:-8001}"
export DESKTOP_AGENT_PORT="${DESKTOP_AGENT_PORT:-8002}"
export BROWSER_AGENT_PUBLIC_URL="${BROWSER_AGENT_PUBLIC_URL:-http://localhost:6080/browser/}"
export DESKTOP_AGENT_PUBLIC_URL="${DESKTOP_AGENT_PUBLIC_URL:-http://localhost:6080/desktop/}"

echo "Preparing environment..."

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
    echo "Missing API credentials. Set GEMINI_API_KEY or GOOGLE_API_KEY before starting the container." >&2
    exit 1
fi

mkdir -p /root/.vnc
mkdir -p /run/dbus
mkdir -p /tmp/.X11-unix
mkdir -p /tmp/chrome-profile
mkdir -p /var/log/nginx

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
    6081 \
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
# Start reverse proxy
# -----------------------------
echo "Starting reverse proxy..."

cat >/etc/nginx/nginx.conf <<EOF
worker_processes 1;

events {
    worker_connections 1024;
}

http {
    map \$http_upgrade \$connection_upgrade {
        default upgrade;
        '' close;
    }

    map \$request_method \$browser_entry_upstream {
        default http://127.0.0.1:${BROWSER_AGENT_PORT}/;
        GET http://127.0.0.1:${BROWSER_AGENT_PORT}/.well-known/agent-card.json;
    }

    map \$request_method \$desktop_entry_upstream {
        default http://127.0.0.1:${DESKTOP_AGENT_PORT}/;
        GET http://127.0.0.1:${DESKTOP_AGENT_PORT}/.well-known/agent-card.json;
    }

    server {
        listen 6080;
        server_name _;

        location = / {
            return 302 /vnc.html?autoconnect=1&resize=remote&path=websockify;
        }

        location = /browser {
            proxy_pass \$browser_entry_upstream;
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        location = /desktop {
            proxy_pass \$desktop_entry_upstream;
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        location = /browser/ {
            proxy_pass \$browser_entry_upstream;
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        location = /desktop/ {
            proxy_pass \$desktop_entry_upstream;
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        location /browser/ {
            rewrite ^/browser/?(.*)$ /\$1 break;
            proxy_pass http://127.0.0.1:${BROWSER_AGENT_PORT};
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        location /desktop/ {
            rewrite ^/desktop/?(.*)$ /\$1 break;
            proxy_pass http://127.0.0.1:${DESKTOP_AGENT_PORT};
            proxy_http_version 1.1;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            proxy_set_header Host \$host;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        location /websockify {
            proxy_pass http://127.0.0.1:6081;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_set_header Host \$host;
        }

        location / {
            proxy_pass http://127.0.0.1:6081;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_set_header Host \$host;
        }
    }
}
EOF

nginx -g 'daemon off;' > /tmp/nginx.log 2>&1 &

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
echo "Browser A2A server:"
echo "http://localhost:6080/browser"
echo ""
echo "Desktop A2A server:"
echo "http://localhost:6080/desktop"
echo ""
echo "Chrome DevTools:"
echo "http://localhost:9222/json/version"
echo "===================================="
echo ""

# -----------------------------
# Keep container alive
# -----------------------------
tail -f \
    /tmp/chromium.log \
    /tmp/nginx.log \
    /tmp/novnc.log \
    /tmp/browser-agent.log \
    /tmp/desktop-agent.log
