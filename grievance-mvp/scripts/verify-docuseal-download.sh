#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

set -a
# shellcheck disable=SC1091
source .env
set +a

SLUG="${1:-}"
if [[ -z "$SLUG" ]]; then
  SLUG="$(docker compose --env-file .env exec -T docuseal_db \
    psql -U "${DOCUSEAL_DB_USER}" -d "${DOCUSEAL_DB_NAME}" -Atc \
    "SELECT slug FROM submitters WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1;" | tr -d '\r')"
fi

if [[ -z "$SLUG" ]]; then
  echo "No completed submitter slug found; provide slug manually: ./scripts/verify-docuseal-download.sh <slug>"
  exit 2
fi

URL="${DOCUSEAL_PROTOCOL}://${DOCUSEAL_HOST}/s/${SLUG}/download"
echo "Checking URL: $URL"

echo "External:"
curl -sSI "$URL" | head -n 5

echo "Local via proxy (Host override):"
curl -sSI -H "Host: ${DOCUSEAL_HOST}" "http://127.0.0.1:${DOCUSEAL_PORT}/s/${SLUG}/download" | head -n 5
