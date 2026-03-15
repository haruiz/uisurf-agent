#!/usr/bin/env bash
set -euo pipefail

NETWORK="${NETWORK:-default}"
RULE_NAME="${RULE_NAME:-allow-multiple-uisurf-agent-sessions}"
SOURCE_RANGES="${SOURCE_RANGES:-0.0.0.0/0}"
SESSION_PORT_RANGE="${SESSION_PORT_RANGE:-7001-7100}"
TARGET_TAG="${TARGET_TAG:-}"

usage() {
  cat <<'EOF'
Usage:
  ./create-gcp-firewall-rules.sh [options]

Creates a GCP ingress firewall rule for:
  - TCP 80
  - TCP session port range (default: 7001-7100)

Options:
  --network NAME          VPC network name. Default: default
  --rule-name NAME        Firewall rule name. Default: allow-multiple-uiagent-sessions
  --source-ranges CIDRS   Source CIDR list. Default: 0.0.0.0/0
  --session-range RANGE   Session TCP port range. Default: 7001-7100
  --target-tag TAG        Optional VM network tag to scope the rule
  --help                  Show this help message

Examples:
  ./create-gcp-firewall-rules.sh
  ./create-gcp-firewall-rules.sh --network my-vpc --target-tag browser-agent
  ./create-gcp-firewall-rules.sh --session-range 7001-7020 --source-ranges 34.10.20.30/32
EOF
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Error: required command not found: $1" >&2
    exit 1
  }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --network)
      NETWORK="$2"
      shift 2
      ;;
    --rule-name)
      RULE_NAME="$2"
      shift 2
      ;;
    --source-ranges)
      SOURCE_RANGES="$2"
      shift 2
      ;;
    --session-range)
      SESSION_PORT_RANGE="$2"
      shift 2
      ;;
    --target-tag)
      TARGET_TAG="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd gcloud

RULES="tcp:80,tcp:${SESSION_PORT_RANGE}"

CMD=(
  gcloud compute firewall-rules create "${RULE_NAME}"
  --network="${NETWORK}"
  --direction=INGRESS
  --action=ALLOW
  --rules="${RULES}"
  --source-ranges="${SOURCE_RANGES}"
)

if [[ -n "${TARGET_TAG}" ]]; then
  CMD+=(--target-tags="${TARGET_TAG}")
fi

echo "Creating GCP firewall rule..."
echo "  Name          : ${RULE_NAME}"
echo "  Network       : ${NETWORK}"
echo "  Source ranges : ${SOURCE_RANGES}"
echo "  Rules         : ${RULES}"
if [[ -n "${TARGET_TAG}" ]]; then
  echo "  Target tag    : ${TARGET_TAG}"
else
  echo "  Target tag    : <all instances in network>"
fi

"${CMD[@]}"

echo
echo "Firewall rule created."
echo "You can verify it with:"
echo "  gcloud compute firewall-rules describe ${RULE_NAME}"
