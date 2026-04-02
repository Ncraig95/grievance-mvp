# Grievance Automation MVP

Production-grade grievance operations platform for union locals running on Ubuntu + Docker Compose.
It accepts Microsoft Forms submissions, generates grievance documents from template libraries, routes signatures through DocuSeal, emails participants through Microsoft Graph, and files artifacts into SharePoint with a full audit trail.

This repo is set up for real workflow operations: idempotent intake, staged signature handling, approval controls, automated notifications, SharePoint foldering, and recovery-oriented runtime tooling (health checks, watchdog, smoke tests).

## Public Form Links (Local Testing)

Use this section in GitHub so other locals can run the process end-to-end.
Replace each placeholder URL with the published Microsoft Form link for that local.

| Local | Form | Document command | Test form link |
|---|---|---|---|
| Local 3106 | Statement of Occurrence | `statement_of_occurrence` | `https://forms.office.com/Pages/ResponsePage.aspx?id=Gm8hRgwHT0uKp3F4_OjDLETN-f9DdKFBsB6MNcKlT95UMFFIODk2WDNaNzJNUDJQVlVSS1QxUVRFMi4u` |
| Local 3106 | BellSouth Meeting Request | `bellsouth_meeting_request` | `https://forms.office.com/Pages/ResponsePage.aspx?id=Gm8hRgwHT0uKp3F4_OjDLETN-f9DdKFBsB6MNcKlT95URUlRMlI3TzFRQVpWMFhKRk5CVE4zU0daNi4u` |
| Local 3106 | Mobility Meeting Request | `mobility_meeting_request` | `https://forms.office.com/Pages/ResponsePage.aspx?id=Gm8hRgwHT0uKp3F4_OjDLETN-f9DdKFBsB6MNcKlT95UMERRWUI3VkIwNVlRNzZVWFU4MU8yU0FKVC4u` |
| Local 3106 | Grievance Data Request | `grievance_data_request` | `https://forms.office.com/r/REPLACE-DATA-3106` |
| Local 3106 | True Intent Grievance Brief | `true_intent_brief` | `https://forms.office.com/r/REPLACE-TRUEINTENT-3106` |
| Local 3106 | Disciplinary Grievance Brief | `disciplinary_brief` | `https://forms.office.com/r/REPLACE-DISCIPLINARY-3106` |
| Local 3106 | Settlement Form 3106 | `settlement_form` | `https://forms.office.com/Pages/ResponsePage.aspx?id=Gm8hRgwHT0uKp3F4_OjDLETN-f9DdKFBsB6MNcKlT95UQzY4TVFaSUJPMVk0T1RUODhGVU0yQTE5UC4u` |
| Local 3106 | Mobility Record of Grievance | `mobility_record_of_grievance` | `https://forms.office.com/r/REPLACE-MOBILITY-RECORD-3106` |
| Local 3106 | BST Grievance Form 3G3A | `bst_grievance_form_3g3a` | `https://forms.office.com/r/REPLACE-3G3A-3106` |
| Standalone | AT&T Mobility Bargaining Suggestion | `att_mobility_bargaining_suggestion` | `https://forms.office.com/r/REPLACE-ATT-MOBILITY-BARGAINING` |
| Local (add rows) | Any additional local-specific form | `<document_command>` | `https://forms.office.com/r/REPLACE-LINK` |

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
               - can fan out extra audit copies to backup subfolders

Approval flow:
  - Derek approves/rejects via POST /cases/{case_id}/approval
  - approval is audited in events table
  - status update emails are sent by Graph mail service
```

## 2) Workflow behavior

### Intake
- Endpoint: `POST /intake`
- Idempotency key: `request_id` (unique in `cases.intake_request_id`)
- Reusing the same `request_id` returns the existing case (`intake_deduped`) and will not create/send again.
- Supports multiple documents in a single intake payload.
- Supports single-document command mode via `document_command` (convenience for Power Automate).
- Supports optional `client_supplied_files` list for uploaded form artifacts.
- `grievance_number` is optional on intake.
- `wait_for_grievance_number_before_signature` controls gating:
  - `true` (default): signature-required docs are queued until grievance number is assigned.
  - `false`: signature-required docs are sent immediately even when grievance number is blank.
- `require_approver_decision` controls the final approval step:
  - `true` (default): case moves to `pending_approval` and Derek approval flow is used.
  - `false`: case is auto-approved by the workflow after signatures complete.
- Extra JSON keys and `template_data` are merged into DOCX template context (normalized snake_case aliases are also added).
- `log_level` controls runtime verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`).
- `rendering` config controls template normalization and per-document layout policies:
  - `normalize_split_placeholders`: rewrites split `{{ ... }}` fields before render.
  - `layout_policies.<doc_type>`: optional clamp/fallback rules for fixed-width lines.
