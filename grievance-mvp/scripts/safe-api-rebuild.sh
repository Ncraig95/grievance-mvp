#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
DB_PATH="${SAFE_REBUILD_DB:-${PROJECT_DIR}/data/grievances.sqlite3}"
CHECK_ONLY=false
FORCE=false

usage() {
  cat <<'EOF'
Usage: ./scripts/safe-api-rebuild.sh [--check-only] [--force]

Checks the live SQLite database for in-flight work that should not be interrupted,
then rebuilds only the api service when safe.

Options:
  --check-only  Run safety checks and exit without rebuilding.
  --force       Print blockers but continue anyway. Use only when you accept the risk.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only)
      CHECK_ONLY=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

log() {
  printf '[safe-api-rebuild] %s\n' "$1"
}

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

API_PORT_DEFAULT="${API_PORT:-8080}"
HEALTH_URL="${SAFE_REBUILD_HEALTH_URL:-http://127.0.0.1:${API_PORT_DEFAULT}/healthz}"
CURL_TIMEOUT_SECONDS="${SAFE_REBUILD_CURL_TIMEOUT_SECONDS:-8}"
POST_REBUILD_RETRIES="${SAFE_REBUILD_HEALTH_RETRIES:-24}"
POST_REBUILD_DELAY_SECONDS="${SAFE_REBUILD_HEALTH_DELAY_SECONDS:-5}"

if [[ ! -f "${DB_PATH}" ]]; then
  log "BLOCKED: database not found at ${DB_PATH}"
  log "Refusing to rebuild because safety checks cannot run."
  exit 2
fi

set +e
python3 - "${DB_PATH}" <<'PYCHECK'
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

path = sys.argv[1]
now = datetime.now(timezone.utc).isoformat()
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def table_exists(name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return bool(row)


def collect(table: str, label: str, where_sql: str, columns: list[str]) -> dict | None:
    if not table_exists(table):
        return None
    count_sql = f"SELECT COUNT(*) AS count FROM {table} WHERE {where_sql}"
    count = int(conn.execute(count_sql, {"now": now}).fetchone()["count"])
    if count <= 0:
        return None
    sample_cols = ", ".join(columns)
    sample_sql = f"SELECT {sample_cols} FROM {table} WHERE {where_sql} LIMIT 8"
    samples = [dict(row) for row in conn.execute(sample_sql, {"now": now}).fetchall()]
    return {"label": label, "table": table, "count": count, "samples": samples}

checks: list[dict] = []
for item in [
    collect(
        "cases",
        "cases currently processing intake/document work",
        "lower(COALESCE(status, '')) IN ('processing')",
        ["id", "status", "approval_status", "created_at_utc"],
    ),
    collect(
        "documents",
        "grievance documents with active DocuSeal/signature work",
        """
        COALESCE(requires_signature, 0)=1 AND (
          lower(COALESCE(status, ''))='awaiting_signature'
          OR lower(COALESCE(status, '')) LIKE 'sent_for_signature%'
        )
        """,
        ["id", "case_id", "doc_type", "status", "docuseal_submission_id", "created_at_utc"],
    ),
    collect(
        "document_stages",
        "staged signature rows still preparing or awaiting signature",
        """
        completed_at_utc IS NULL
        AND failed_at_utc IS NULL
        AND (
          lower(COALESCE(status, ''))='preparing'
          OR lower(COALESCE(status, '')) LIKE 'sent_for_signature%'
          OR docuseal_submission_id IS NOT NULL
        )
        """,
        ["id", "case_id", "document_id", "stage_no", "status", "docuseal_submission_id", "started_at_utc"],
    ),
    collect(
        "standalone_submissions",
        "standalone hosted form submissions currently processing or awaiting signature",
        "lower(COALESCE(status, '')) IN ('processing', 'awaiting_signature')",
        ["id", "form_key", "signer_email", "status", "created_at_utc"],
    ),
    collect(
        "standalone_documents",
        "standalone documents with active DocuSeal/signature work",
        """
        COALESCE(requires_signature, 0)=1 AND (
          lower(COALESCE(status, ''))='awaiting_signature'
          OR lower(COALESCE(status, '')) LIKE 'sent_for_signature%'
        )
        """,
        ["id", "submission_id", "form_key", "status", "docuseal_submission_id", "created_at_utc"],
    ),
    collect(
        "pay_periods",
        "pay periods locked or awaiting president signature",
        "lower(COALESCE(status, '')) IN ('locked', 'awaiting_signature')",
        ["id", "period_start", "period_end", "status", "locked_by", "locked_at_utc", "president_email"],
    ),
    collect(
        "pay_packets",
        "pay packets awaiting president DocuSeal signature",
        "lower(COALESCE(status, '')) IN ('awaiting_signature', 'sent_for_signature', 'processing')",
        ["id", "period_id", "revision", "status", "docuseal_submission_id", "created_at_utc"],
    ),
    collect(
        "statement_auto_sign_jobs",
        "statement auto-sign jobs due or locked",
        """
        lower(COALESCE(status, '')) IN ('running', 'processing')
        OR (
          lower(COALESCE(status, ''))='pending'
          AND (locked_at_utc IS NOT NULL OR run_after_utc <= :now)
        )
        """,
        ["id", "case_id", "document_id", "status", "run_after_utc", "locked_at_utc", "attempts"],
    ),
    collect(
        "pay_demo_jobs",
        "pay demo jobs queued or running",
        "lower(COALESCE(status, '')) IN ('queued', 'running', 'processing')",
        ["id", "status", "created_at_utc", "updated_at_utc"],
    ),
]:
    if item:
        checks.append(item)

conn.close()

if checks:
    print("BLOCKED: unsafe in-flight work found. Do not rebuild the api yet.\n")
    for check in checks:
        print(f"- {check['label']} ({check['table']}): {check['count']}")
        for sample in check["samples"]:
            rendered = ", ".join(f"{k}={v}" for k, v in sample.items())
            print(f"  {rendered}")
    print("\nUse --force only if you have confirmed the interruption is acceptable.")
    raise SystemExit(2)

print("OK: no unsafe in-flight signature, pay packet, processing, or due auto-sign work found.")
PYCHECK
check_rc=$?
set -e

if [[ "${CHECK_ONLY}" == "true" ]]; then
  exit "${check_rc}"
fi

if [[ "${check_rc}" -ne 0 ]]; then
  if [[ "${FORCE}" != "true" ]]; then
    exit "${check_rc}"
  fi
  log "FORCE requested; continuing despite safety check blockers."
fi

log "Rebuilding api service only."
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --build api

log "Waiting for health check: ${HEALTH_URL}"
for ((attempt = 1; attempt <= POST_REBUILD_RETRIES; attempt++)); do
  if curl -fsS --max-time "${CURL_TIMEOUT_SECONDS}" "${HEALTH_URL}" >/dev/null; then
    log "API health check passed after rebuild."
    exit 0
  fi
  sleep "${POST_REBUILD_DELAY_SECONDS}"
done

log "API rebuild finished, but health check did not recover at ${HEALTH_URL}."
exit 1
