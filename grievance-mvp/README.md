# Grievance Automation MVP

Production-oriented grievance automation service on Ubuntu + Docker Compose.

## 1) Architecture

```
Microsoft Forms / Intake Client
        |
        | (HMAC-signed POST /intake)
        v
FastAPI Orchestrator (this app)
  - validates + normalizes payload
  - creates Case + Document records (SQLite)
  - renders DOCX templates + converts to PDF
  - sends DocuSeal submissions for docs requiring signature
  - sends all outbound mail via Microsoft Graph Mail API
        |
        +--> DocuSeal (signing + audit trail)
        |      |
        |      +-- webhook --> POST /webhook/docuseal
        |
        +--> SharePoint (Microsoft Graph)
               - finds/creates case folder by grievance_id
               - uploads generated/signed/audit files

Approval flow:
  - Derek approves/rejects via POST /cases/{case_id}/approval
  - approval is audited in events table
  - status update emails are sent by Graph mail service
```

## 2) Workflow behavior

### Intake
- Endpoint: `POST /intake`
- Idempotency key: `request_id` (unique in `cases.intake_request_id`)
- Supports multiple documents in a single intake payload.
- Supports single-document command mode via `document_command` (convenience for Power Automate).
- Supports optional `client_supplied_files` list for uploaded form artifacts.
- `grievance_number` is optional on intake. If omitted, signature-required docs are queued in limbo until assigned later.
- Extra JSON keys and `template_data` are merged into DOCX template context (normalized snake_case aliases are also added).

### Document creation
- Per document:
  - choose template by `doc_templates[template_key/doc_type]`, fallback to `docx_template_path`
  - render DOCX
  - convert DOCX -> PDF using headless LibreOffice
  - persist paths + SHA256 in `documents`

### E-signature
- For `requires_signature=true`, app queues documents in `pending_grievance_number` until a grievance number exists.
- Once a grievance number is assigned (`POST /cases/{case_id}/grievance-number` or intake provides `grievance_number`), app submits document to DocuSeal API.
- On self-hosted OSS DocuSeal where `POST /api/templates` is unavailable, app uses a base template (`docuseal.default_template_id`) and performs web `clone_and_replace` with the generated PDF before submission.
  - Requires web credentials (`DOCUSEAL_WEB_EMAIL` / `DOCUSEAL_WEB_PASSWORD`) and HTTPS base (`docuseal.web_base_url` or `docuseal.public_base_url`).
  - If the generated PDF contains Adobe-style placeholders (`{{Sig_es_:signerN:signature}}`, `{{Dte_es_:signerN:date}}`), app auto-aligns DocuSeal fields to those exact coordinates.
- App-owned mail sends signature requests (DocuSeal SMTP is not used).
- DocuSeal links are rewritten to public HTTPS origin via `docuseal.public_base_url` when needed.

### Webhook completion
- Endpoint: `POST /webhook/docuseal`
- Verifies signature if `docuseal.webhook_secret` is configured.
- Deduplicates by `webhook_receipts(provider, receipt_key)`.
- Stores artifacts locally and uploads generated/signed/audit files to SharePoint.
- Sends completion notifications to signer(s), internal recipients, and Derek.
- Moves case to `pending_approval` when signature-required docs are complete.

### Approval
- Endpoint: `POST /cases/{case_id}/approval`
- Derek approves/rejects with audit trail.
- Optional grievance number recorded on approval.
- Sends app-owned status-update emails via Graph.
- On approval, app ensures SharePoint case folder and uploads any remaining generated/signed/audit artifacts that were not uploaded earlier.

### Grievance Number Assignment
- Endpoint: `POST /cases/{case_id}/grievance-number`
- Assigns grievance number and releases any documents in limbo (`pending_grievance_number`) for signature dispatch.