- `docx_pdf_engine` selects DOCX -> PDF backend:
  - `libreoffice` (default): local `soffice --headless`
  - `graph_word_online`: Microsoft Graph Word Online conversion

### Document creation
- Per document:
  - choose template by `doc_templates[template_key/doc_type]`, fallback to `docx_template_path`
  - render DOCX
  - convert DOCX -> PDF using configured backend (`docx_pdf_engine`)
  - persist paths + SHA256 in `documents`

### E-signature
- For `requires_signature=true`, queueing behavior depends on `wait_for_grievance_number_before_signature`.
- Once a grievance number is assigned (`POST /cases/{case_id}/grievance-number` or intake provides `grievance_number`), app submits document to DocuSeal API.
- On self-hosted OSS DocuSeal where `POST /api/templates` is unavailable, app uses a base template (`docuseal.default_template_id`) and performs web `clone_and_replace` with the generated PDF before submission.
  - Requires web credentials (`DOCUSEAL_WEB_EMAIL` / `DOCUSEAL_WEB_PASSWORD`) and HTTPS base (`docuseal.web_base_url` or `docuseal.public_base_url`).
  - If the generated PDF contains Adobe-style placeholders (`{{Sig_es_:signerN:signature}}`, `{{Dte_es_:signerN:date}}`, `{{Eml_es_:signerN:email}}`), app auto-aligns DocuSeal fields to those exact coordinates.
  - Signature/date placement supports hybrid table behavior (`docuseal.signature_layout_mode=table_preferred`): trace table cell geometry first, run guard checks (`docuseal.signature_table_guard_*`), then per-form fixed map fallback (`docuseal.signature_table_maps`), then generic placeholder geometry.
  - `Eml_es_` placeholders create locked text fields and are prefilled with the signer email used for that signer slot.
  - Template IDs can be isolated by form with `docuseal.template_ids.<template_key_or_doc_type>`. Set `docuseal.strict_template_ids=true` to fail closed if a form is missing an explicit mapping.
- App-owned mail sends signature requests via Graph.
- Compose also includes a local `smtp_graph_bridge` so DocuSeal SMTP traffic can be relayed through Graph when DocuSeal needs to emit mail.
- DocuSeal links are rewritten to public HTTPS origin via `docuseal.public_base_url` when needed.

### Webhook completion
- Endpoint: `POST /webhook/docuseal`
- Verifies webhook auth when `docuseal.webhook_secret` is configured:
  - HMAC body signature via `X-DocuSeal-Signature` / `X-Signature`, or
  - static shared token via `X-Webhook-Token` / `X-DocuSeal-Webhook-Token` / `Authorization: Bearer <secret>`.
- Deduplicates by `webhook_receipts(provider, receipt_key)`.
- Stores artifacts locally and uploads generated/signed/audit files to SharePoint.
- Supports multi-destination audit backups:
  - primary: `graph.audit_subfolder`
  - optional extra SharePoint copies: `graph.audit_backup_subfolders`
  - optional local/NAS mirror copies: `graph.audit_local_backup_roots`
- Always sends completion notifications to internal recipients.
- `email.test_mode=true` prefixes outbound subjects with `[TEST]` and adds a test banner to body content.
- Per-form overrides are supported via `email.test_mode_by_form.<template_key_or_doc_type>`.
- Sends completion notifications to Derek only when `require_approver_decision=true`.
- Moves case to `pending_approval` when `require_approver_decision=true`; otherwise auto-approves.

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
  - each path in `graph.audit_backup_subfolders` (additional audit copies)
  - `graph.client_supplied_subfolder` (created only when `client_supplied_files` are provided)
- Local/NAS audit mirrors:
  - each root in `graph.audit_local_backup_roots` gets:
    - `<root>/<case_parent_folder>/<case_folder>/<audit_subfolder>/<doc_type>_audit.zip`

## 3) Data model (SQLite)

Core tables:
- `cases`
  - `id`, `grievance_id`, `status`, `approval_status`, `grievance_number`, `intake_request_id`, `member_*`, SharePoint folder metadata
- `documents`
  - one row per document per case
  - status lifecycle, template key, signature requirements, DocuSeal ids/links, local file paths, SharePoint URLs
  - `audit_backup_locations_json` stores extra audit copy destinations
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
- Requires `CLOUDFLARE_TUNNEL_TOKEN` to be set when running `cloudflared` service.

### Compose from `grievance-mvp/` (supported)
- File: `grievance-mvp/docker-compose.yml`

