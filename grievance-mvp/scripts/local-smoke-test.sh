#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE=""
ALLOW_DOT_ENV=0

usage() {
  cat <<'USAGE'
Usage: scripts/local-smoke-test.sh [--env-file PATH] [--allow-dot-env]

Defaults to ./.env.local when present, otherwise ./.env.local.example.
Refuses .env unless --allow-dot-env is explicitly supplied.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file)
      if [ "$#" -lt 2 ]; then
        echo "[local-smoke] --env-file requires a path" >&2
        exit 2
      fi
      ENV_FILE="$2"
      shift 2
      ;;
    --allow-dot-env)
      ALLOW_DOT_ENV=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[local-smoke] unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$ENV_FILE" ]; then
  if [ -f ./.env.local ]; then
    ENV_FILE="./.env.local"
  else
    ENV_FILE="./.env.local.example"
  fi
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "[local-smoke] env file not found: $ENV_FILE" >&2
  exit 1
fi

ENV_REALPATH="$(python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).resolve())
PY
)"
DOT_ENV_REALPATH="$(python3 - <<'PY'
from pathlib import Path
print(Path('.env').resolve())
PY
)"
if [ "$ENV_REALPATH" = "$DOT_ENV_REALPATH" ] && [ "$ALLOW_DOT_ENV" != "1" ]; then
  echo "[local-smoke] refusing to use .env without --allow-dot-env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ "${APP_MODE:-}" != "local" ]; then
  echo "[local-smoke] APP_MODE must be local in $ENV_FILE" >&2
  exit 1
fi

APP_ENV_FILE="$ENV_FILE"
export APP_ENV_FILE
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-grievance_mvp_local_safe}"
export COMPOSE_PROJECT_NAME
API_PORT="${API_PORT:-8080}"
HMAC_SECRET="${LOCAL_HMAC_SHARED_SECRET:-local-dev-hmac-secret}"
WEBHOOK_SECRET="${LOCAL_DOCUSEAL_WEBHOOK_SECRET:-local-dev-docuseal-webhook-secret}"
LOCAL_DB_PATH="${LOCAL_DB_PATH:-/data/local-safe/grievances.sqlite3}"
LOCAL_DATA_ROOT="${LOCAL_DATA_ROOT:-/data/local-safe/grievances}"

