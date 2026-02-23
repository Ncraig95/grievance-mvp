#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

STATE_DIR="${WATCHDOG_STATE_DIR:-${PROJECT_DIR}/data/watchdog}"
DISABLE_FILE="${WATCHDOG_DISABLE_FILE:-${STATE_DIR}/disable_auto_restart}"
FAIL_FILE="${STATE_DIR}/failure_count"
LAST_RESTART_FILE="${STATE_DIR}/last_restart_epoch"

FAIL_THRESHOLD="${WATCHDOG_FAILURE_THRESHOLD:-3}"
COOLDOWN_SECONDS="${WATCHDOG_RESTART_COOLDOWN_SECONDS:-600}"
CURL_TIMEOUT_SECONDS="${WATCHDOG_CURL_TIMEOUT_SECONDS:-8}"
POST_RESTART_RETRIES="${WATCHDOG_POST_RESTART_HEALTH_RETRIES:-12}"
POST_RESTART_DELAY_SECONDS="${WATCHDOG_POST_RESTART_HEALTH_DELAY_SECONDS:-5}"
ALERT_EMAIL="${WATCHDOG_ALERT_EMAIL:-true}"
ALERT_POPUP="${WATCHDOG_ALERT_POPUP:-false}"

API_PORT_DEFAULT="${API_PORT:-8080}"
HEALTH_URL="${WATCHDOG_HEALTH_URL:-http://127.0.0.1:${API_PORT_DEFAULT}/healthz}"

mkdir -p "${STATE_DIR}"

log() {
  local msg="$1"
  printf '[watchdog] %s\n' "${msg}"
  logger -t grievance-watchdog -- "${msg}" >/dev/null 2>&1 || true
}

normalize_bool() {
  local v="${1:-}"
  v="$(printf '%s' "${v}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${v}" == "1" || "${v}" == "true" || "${v}" == "yes" || "${v}" == "on" ]]; then
    printf 'true'
    return
  fi
  printf 'false'
}

health_ok() {
  curl -fsS --max-time "${CURL_TIMEOUT_SECONDS}" "${HEALTH_URL}" >/dev/null
}

notify_popup() {
  local message="$1"
  if [[ "$(normalize_bool "${ALERT_POPUP}")" != "true" ]]; then
    return 0
  fi
  if command -v notify-send >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
    notify-send "Grievance Watchdog" "${message}" || true
    return 0
  fi
  if command -v wall >/dev/null 2>&1; then
    wall "Grievance Watchdog: ${message}" || true
  fi
}

send_alert_email() {
  local subject="$1"
  local body="$2"
  if [[ "$(normalize_bool "${ALERT_EMAIL}")" != "true" ]]; then
    return 0
  fi
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" exec -T api \
    python -m grievance_api.scripts.ops_alert \
    --subject "${subject}" \
    --body "${body}" >/dev/null 2>&1 || log "alert email send skipped (api unavailable or Graph error)"
}

if [[ -f "${DISABLE_FILE}" ]]; then
  log "auto-restart disabled via ${DISABLE_FILE}"
  exit 0
fi

if health_ok; then
  printf '0\n' >"${FAIL_FILE}"
  exit 0
fi

if [[ -f "${FAIL_FILE}" ]]; then
  failure_count="$(tr -dc '0-9' < "${FAIL_FILE}")"
else
  failure_count="0"
fi
failure_count="${failure_count:-0}"
failure_count=$((failure_count + 1))
printf '%s\n' "${failure_count}" >"${FAIL_FILE}"

log "health check failed (${failure_count}/${FAIL_THRESHOLD}) at ${HEALTH_URL}"
if (( failure_count < FAIL_THRESHOLD )); then
  exit 0
fi

now_epoch="$(date +%s)"
if [[ -f "${LAST_RESTART_FILE}" ]]; then
  last_restart_epoch="$(tr -dc '0-9' < "${LAST_RESTART_FILE}")"
else
  last_restart_epoch="0"
fi
last_restart_epoch="${last_restart_epoch:-0}"
elapsed=$((now_epoch - last_restart_epoch))

if (( elapsed < COOLDOWN_SECONDS )); then
  log "restart cooldown active (${elapsed}s elapsed, ${COOLDOWN_SECONDS}s required)"
  exit 1
fi

printf '%s\n' "${now_epoch}" >"${LAST_RESTART_FILE}"
printf '0\n' >"${FAIL_FILE}"

log "restart threshold reached; running docker compose up -d"
if ! docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d; then
  failure="Grievance stack restart command failed on host $(hostname)."
  log "${failure}"
  notify_popup "${failure}"
  send_alert_email "[CRITICAL] Grievance stack restart failed" "${failure}"
  exit 1
fi

for ((attempt = 1; attempt <= POST_RESTART_RETRIES; attempt++)); do
  sleep "${POST_RESTART_DELAY_SECONDS}"
  if health_ok; then
    recovered="Grievance stack auto-restarted and recovered after ${attempt} post-restart checks."
    log "${recovered}"
    notify_popup "${recovered}"
    send_alert_email "[RECOVERY] Grievance stack restarted automatically" "${recovered}"
    exit 0
  fi
done

still_down="Grievance stack restart completed but health endpoint is still failing: ${HEALTH_URL}"
log "${still_down}"
notify_popup "${still_down}"
send_alert_email "[CRITICAL] Grievance stack still unhealthy after restart" "${still_down}"
exit 1