### DocuSeal HTTPS forwarding
- `docuseal_proxy` (nginx) forwards Host + `X-Forwarded-*` headers to DocuSeal.
- nginx uses Docker DNS re-resolution (`resolver 127.0.0.11`) so DocuSeal container IP changes do not require proxy restart.
- Cloudflare tunnel can target `docuseal_proxy`.
- `cloudflared` runs in host-network mode so tunnel ingress routes pointing to `localhost` / `127.0.0.1` are valid.
- DocuSeal public URL is kept in sync by `sync-docuseal-public-url.sh`.

### Local SMTP relay for DocuSeal
- `smtp_graph_bridge` accepts SMTP locally and relays to Microsoft Graph `sendMail`.
- `docuseal` is wired to `SMTP_ADDRESS=smtp_graph_bridge` and `SMTP_PORT=${DOCUSEAL_SMTP_PORT}`.
- SMTP reachability is isolated on `smtp-relay-net`, shared only by `docuseal` and `smtp_graph_bridge`.
- Relay reads Graph credentials and sender mailbox from `config/config.yaml`:
  - `graph.tenant_id`, `graph.client_id`, `graph.cert_pem_path`, `graph.cert_thumbprint`
  - `email.sender_user_id`
- Optional env overrides in `.env`:
  - `DOCUSEAL_SMTP_PORT` (default `1025`)
  - `DOCUSEAL_SMTP_FROM` (default `admin@example.org`)
  - `GRAPH_SENDER_USER_ID` (override sender mailbox for relay container only)

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

Sync DocuSeal completion webhook:

```bash
cd grievance-mvp
./sync-docuseal-webhook.sh https://api.cwa3106.org/webhook/docuseal
```

## 6) Autostart + self-heal

Use systemd for boot startup plus a watchdog timer.

Install (Ubuntu host):

```bash
cd grievance-mvp
sudo ./scripts/install-systemd-services.sh
```

What gets installed:
- `grievance-mvp.service` (runs `docker compose up -d` at boot)
- `grievance-mvp-watchdog.timer` (runs every 2 minutes)
- `grievance-mvp-watchdog.service` (calls `scripts/watchdog-restart.sh`)

Watchdog behavior:
- checks `WATCHDOG_HEALTH_URL` (default `http://127.0.0.1:8080/healthz`)
- increments failure counter on each failed check
- auto-runs `docker compose up -d` after `WATCHDOG_FAILURE_THRESHOLD` failures (default `3`)
- enforces cooldown with `WATCHDOG_RESTART_COOLDOWN_SECONDS` (default `600`)
- optionally sends an internal Graph alert email (`WATCHDOG_ALERT_EMAIL=true`)
- optional popup/wall notification (`WATCHDOG_ALERT_POPUP=true`)

Override switch (disable auto-restart):

```bash
cd grievance-mvp
make watchdog-disable
```

Re-enable:

```bash
cd grievance-mvp
make watchdog-enable
```

Manual status/check:

```bash
cd grievance-mvp
make watchdog-status
make watchdog-check
```

Main watchdog env vars (`grievance-mvp/.env`):
- `WATCHDOG_HEALTH_URL`
- `WATCHDOG_FAILURE_THRESHOLD`
- `WATCHDOG_RESTART_COOLDOWN_SECONDS`
- `WATCHDOG_CURL_TIMEOUT_SECONDS`
- `WATCHDOG_POST_RESTART_HEALTH_RETRIES`
- `WATCHDOG_POST_RESTART_HEALTH_DELAY_SECONDS`
- `WATCHDOG_ALERT_EMAIL`
- `WATCHDOG_ALERT_POPUP`

## 7) Smoke tests

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

## 8) Microsoft Graph requirements

Mail (app-owned delivery):
- `Mail.Send`
- `Mail.ReadWrite` (used to create draft and record message id)

SharePoint:
- Prefer `Sites.Selected` with site-scoped grants
- Document library must be reachable by configured site + library name

Least privilege:
- Restrict mailbox scope with Exchange application access policy to the authorized sender mailbox.

## 9) Security notes

- Do not commit secrets.
- Keep secrets in:
  - `grievance-mvp/.env`
  - `grievance-mvp/config/config.yaml`
  - `grievance-mvp/config/graph_cert.pem`
- App logs metadata only (case/document ids, status, message ids), not document contents.

### Intake endpoint hardening (works without HMAC)

`POST /intake` supports optional header-based gating via `intake_auth` config:

```yaml
intake_auth:
  shared_header_name: X-Intake-Key
  shared_header_value: ""
  cloudflare_access_client_id: ""
  cloudflare_access_client_secret: ""
```

