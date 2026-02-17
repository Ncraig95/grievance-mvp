#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f .env ]]; then
  echo "STOP: .env not found in $SCRIPT_DIR"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

DOCUSEAL_IMAGE="${DOCUSEAL_IMAGE:-docuseal/docuseal:2.3.0}"

echo "Pulling DocuSeal image: $DOCUSEAL_IMAGE"
docker pull "$DOCUSEAL_IMAGE"

echo "Restarting DocuSeal stack (db + app + proxy)"
docker compose --env-file .env up -d --force-recreate docuseal_db docuseal docuseal_proxy

echo "Waiting for local DocuSeal proxy"
for i in {1..30}; do
  if curl -fsS "http://127.0.0.1:${DOCUSEAL_PORT:-8081}/" >/dev/null 2>&1; then
    echo "DocuSeal proxy is responding"
    exit 0
  fi
  sleep 1
done

echo "DocuSeal proxy failed to respond. Recent logs:"
docker compose --env-file .env logs --tail=200 docuseal docuseal_proxy || true
exit 1
