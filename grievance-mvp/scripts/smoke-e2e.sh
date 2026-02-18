#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_PORT="${API_PORT:-8080}"
DOCUSEAL_HOST="${DOCUSEAL_HOST:-docuseal.cwa3106.org}"
DOCUSEAL_PROTOCOL="${DOCUSEAL_PROTOCOL:-https}"
APPROVER_EMAIL="${APPROVER_EMAIL:-derek@REPLACE.org}"

log() {
  printf '%s\n' "$*"
}

log "[smoke] validating compose config"
docker compose --env-file .env config >/dev/null

log "[smoke] resetting local sqlite state"
rm -f ./data/grievances.sqlite3

log "[smoke] ensuring services are running"
docker compose --env-file .env up -d --build api docuseal_db docuseal docuseal_proxy >/dev/null

log "[smoke] health check"
for i in {1..40}; do
  if curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/tmp/smoke_health.$$ 2>/dev/null; then
    sed 's/.*/[smoke] healthz -> &/' /tmp/smoke_health.$$
    rm -f /tmp/smoke_health.$$ || true
    break
  fi
  sleep 1
  if [[ "$i" -eq 40 ]]; then
    echo "[smoke] API health check failed after wait window"
    rm -f /tmp/smoke_health.$$ || true
    exit 1
  fi
done

REQUEST_ID="smoke-$(date +%s)"

PAYLOAD="$(cat <<JSON
{
  "request_id": "${REQUEST_ID}",
  "contract": "AT&T",
  "grievant_firstname": "John",
  "grievant_lastname": "Doe",
  "grievant_email": "john.doe@example.org",
  "narrative": "Smoke test narrative",
  "documents": [
    {"doc_type": "grievance_form", "requires_signature": false},
    {"doc_type": "witness_statement", "requires_signature": false}
  ]
}
JSON
)"

log "[smoke] intake request"
INTAKE_RESP="$(curl -fsS -X POST "http://127.0.0.1:${API_PORT}/intake" -H "Content-Type: application/json" -d "$PAYLOAD")"
printf '%s\n' "$INTAKE_RESP" | sed 's/.*/[smoke] intake -> &/'

CASE_ID="$(printf '%s' "$INTAKE_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["case_id"])')"

log "[smoke] case status"
curl -fsS "http://127.0.0.1:${API_PORT}/cases/${CASE_ID}" | sed 's/.*/[smoke] case -> &/'

log "[smoke] approval request"
APPROVAL_PAYLOAD="{\"approver_email\":\"${APPROVER_EMAIL}\",\"approve\":true,\"grievance_number\":\"2026001\",\"notes\":\"smoke approval\"}"
curl -fsS -X POST "http://127.0.0.1:${API_PORT}/cases/${CASE_ID}/approval" -H "Content-Type: application/json" -d "$APPROVAL_PAYLOAD" | sed 's/.*/[smoke] approval -> &/'

log "[smoke] local docuseal proxy check"
DOCUSEAL_LOCAL_OK=0
for i in {1..40}; do
  LOCAL_LINE="$(curl -sSI --connect-timeout 2 --max-time 5 "http://127.0.0.1:${DOCUSEAL_PORT:-8081}/" | head -n 1 || true)"
  if [[ "$LOCAL_LINE" =~ ^HTTP/.*\ (2[0-9]{2}|3[0-9]{2})\  ]]; then
    echo "$LOCAL_LINE" | sed 's/.*/[smoke] docuseal local -> &/'
    DOCUSEAL_LOCAL_OK=1
    break
  fi
  sleep 1
done
if [[ "$DOCUSEAL_LOCAL_OK" -ne 1 ]]; then
  echo "[smoke] docuseal local check failed"
  exit 1
fi

log "[smoke] external https host check (may fail if DNS/tunnel not active)"
if curl -fsSI "${DOCUSEAL_PROTOCOL}://${DOCUSEAL_HOST}/" >/tmp/smoke_docuseal_head.$$ 2>/dev/null; then
  head -n 1 /tmp/smoke_docuseal_head.$$ | sed 's/.*/[smoke] docuseal external -> &/'
else
  log "[smoke] docuseal external check skipped/failed (ensure tunnel + DNS are active)"
fi
rm -f /tmp/smoke_docuseal_head.$$ || true

log "[smoke] complete"