`shared_header_value`, `cloudflare_access_client_id`, and `cloudflare_access_client_secret` can also be supplied via env vars:
- `INTAKE_SHARED_HEADER_VALUE`
- `CF_ACCESS_CLIENT_ID`
- `CF_ACCESS_CLIENT_SECRET`

Rules:
- If `shared_header_value` is set, request must include matching `shared_header_name`.
- If Cloudflare values are set, request must include:
  - `CF-Access-Client-Id`
  - `CF-Access-Client-Secret`
- If both methods are configured, both checks are enforced.
- Cloudflare ID/secret must either both be set or both be blank.

## 10) Key API endpoints

- `GET /healthz`
- `POST /intake`
- `GET /cases/{case_id}`
- `POST /cases/{case_id}/grievance-number`
- `POST /webhook/docuseal`
- `POST /cases/{case_id}/notifications/resend`
- `POST /standalone/forms/{form_key}/submissions`
- `GET /standalone/submissions/{submission_id}`
- `POST /standalone/submissions/{submission_id}/notifications/resend`
- `GET /cases/{case_id}/approval`
- `POST /cases/{case_id}/approval`
- `GET /ops` (local/private ops page)
- `GET /ops/cases/{case_id}/trace` (full trace JSON: docs, events, outbound emails)
- `POST /ops/cases/{case_id}/resend-signature` (resend signature request emails for case docs)
- `POST /ops/cases/{case_id}/resubmit` (replay saved intake payload with fresh request_id)

`/ops` endpoints are restricted to loopback/private network clients.

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
- If `intake_auth.shared_header_value` is set, include:
  - `<intake_auth.shared_header_name>: <configured value>`
- If Cloudflare Access service token is configured in `intake_auth`, include:
  - `CF-Access-Client-Id: <token id>`
  - `CF-Access-Client-Secret: <token secret>`

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

## 10a) Document Command Catalog (3g3a excluded)

These are ready for intake integration now:

- `statement_of_occurrence` -> `statement_of_occurrence fixed.docx`
- `bellsouth_meeting_request` -> `Bellsouth-Telecommunications-formal-grievance-meeting-request.docx`
- `mobility_meeting_request` -> `mobility-formal-grievance-meeting-request.docx`
- `grievance_data_request` -> `grievance_data_request_form.docx`
- `true_intent_brief` -> `form_true_intent_grievance_brief_revised_05.20.21.docx`
- `disciplinary_brief` -> `form_disciplinary_grievance_brief_revised_05.19.21.docx`
- `settlement_form` -> `Settlement Form 3106.docx`
- `mobility_record_of_grievance` -> `mobility_record_of_grievance_revised.docx`

Equivalent full command names also work:

- `mobility_formal_grievance_meeting_request`
- `grievance_data_request_form`
- `true_intent_grievance_brief`
- `disciplinary_grievance_brief`
- `settlement_form_3106`
- `mobility_record_of_grievance`

`bst_grievance_form_3g3a` now has a staged integration guide for questions 1-10:
- `docs/power-automate/bst_grievance_form_3g3a.md`
- `docs/power-automate/examples/bst_grievance_form_3g3a.payload.json`

3G3A staged flow behavior:
- Enabled only when `document_policies.bst_grievance_form_3g3a.staged_flow_enabled=true`.
- Requires explicit `documents[0].signers` with 3 emails in order:
  1. local union
  2. second-level manager
  3. union final disposition
- Intake sends stage 1 only; webhook completion auto-advances stages 2 and 3.
- Q6-Q10 are DocuSeal-owned fill fields in stages 2/3 (`Txt_es_` tags), not Power Automate prefill fields.

Mobility record staged flow behavior:
- Enabled only when `document_policies.mobility_record_of_grievance.staged_flow_enabled=true`.
- Requires explicit `documents[0].signers` with 3 emails in order:
  1. union filing signer
  2. company responder
  3. union appeal/final signer
- Intake sends stage 1 only; webhook completion auto-advances stages 2 and 3.
- Stage 1 owns the union first-level position block.
- Stage 2 owns company statement/disposition blocks and both company signature blocks.
- Stage 3 owns the union disposition/final blocks and the union second-level position block.

Minimum payload pattern for all commands:

```json
{
  "request_id": "forms-<unique-response-id>",
  "document_command": "true_intent_brief",
  "contract": "CWA",
  "grievant_firstname": "John",
  "grievant_lastname": "Doe",
  "grievant_email": "john@example.com",
  "narrative": "Base narrative text",
  "template_data": {
    "union_rep_email": "rep@example.com"
  }
}
```