### SharePoint placement
- App searches under `graph.case_parent_folder` for existing folder whose name contains `grievance_id`.
- If missing, creates `<grievance_id> <member_name> - <contract>` when contract is available (otherwise `<grievance_id> <member_name>`).
- Upload targets:
  - `graph.generated_subfolder`
  - `graph.signed_subfolder`
  - `graph.audit_subfolder`
  - `graph.client_supplied_subfolder` (created only when `client_supplied_files` are provided)

## 3) Data model (SQLite)

Core tables:
- `cases`
  - `id`, `grievance_id`, `status`, `approval_status`, `grievance_number`, `intake_request_id`, `member_*`, SharePoint folder metadata
- `documents`
  - one row per document per case
  - status lifecycle, template key, signature requirements, DocuSeal ids/links, local file paths, SharePoint URLs
- `events`
  - append-only audit log with `case_id`, optional `document_id`, `event_type`, JSON details
- `webhook_receipts`
  - webhook idempotency and dedupe
- `outbound_emails`
  - per-recipient mail audit including idempotency key, `graph_message_id`, resend tracking

## 4) Compose + infrastructure

### Compose from repo root (supported)
- File: `docker-compose.yml` (repo root)
- Uses `grievance-mvp/.env` via `--env-file` in Makefile.

### Compose from `grievance-mvp/` (supported)
- File: `grievance-mvp/docker-compose.yml`

### DocuSeal HTTPS forwarding
- `docuseal_proxy` (nginx) forwards Host + `X-Forwarded-*` headers to DocuSeal.
- Cloudflare tunnel can target `docuseal_proxy`.
- DocuSeal public URL is kept in sync by `sync-docuseal-public-url.sh`.

## 5) Runbook

From repo root:

```bash
make up
make ps
curl -sS http://127.0.0.1:8080/healthz
```

From `grievance-mvp/`:

```bash
docker compose --env-file .env up -d --build
```

Sync DocuSeal public URL:

```bash
cd grievance-mvp
./sync-docuseal-public-url.sh
```

## 6) Smoke tests

Local end-to-end smoke (intake -> docs -> approval -> docuseal local/external checks):

```bash
cd grievance-mvp
./scripts/smoke-e2e.sh
```

Signed-intake smoke (requires real DocuSeal API token + template with signer fields):

```bash
cd grievance-mvp
./scripts/smoke-signed-intake.sh
```

What the script validates:
- compose config + service startup
- API health
- intake with multiple documents
- case status retrieval
- Derek approval endpoint
- DocuSeal local proxy HTTP reachability
- external HTTPS DocuSeal host reachability

Download-link checks (manual):

```bash
cd grievance-mvp
# sample slug discovery
docker compose --env-file .env exec -T docuseal_db \
  psql -U ${DOCUSEAL_DB_USER} -d ${DOCUSEAL_DB_NAME} -Atc \
  "SELECT slug FROM submitters ORDER BY id DESC LIMIT 5;"

# external test (requires valid completed submitter slug/signature context)
curl -I https://${DOCUSEAL_HOST}/s/<slug>/download

# local proxy with host header
curl -I -H "Host: ${DOCUSEAL_HOST}" http://127.0.0.1:${DOCUSEAL_PORT}/s/<slug>/download
```

Automated helper for completed slugs:

```bash
cd grievance-mvp
./scripts/verify-docuseal-download.sh
```

## 7) Microsoft Graph requirements

Mail (app-owned delivery):
- `Mail.Send`
- `Mail.ReadWrite` (used to create draft and record message id)

SharePoint:
- Prefer `Sites.Selected` with site-scoped grants
- Document library must be reachable by configured site + library name

Least privilege:
- Restrict mailbox scope with Exchange application access policy to the authorized sender mailbox.

## 8) Security notes

- Do not commit secrets.
- Keep secrets in:
  - `grievance-mvp/.env`
  - `grievance-mvp/config/config.yaml`
  - `grievance-mvp/config/graph_cert.pem`
- App logs metadata only (case/document ids, status, message ids), not document contents.

## 9) Key API endpoints

