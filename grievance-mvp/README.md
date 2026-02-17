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
- If missing, creates `<grievance_id> <member_name>`.
- Upload targets:
  - `graph.generated_subfolder`
  - `graph.signed_subfolder`
  - `graph.audit_subfolder`

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
