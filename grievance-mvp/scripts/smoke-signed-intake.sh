#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f config/config.yaml ]]; then
  echo "Missing config/config.yaml"
  exit 1
fi

if grep -qE 'api_token:\s*"?REPLACE' config/config.yaml; then
  echo "docuseal.api_token is not configured; cannot run signed-intake smoke test"
  exit 2
fi

API_PORT="${API_PORT:-8080}"

docker compose --env-file .env up -d --build api docuseal_db docuseal docuseal_proxy >/dev/null

for i in {1..40}; do
  if curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [[ "$i" -eq 40 ]]; then
    echo "API did not become healthy"
    exit 1
  fi
done

REQUEST_ID="signed-smoke-$(date +%s)"
PAYLOAD="$(cat <<JSON
{
  "request_id": "${REQUEST_ID}",
  "grievance_id": "2026003",
  "grievance_number": "GN-2026003",
  "contract": "AT&T",
  "grievant_firstname": "Signed",
  "grievant_lastname": "Flow",
  "grievant_email": "signed.flow@example.org",
  "narrative": "Signed smoke flow",
  "documents": [
    {"doc_type": "grievance_form", "requires_signature": true}
  ]
}
JSON
)"

RESP="$(curl -fsS -X POST "http://127.0.0.1:${API_PORT}/intake" -H "Content-Type: application/json" -d "$PAYLOAD")"
echo "$RESP"

STATUS="$(printf '%s' "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("documents", [{}])[0].get("status", ""))')"
SIGN_URL="$(printf '%s' "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("documents", [{}])[0].get("signing_link", ""))')"

if [[ "$STATUS" != "sent_for_signature" ]]; then
  echo "Expected sent_for_signature, got: $STATUS"
  exit 1
fi
if [[ -z "$SIGN_URL" ]]; then
  echo "No signing_link returned"
  exit 1
fi

echo "signing_link=$SIGN_URL"
curl -sSI "$SIGN_URL" | head -n 1
