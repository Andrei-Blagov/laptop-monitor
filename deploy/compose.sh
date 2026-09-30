#!/usr/bin/env bash
# Version-aware docker compose wrapper for laptop-monitor.
# Reads VERSION from project root and exports LAPTOP_MONITOR_VERSION.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VERSION_FILE="${ROOT}/VERSION"

if [[ ! -f "${VERSION_FILE}" ]]; then
  echo "VERSION file not found at ${VERSION_FILE}" >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"
if [[ -z "${VERSION}" ]]; then
  echo "VERSION file is empty" >&2
  exit 1
fi

export LAPTOP_MONITOR_VERSION="${VERSION}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-laptop-monitor}"

cd "${ROOT}"
exec docker compose \
  -f "${ROOT}/deploy/docker-compose.yml" \
  --project-directory "${ROOT}" \
  "$@"
