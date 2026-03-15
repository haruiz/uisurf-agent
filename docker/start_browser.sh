#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=:1
CDP_URL="http://127.0.0.1:9222"

pkill -f 'chromium' || true
rm -rf /tmp/chrome-profile
mkdir -p /tmp/chrome-profile

chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-software-rasterizer \
  --no-first-run \
  --no-default-browser-check \
  --start-maximized \
  --window-size=1280,800 \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-profile \
  about:blank > /tmp/chrome.log 2>&1 &

sleep 3

echo "Checking CDP..."
curl -s "$CDP_URL/json/version"
echo
echo "Chrome started."
