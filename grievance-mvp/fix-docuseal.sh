#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$HOME/apps/grievance mvp/grievance-mvp"
COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env"

cd "$PROJECT_DIR"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "STOP: docker-compose.yml not found in: $PROJECT_DIR"
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "STOP: .env not found in: $PROJECT_DIR"
  exit 1
fi

upsert_env() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

echo "=== A) Pin DocuSeal image to avoid Ruby 4.0.1 crash (ExitCode 139) ==="
# 2.3.1 release notes mention upgrade to Ruby 4.0.1. Pin to 2.3.0 or earlier.
upsert_env "DOCUSEAL_IMAGE" "docuseal/docuseal:2.3.0"
upsert_env "DOCUSEAL_PORT" "8081"

echo "=== B) Ensure docuseal ports mapping uses internal 80 (default) ==="
# Force docuseal to map host:${DOCUSEAL_PORT} -> container:80
# This replaces any existing docuseal port mapping line that contains DOCUSEAL_PORT.
awk '
  BEGIN { in_doc=0; in_ports=0 }
  /^  docuseal:/ { in_doc=1; in_ports=0; print; next }
  in_doc && /^  [A-Za-z0-9_-]+:/ && $0 !~ /^  docuseal:/ { in_doc=0; in_ports=0; print; next }
  in_doc && /^    ports:/ { in_ports=1; print; next }
  in_doc && in_ports && $0 ~ /DOCUSEAL_PORT/ { print "      - \"127.0.0.1:${DOCUSEAL_PORT}:80\""; next }
  in_doc && in_ports && /^    [A-Za-z0-9_-]+:/ && $0 !~ /^    ports:/ { in_ports=0; print; next }
  { print }
' "$COMPOSE_FILE" > "${COMPOSE_FILE}.tmp"
mv "${COMPOSE_FILE}.tmp" "$COMPOSE_FILE"

echo "=== C) Restart DocuSeal cleanly ==="
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

docker compose stop docuseal >/dev/null 2>&1 || true
docker compose rm -f docuseal >/dev/null 2>&1 || true
docker rm -f docuseal >/dev/null 2>&1 || true

docker pull "$DOCUSEAL_IMAGE"
docker compose up -d docuseal_db docuseal

echo "=== D) Wait for DocuSeal to respond on http://127.0.0.1:${DOCUSEAL_PORT}/ ==="
ok=0
for i in {1..30}; do
  if curl -fsS "http://127.0.0.1:${DOCUSEAL_PORT}/" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done

echo
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

if [ "$ok" -ne 1 ]; then
  echo
  echo "DocuSeal did not come up. Showing last logs:"
  docker logs --tail 200 docuseal || true
  echo
  echo "DocuSeal state:"
  docker inspect docuseal --format "Status={{.State.Status}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}" || true
  exit 1
fi

echo
echo "DocuSeal is up."
curl -sS -D- "http://127.0.0.1:${DOCUSEAL_PORT}/" | head -n 20

echo
echo "What I changed"
echo "- .env: set DOCUSEAL_IMAGE=docuseal/docuseal:2.3.0"
echo "- .env: set DOCUSEAL_PORT=8081"
echo "- docker-compose.yml: forced docuseal port mapping to 127.0.0.1:\${DOCUSEAL_PORT}:80"
