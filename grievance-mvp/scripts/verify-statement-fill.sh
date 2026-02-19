#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_PORT="${API_PORT:-8080}"
SIGNER_EMAIL="${SIGNER_EMAIL:-ncraig2@me.com}"

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

RUN_TOKEN="VERIFY$(date +%s)"
REQUEST_ID="verify-${RUN_TOKEN}"

PAYLOAD="$(cat <<JSON
{
  "request_id":"${REQUEST_ID}",
  "grievance_number":"GN-${RUN_TOKEN}",
  "contract":"AT&T",
  "grievant_firstname":"Nick",
  "grievant_lastname":"Craig",
  "grievant_email":"nick.craig@cwa3106.com",
  "grievant_phone":"904-555-1234",
  "work_location":"Jacksonville Yard",
  "supervisor":"Derek Williamson",
  "incident_date":"2026-02-19",
  "narrative":"Statement narrative ${RUN_TOKEN}",
  "template_data":{
    "personal_email":"${SIGNER_EMAIL}",
    "article":"Article 12",
    "statement_continuation":"Continuation ${RUN_TOKEN}",
    "witness_1_name":"Witness Prime",
    "witness_1_title":"Steward",
    "witness_1_phone":"904-555-1111"
  },
  "documents":[{"doc_type":"grievance_form","requires_signature":true}]
}
JSON
)"

RESP="$(curl -fsS -X POST "http://127.0.0.1:${API_PORT}/intake" -H "Content-Type: application/json" -d "$PAYLOAD")"
printf '%s\n' "$RESP"

CASE_ID="$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["case_id"])')"

docker run --rm -i \
  -v "$(pwd)/data:/data" \
  -e CASE_ID="$CASE_ID" \
  -e RUN_TOKEN="$RUN_TOKEN" \
  -e SIGNER_EMAIL="$SIGNER_EMAIL" \
  grievance_mvp-api python - <<'PY'
import os
import re
import sqlite3
import zipfile

case_id = os.environ["CASE_ID"]
run_token = os.environ["RUN_TOKEN"]
expected_signer = os.environ["SIGNER_EMAIL"]

con = sqlite3.connect("/data/grievances.sqlite3")
con.row_factory = sqlite3.Row
doc = con.execute(
    "select id, docx_path, docuseal_signing_link, status from documents where case_id=?",
    (case_id,),
).fetchone()
mail = con.execute(
    "select recipient_email, status, graph_message_id from outbound_emails where case_id=? order by id desc limit 1",
    (case_id,),
).fetchone()
print("DOC", dict(doc))
print("MAIL", dict(mail) if mail else None)
if not mail or mail["recipient_email"].lower() != expected_signer.lower():
    raise SystemExit(f"Signer email mismatch; expected {expected_signer}")

xml = zipfile.ZipFile(doc["docx_path"]).read("word/document.xml").decode("utf-8", "ignore")
left = re.findall(r"\{\{.*?\}\}", xml)
print("LEFTOVER", left)
allowed = {"{{Sig_es_:signer1:signature}}", "{{Dte_es_:signer1:date}}"}
if any(item not in allowed for item in left):
    raise SystemExit("Unexpected leftover placeholders in statement document")

checks = [
    f"Statement narrative {run_token}",
    f"Continuation {run_token}",
    "Article 12",
    "Witness Prime",
]
for item in checks:
    ok = item in xml
    print(item, "=>", ok)
    if not ok:
        raise SystemExit(f"Missing expected merged value: {item}")

print("VERIFY_OK")
con.close()
PY
