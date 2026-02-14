#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

extract_token() {
  local line value
  line="$(grep -E '^[[:space:]]*CLOUDFLARE_TUNNEL_TOKEN=' "$ENV_FILE" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#*=}"

  # trim surrounding whitespace
  value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"

  # support optional single/double quotes
  if [[ "$value" =~ ^\".*\"$ ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" =~ ^\'.*\'$ ]]; then
    value="${value:1:${#value}-2}"
  fi

  printf '%s' "$value"
}

log "Preflight: checking Docker tooling"
require_cmd docker

docker compose version >/dev/null 2>&1 || fail "docker compose plugin is unavailable"
docker info >/dev/null 2>&1 || fail "Cannot talk to Docker daemon. Ensure your user can run Docker."

[[ -f "$ENV_FILE" ]] || fail "Missing .env file at $ENV_FILE"

TOKEN_VALUE="$(extract_token || true)"
if [[ -z "$TOKEN_VALUE" || "$TOKEN_VALUE" == "PASTE_TOKEN_HERE" ]]; then
  fail "Set CLOUDFLARE_TUNNEL_TOKEN in $SCRIPT_DIR/.env (replace placeholder) and re-run."
fi

log "Preflight: validating compose file"
docker compose config >/dev/null
if ! docker compose config --services | grep -Fxq "cloudflared"; then
  fail "Compose service 'cloudflared' was not found. Check docker-compose.yml."
fi

log "Starting cloudflared service"
docker compose up -d cloudflared >/dev/null

sleep 2
if ! docker ps --format '{{.Names}}' | grep -Fxq 'cloudflared'; then
  log "Recent cloudflared logs:"
  docker logs --tail 120 cloudflared 2>&1 || true
  fail "cloudflared container is not running"
fi

TMP_LOG="$(mktemp)"
trap 'rm -f "$TMP_LOG"' EXIT

docker logs --tail 200 cloudflared >"$TMP_LOG" 2>&1 || true
# Wait briefly for connection events after startup/restart.
timeout 20s docker logs -f --since 2s cloudflared >>"$TMP_LOG" 2>&1 || true

TUNNEL_STATUS="starting"
if grep -Eqi '(registered tunnel connection|connection .* registered|initial protocol h2mux|connected to edge)' "$TMP_LOG"; then
  TUNNEL_STATUS="connected"
elif grep -Eqi '(serving tunnel|starting tunnel)' "$TMP_LOG"; then
  TUNNEL_STATUS="starting"
fi

FAIL_REASON=""
COMMON_FAILURE_PATTERN='(invalid token|authentication failed|unauthorized|token is not valid|failed to fetch tunnel|error parsing tunnel token|connection refused|x509|lookup .* no such host|cannot resolve|serve tunnel error|failed to serve tunnel connection|control stream encountered a failure)'
if grep -Eqi "$COMMON_FAILURE_PATTERN" "$TMP_LOG"; then
  FAIL_REASON="cloudflared logs show an actionable tunnel/connectivity error"
fi

PUBLIC_HOSTNAME=""
HOST_CANDIDATE="$(grep -Eio 'https://[a-z0-9.-]+' "$TMP_LOG" | grep -Evi '(github.com|cloudflare.com|quic-go|localhost|127.0.0.1|docuseal)' | head -n 1 || true)"
if [[ -n "$HOST_CANDIDATE" ]]; then
  PUBLIC_HOSTNAME="$HOST_CANDIDATE"
else
  HOST_CANDIDATE="$(grep -Eio '([a-z0-9-]+\.)+[a-z]{2,}' "$TMP_LOG" | grep -Evi '(github.com|cloudflare|quic-go|localhost|docuseal)' | head -n 1 || true)"
  if [[ -n "$HOST_CANDIDATE" ]]; then
    PUBLIC_HOSTNAME="https://$HOST_CANDIDATE"
  fi
fi

log ""
log "Final status summary"
log "Containers:"
docker compose ps
log "Tunnel status: $TUNNEL_STATUS"
if [[ -n "$PUBLIC_HOSTNAME" ]]; then
  log "Public hostname: $PUBLIC_HOSTNAME"
else
  log "Public hostname: not found in logs (check Cloudflare Zero Trust Public Hostname settings)"
fi

if [[ -n "$FAIL_REASON" ]]; then
  log "Recent cloudflared logs:"
  tail -n 120 "$TMP_LOG"
  fail "$FAIL_REASON"
fi

if [[ "$TUNNEL_STATUS" != "connected" ]]; then
  fail "Tunnel did not reach a confirmed connected state within timeout. Re-run this script and inspect cloudflared logs."
fi

exit 0
