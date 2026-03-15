#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${SCRIPT_DIR}/a2a-inspector"
REPO_URL="https://github.com/a2aproject/a2a-inspector.git"

if [ ! -d "${REPO_DIR}/.git" ]; then
  echo "Cloning a2a-inspector..."
  git clone "${REPO_URL}" "${REPO_DIR}"
fi

echo "Installing Python dependencies..."
(
  cd "${REPO_DIR}"
  uv sync
)

echo "Installing frontend dependencies..."
(
  cd "${REPO_DIR}/frontend"
  npm install
)

echo "Starting A2A Inspector..."
cd "${REPO_DIR}"
exec bash scripts/run.sh