Recommendation:
- Pass every template placeholder value via `template_data` using the exact tag name from the DOCX.
- For meeting-request docs, include `template_data.union_rep_email` so signer routing is deterministic.

Detailed per-document setup guides:
- `docs/power-automate/README.md`
- `docs/power-automate/statement_of_occurrence.md`
- `docs/power-automate/bellsouth_meeting_request.md`
- `docs/power-automate/mobility_meeting_request.md`
- `docs/power-automate/grievance_data_request.md`
- `docs/power-automate/true_intent_grievance_brief.md`
- `docs/power-automate/disciplinary_grievance_brief.md`
- `docs/power-automate/settlement_form_3106.md`
- `docs/power-automate/mobility_record_of_grievance.md`
- `docs/power-automate/att_mobility_bargaining_suggestion.md` (combined Forms + Power Automate handoff)

## 10b) Power Automate setup (BellSouth Meeting Request)

Use:
- `document_command: "bellsouth_meeting_request"`
- `grievance_id`: existing grievance number whose SharePoint folder already exists

Folder resolution behavior for BellSouth:
- Matches only folders named exactly `<grievance_id>` or starting with `<grievance_id> ` under `graph.case_parent_folder`.
- `422`: no matching folder.
- `409`: multiple matching folders (response includes `candidates` list).
- Folder is never auto-created in this mode.

Default signer behavior for BellSouth:
- If `documents[].signers` is provided, that wins.
- Else uses `template_data.union_rep_email`.
- Else falls back to `grievant_email`.
- If no valid signer resolves, signature send is marked failed for that document.

Required/expected `template_data` keys for this template:
- `to`
- `request_date`
- `grievant_names`
- `grievants_attending`
- `grievants_in_attendance`
- `date_grievance_occurred`
- `issue_contract_section`
- `informal_meeting_date`
- `meeting_requested_date`
- `meeting_requested_time`
- `meeting_requested_place`
- `union_rep_email` (default signer source)
- `union_rep_attending`
- `union_reps_in_attendance`
- `company_reps_in_attendance`
- `additional_info`
- `reply_to_name_1`
- `reply_to_name_2`
- `reply_to_address_1`
- `reply_to_address_2`

BellSouth payload example:

```json
{
  "request_id": "forms-@{triggerOutputs()?['body/responseId']}",
  "document_command": "bellsouth_meeting_request",
  "grievance_id": "@{outputs('Get_response_details')?['body/r_grievance_id']}",
  "contract": "BellSouth",
  "grievant_firstname": "@{outputs('Get_response_details')?['body/r_first_name']}",
  "grievant_lastname": "@{outputs('Get_response_details')?['body/r_last_name']}",
  "grievant_email": "@{outputs('Get_response_details')?['body/r_grievant_email']}",
  "grievant_phone": "@{outputs('Get_response_details')?['body/r_phone']}",
  "incident_date": "@{outputs('Get_response_details')?['body/r_incident_date']}",
  "narrative": "@{outputs('Get_response_details')?['body/r_narrative']}",
  "template_data": {
    "union_rep_email": "@{outputs('Get_response_details')?['body/r_union_rep_email']}",
    "to": "@{outputs('Get_response_details')?['body/r_to']}",
    "request_date": "@{outputs('Get_response_details')?['body/r_request_date']}",
    "grievant_names": "@{outputs('Get_response_details')?['body/r_grievant_names']}",
    "date_grievance_occurred": "@{outputs('Get_response_details')?['body/r_date_grievance_occurred']}",
    "issue_contract_section": "@{outputs('Get_response_details')?['body/r_issue_contract_section']}",
    "meeting_requested_date": "@{outputs('Get_response_details')?['body/r_meeting_requested_date']}",
    "meeting_requested_time": "@{outputs('Get_response_details')?['body/r_meeting_requested_time']}",
    "meeting_requested_place": "@{outputs('Get_response_details')?['body/r_meeting_requested_place']}",
    "additional_info": "@{outputs('Get_response_details')?['body/r_additional_info']}"
  }
}
```

## 10c) Single Microsoft Form for BellSouth + Mobility meeting requests

If you want one shared Form/Flow:
- Add a required multiple-choice question, for example `Contract bucket`
- Choices: `BellSouth`, `AT&T Mobility`

In Power Automate (Condition or Switch), map that answer to command + contract:
- `BellSouth` -> `document_command: "bellsouth_meeting_request"`, `contract: "BellSouth"`
- `AT&T Mobility` -> `document_command: "mobility_meeting_request"`, `contract: "AT&T Mobility"`

All other meeting-request fields can stay identical between the two branches.

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
