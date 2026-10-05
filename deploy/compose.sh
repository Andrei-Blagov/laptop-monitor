#!/usr/bin/env bash
# Version-aware docker compose wrapper for laptop-monitor.
# Reads VERSION from project root and exports LAPTOP_MONITOR_VERSION.
#
#   ./deploy/compose.sh build-image   # canonical: builds laptop-monitor:<VERSION>
#   ./deploy/compose.sh <any docker compose args>
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

compose() {
  docker compose \
    -f "${ROOT}/deploy/docker-compose.yml" \
    --project-directory "${ROOT}" \
    "$@"
}

case "${1:-}" in
  build | build-image)
    # The only service with a build section (pipeline) is in profile "manual";
    # a bare `docker compose build` would silently build nothing.
    cmd="$1"
    shift
    compose --profile manual build "$@"
    if [[ "${cmd}" == "build-image" ]]; then
      docker image inspect "laptop-monitor:${VERSION}" --format '{{.Id}}' >/dev/null
      echo "Built image laptop-monitor:${VERSION}"
    fi
    ;;
  *)
    exec docker compose \
      -f "${ROOT}/deploy/docker-compose.yml" \
      --project-directory "${ROOT}" \
      "$@"
    ;;
esac