- `GET /healthz`
- `POST /intake`
- `GET /cases/{case_id}`
- `POST /cases/{case_id}/grievance-number`
- `POST /webhook/docuseal`
- `POST /cases/{case_id}/notifications/resend`
- `GET /cases/{case_id}/approval`
- `POST /cases/{case_id}/approval`

## 10) Power Automate setup (Statement of Occurrence)

Use the intake `document_command` so the flow tells the app exactly which document to run.

For this document:
- `document_command: "statement_of_occurrence"`

Flow shape:
1. Trigger: `When a new response is submitted` (Microsoft Forms)
2. Action: `Get response details`
3. Action: `HTTP` (POST) to `https://<your-api-host>/intake`
4. Parse JSON response and store `case_id`, `grievance_id`, and `documents[0].signing_link` if needed

HTTP body example:

```json
{
  "request_id": "forms-@{triggerOutputs()?['body/responseId']}",
  "document_command": "statement_of_occurrence",
  "contract": "COJ",
  "grievant_firstname": "@{outputs('Get_response_details')?['body/r_first_name']}",
  "grievant_lastname": "@{outputs('Get_response_details')?['body/r_last_name']}",
  "grievant_email": "@{outputs('Get_response_details')?['body/r_work_email']}",
  "grievant_phone": "@{outputs('Get_response_details')?['body/r_phone']}",
  "work_location": "@{outputs('Get_response_details')?['body/r_work_location']}",
  "supervisor": "@{outputs('Get_response_details')?['body/r_supervisor']}",
  "incident_date": "@{outputs('Get_response_details')?['body/r_incident_date']}",
  "narrative": "@{outputs('Get_response_details')?['body/r_statement']}",
  "grievance_number": "@{outputs('Get_response_details')?['body/r_grievance_number']}",
  "template_data": {
    "personal_email": "@{outputs('Get_response_details')?['body/r_personal_email']}",
    "article": "@{outputs('Get_response_details')?['body/r_article']}",
    "statement_continuation": "@{outputs('Get_response_details')?['body/r_statement_cont']}",
    "witness_1_name": "@{outputs('Get_response_details')?['body/r_witness_1_name']}",
    "witness_1_title": "@{outputs('Get_response_details')?['body/r_witness_1_title']}",
    "witness_1_phone": "@{outputs('Get_response_details')?['body/r_witness_1_phone']}"
  },
  "client_supplied_files": [
    {
      "file_name": "supporting-evidence.pdf",
      "download_url": "@{outputs('Get_file_metadata')?['body/@microsoft.graph.downloadUrl']}"
    }
  ]
}
```

Notes:
- If `grievance_number` is blank, signature dispatch is queued until Derek assigns one.
- If `template_data.personal_email` is present, that address is used as signer email.
- `documents` array still works and takes precedence if you send both.
- If `hmac_shared_secret` is set (not `REPLACE...`), include `X-Timestamp` and `X-Signature` headers.

For Forms file uploads:
- Add `client_supplied_files` to intake payload.
- Each item should include `file_name` and either:
  - `download_url` (recommended for large files up to 1GB total), or
  - `content_base64` (small files only).
- Intake returns `503` if file transfer/upload fails so Power Automate can retry.

Example file item:

```json
{
  "file_name": "supporting-evidence.pdf",
  "download_url": "https://<tenant>.sharepoint.com/.../download?...token..."
}
```

## 11) Dynamic Statement Rows (Template-ready backend)

Backend is now pre-wired for dynamic lined statement rows; no additional API code changes are required when you update the DOCX.

Context keys available to template:
- `statement_lines`: list of `{ "text": "...", "line_no": N }`
- `statement_rows`: alias of `statement_lines`
- `statement_line_count`
- `statement_full_text`
- `statement_has_continuation`

Optional input override from Forms/Power Automate:
- `template_data.statement_line_wrap_width` (default: `95`)

Recommended DOCX row loop tags (when you are ready to edit the Word file):

```jinja
{%tr for line in statement_lines %}
{{ line.text }}
{%tr endfor %}
```
