#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE_NAME="${IMAGE_NAME:-uisurf-agent:latest}"
BUILD_CONTEXT="${BUILD_CONTEXT:-.}"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-./docker/Dockerfile}"

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

main() {
  require_cmd docker
  validate_build_inputs

  log "Building Docker image: ${IMAGE_NAME}"
  docker build -f "${DOCKERFILE_PATH}" -t "${IMAGE_NAME}" "${BUILD_CONTEXT}"
  log "Build complete: ${IMAGE_NAME}"
}

main "$@"