case "$LOCAL_DB_PATH" in
  /data/local-safe/*) HOST_DB_PATH="./data/local-safe/${LOCAL_DB_PATH#/data/local-safe/}" ;;
  *) echo "[local-smoke] LOCAL_DB_PATH must stay under /data/local-safe" >&2; exit 1 ;;
esac
case "$LOCAL_DATA_ROOT" in
  /data/local-safe/*) HOST_DATA_ROOT="./data/local-safe/${LOCAL_DATA_ROOT#/data/local-safe/}" ;;
  *) echo "[local-smoke] LOCAL_DATA_ROOT must stay under /data/local-safe" >&2; exit 1 ;;
esac

running_services="$(docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" ps --services --filter status=running 2>/dev/null || true)"
for forbidden in smtp_graph_bridge docuseal docuseal_db docuseal_proxy cloudflared; do
  if printf '%s\n' "$running_services" | grep -qx "$forbidden"; then
    echo "[local-smoke] refusing to run while forbidden local-safe service is already running: $forbidden" >&2
    exit 1
  fi
done

echo "[local-smoke] using env file: $ENV_FILE"
echo "[local-smoke] using compose project: $COMPOSE_PROJECT_NAME"
echo "[local-smoke] resetting ./data/local-safe only"
rm -rf ./data/local-safe
mkdir -p ./data/local-safe

echo "[local-smoke] starting API only"
docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" up -d --build --force-recreate api >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null

echo "[local-smoke] posting signed intake"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
PAYLOAD_FILE="$WORK_DIR/intake.json"
INTAKE_RESPONSE="$WORK_DIR/intake-response.json"
WEBHOOK_PAYLOAD="$WORK_DIR/webhook.json"
WEBHOOK_RESPONSE="$WORK_DIR/webhook-response.json"

python3 - "$PAYLOAD_FILE" <<'PY'
import json
import sys
from datetime import date
payload = {
    "request_id": f"local-smoke-{date.today().isoformat()}",
    "contract": "AT&T",
    "grievant_firstname": "Local",
    "grievant_lastname": "Signer",
    "grievant_email": "local.signer@example.invalid",
    "grievant_phone": "555-0100",
    "work_location": "Local-safe test desk",
    "supervisor": "Test Supervisor",
    "incident_date": date.today().isoformat(),
    "narrative": "Local-safe smoke test narrative for the grievance workflow.",
    "template_data": {
        "personal_email": "local.signer@example.invalid",
        "grievant_email": "local.signer@example.invalid",
        "article": "Local-safe Article 1",
    },
    "documents": [
        {
            "doc_type": "grievance_form",
            "template_key": "grievance_form",
            "requires_signature": True,
            "signers": ["local.signer@example.invalid"],
        }
    ],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(payload, f, separators=(",", ":"), sort_keys=True)
PY

TS="$(date +%s)"
SIG="$(python3 - "$HMAC_SECRET" "$TS" "$PAYLOAD_FILE" <<'PY'
import hashlib
import hmac
import sys
secret, ts, path = sys.argv[1:]
body = open(path, "rb").read()
mac = hmac.new(secret.encode("utf-8"), ts.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
print("sha256=" + mac)
PY
)"
HTTP_STATUS="$(curl -sS -o "$INTAKE_RESPONSE" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -H "X-Timestamp: $TS" \
  -H "X-Signature: $SIG" \
  --data-binary "@$PAYLOAD_FILE" \
  "http://127.0.0.1:${API_PORT}/intake")"
if [ "$HTTP_STATUS" != "200" ]; then
  echo "[local-smoke] intake failed with HTTP $HTTP_STATUS" >&2
  cat "$INTAKE_RESPONSE" >&2
  exit 1
fi

read -r CASE_ID DOCUMENT_ID SUBMISSION_ID < <(python3 - "$INTAKE_RESPONSE" "$HOST_DB_PATH" <<'PY'
import json
import re
import sqlite3
import sys
from urllib.parse import unquote
resp_path, db_path = sys.argv[1:]
resp = json.load(open(resp_path, encoding="utf-8"))
case_id = resp["case_id"]
doc = resp["documents"][0]
document_id = doc["document_id"]
signing_link = str(doc.get("signing_link") or "")
match = re.search(r"local://docuseal/submissions/([^/]+)/sign/", signing_link)
if not match:
    raise SystemExit(f"missing local synthetic signing link: {signing_link}")
link_submission_id = unquote(match.group(1))
with sqlite3.connect(db_path) as con:
    row = con.execute("SELECT docuseal_submission_id FROM documents WHERE id=?", (document_id,)).fetchone()
if not row or not row[0]:
    raise SystemExit("missing docuseal_submission_id after intake")
if str(row[0]) != link_submission_id:
    raise SystemExit("signing link submission id does not match database submission id")
print(case_id, document_id, link_submission_id)
PY
)
echo "[local-smoke] case: $CASE_ID document: $DOCUMENT_ID submission: $SUBMISSION_ID"

echo "[local-smoke] simulating DocuSeal completion webhook"
python3 - "$WEBHOOK_PAYLOAD" "$SUBMISSION_ID" <<'PY'
import json
import sys
from datetime import datetime, timezone
path, submission_id = sys.argv[1:]
payload = {
    "event": "submission.completed",
    "submission_id": submission_id,
    "submission": {
        "id": submission_id,
        "status": "completed",
        "submitters": [
            {
                "email": "local.signer@example.invalid",
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    },
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, separators=(",", ":"), sort_keys=True)
PY
WEBHOOK_SIG="$(python3 - "$WEBHOOK_SECRET" "$WEBHOOK_PAYLOAD" <<'PY'
import hashlib
import hmac
import sys
secret, path = sys.argv[1:]
body = open(path, "rb").read()
print(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest())
PY
)"
HTTP_STATUS="$(curl -sS -o "$WEBHOOK_RESPONSE" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -H "X-DocuSeal-Signature: $WEBHOOK_SIG" \
  --data-binary "@$WEBHOOK_PAYLOAD" \
  "http://127.0.0.1:${API_PORT}/webhook/docuseal")"
if [ "$HTTP_STATUS" != "200" ]; then
  echo "[local-smoke] webhook failed with HTTP $HTTP_STATUS" >&2
  cat "$WEBHOOK_RESPONSE" >&2
  exit 1
fi
python3 - "$WEBHOOK_RESPONSE" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
if not payload.get("ok") or not payload.get("handled"):
    raise SystemExit(f"webhook did not handle completion: {payload}")
PY

echo "[local-smoke] verifying local artifacts"
python3 - "$HOST_DB_PATH" "$HOST_DATA_ROOT" "$CASE_ID" "$DOCUMENT_ID" "$SUBMISSION_ID" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1])
data_root = Path(sys.argv[2])
case_id, document_id, submission_id = sys.argv[3:6]
if not db_path.exists():
    raise SystemExit(f"db missing: {db_path}")

def host_path(container_path: str) -> Path:
    if not container_path:
        return Path("__missing__")
    if container_path.startswith("/data/"):
        return Path("data") / container_path[len("/data/"):]
    return Path(container_path)

with sqlite3.connect(db_path) as con:
    doc = con.execute(
        """
        SELECT status, pdf_path, signed_pdf_path, audit_zip_path,
               sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url
        FROM documents WHERE id=?
        """,
        (document_id,),
    ).fetchone()
    if not doc:
        raise SystemExit("document row missing")
    email_count = con.execute(
        "SELECT COUNT(1) FROM outbound_emails WHERE status='sent'"
    ).fetchone()[0]

status, pdf_path, signed_pdf_path, audit_zip_path, sp_generated, sp_signed, sp_audit = doc
if status not in {"signed", "approved"}:
    raise SystemExit(f"unexpected document status after webhook: {status}")
for label, container_path in {
    "generated_pdf": pdf_path,
    "signed_pdf": signed_pdf_path,
    "audit_zip": audit_zip_path,
}.items():
    path = host_path(str(container_path or ""))
    if not path.exists() or path.stat().st_size == 0:
        raise SystemExit(f"{label} missing or empty: {path}")

if not all(str(value or "").startswith("local://sharepoint/") for value in (sp_generated, sp_signed, sp_audit)):
    raise SystemExit("expected local://sharepoint URLs for generated/signed/audit uploads")

sharepoint_root = data_root / "local_mock" / "sharepoint" / "Documents" / "Grievances"
expected_names = {
    f"grievance_form_{document_id}.pdf",
    f"grievance_form_{document_id}_signed.pdf",
    f"grievance_form_{document_id}_audit.zip",
}
found_names = {p.name for p in sharepoint_root.rglob("*") if p.is_file()}
missing = sorted(expected_names - found_names)
if missing:
    raise SystemExit(f"local SharePoint artifacts missing: {missing}")

mail_messages = list((data_root / "local_mock" / "mail").glob("*/message.json"))
if not mail_messages:
    raise SystemExit("local mail JSON files missing")
if email_count < 1:
    raise SystemExit("outbound email audit rows missing")

submission_root = data_root / "local_mock" / "docuseal" / "submissions" / submission_id
for name in ("submitted.pdf", "signed.pdf", "completed.zip", "metadata.json"):
    path = submission_root / name
    if not path.exists() or path.stat().st_size == 0:
        raise SystemExit(f"local DocuSeal artifact missing: {path}")

print(f"[local-smoke] verified generated/signed/audit files, {email_count} sent email audit rows, {len(mail_messages)} local mail messages")
PY

echo "[local-smoke] PASS"
