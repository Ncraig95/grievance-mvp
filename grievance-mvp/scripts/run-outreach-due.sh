#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

cd "${PROJECT_DIR}"
exec /usr/bin/docker compose --env-file ".env" -f "docker-compose.yml" exec -T api \
  python -m grievance_api.scripts.run_outreach_due
