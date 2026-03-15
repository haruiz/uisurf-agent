#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE_NAME="${IMAGE_NAME:-uisurf-agent:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-uisurf-agent-test}"
PORT="${PORT:-6080}"
BUILD_CONTEXT="${BUILD_CONTEXT:-.}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-./docker/Dockerfile}"
ENV_FILE="${ENV_FILE:-.env}"
SHM_SIZE="${SHM_SIZE:-2g}"
CONTAINER_PORT="${CONTAINER_PORT:-6080}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://${HOSTNAME_OVERRIDE:-localhost}:${PORT}}"
BROWSER_AGENT_PUBLIC_URL="${BROWSER_AGENT_PUBLIC_URL:-${PUBLIC_BASE_URL}/browser/}"
DESKTOP_AGENT_PUBLIC_URL="${DESKTOP_AGENT_PUBLIC_URL:-${PUBLIC_BASE_URL}/desktop/}"

DOCKER_ENV_ARGS=()
if [[ -f "${ENV_FILE}" ]]; then
  DOCKER_ENV_ARGS+=(--env-file "${ENV_FILE}")
fi
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  DOCKER_ENV_ARGS+=(-e "GEMINI_API_KEY=${GEMINI_API_KEY}")
fi
if [[ -n "${GOOGLE_API_KEY:-}" ]]; then
  DOCKER_ENV_ARGS+=(-e "GOOGLE_API_KEY=${GOOGLE_API_KEY}")
fi
DOCKER_ENV_ARGS+=(-e "BROWSER_AGENT_PUBLIC_URL=${BROWSER_AGENT_PUBLIC_URL}")
DOCKER_ENV_ARGS+=(-e "DESKTOP_AGENT_PUBLIC_URL=${DESKTOP_AGENT_PUBLIC_URL}")

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

error() {
  printf '\n[ERROR] %s\n' "$*" >&2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    error "Required command not found: $1"
    exit 1
  }
}

cleanup_existing_container() {
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
    log "Removing existing container: ${CONTAINER_NAME}"
    docker rm -f "${CONTAINER_NAME}" >/dev/null
  fi
}

validate_build_inputs() {
  [[ -d "${BUILD_CONTEXT}" ]] || {
    error "Build context does not exist: ${BUILD_CONTEXT}"
    exit 1
  }

  [[ -f "${DOCKERFILE_PATH}" ]] || {
    error "Dockerfile not found at: ${DOCKERFILE_PATH}"
    exit 1
  }
}

wait_for_container() {
  local retries=15
  local delay=2

  for ((i=1; i<=retries; i++)); do
    if docker ps --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
      log "Container is running: ${CONTAINER_NAME}"
      return 0
    fi
    sleep "${delay}"
  done

  error "Container failed to start."
  docker logs "${CONTAINER_NAME}" || true
  exit 1
}

show_access_info() {
  local host="${HOSTNAME_OVERRIDE:-localhost}"
  log "Container started successfully"
  echo "Container name : ${CONTAINER_NAME}"
  echo "Image          : ${IMAGE_NAME}"
  echo "noVNC URL      : http://${host}:${PORT}"
  echo "Browser A2A URL: http://${host}:${PORT}/browser"
  echo "Desktop A2A URL: http://${host}:${PORT}/desktop"
  echo
  echo "To view logs:"
  echo "  docker logs -f ${CONTAINER_NAME}"
  echo
  echo "To stop it:"
  echo "  docker rm -f ${CONTAINER_NAME}"
}

main() {
  require_cmd docker
  validate_build_inputs

  log "Building Docker image: ${IMAGE_NAME}"
  docker build -f "${DOCKERFILE_PATH}" -t "${IMAGE_NAME}" "${BUILD_CONTEXT}"

  cleanup_existing_container

  log "Advertised browser URL: ${BROWSER_AGENT_PUBLIC_URL}"
  log "Advertised desktop URL: ${DESKTOP_AGENT_PUBLIC_URL}"

  log "Starting container: ${CONTAINER_NAME}"
  docker run -d \
    --name "${CONTAINER_NAME}" \
    --init \
    --ipc=host \
    --shm-size="${SHM_SIZE}" \
    --cap-add=SYS_ADMIN \
    "${DOCKER_ENV_ARGS[@]}" \
    -p "${PORT}:${CONTAINER_PORT}" \
    "${IMAGE_NAME}" >/dev/null

  wait_for_container
  show_access_info
}

main "$@"
