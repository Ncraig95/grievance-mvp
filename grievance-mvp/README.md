Grievance Intake + e-sign + SharePoint drop (MVP scaffold)

What this is:
- FastAPI orchestrator (self-hosted on Ubuntu, localhost-only)
- DocuSeal (container) for signing (signed PDF + certificate/audit log)
- SQLite for minimal state
- Microsoft Graph app-only auth (certificate-based) for SharePoint uploads + app-owned email delivery

What is NOT complete yet (intentional MVP scaffolding):
- DocuSeal API client: create submission + download completed artifacts (stubs)
- SharePoint upload paths need your exact site + library wiring in config

Email delivery architecture:
- DocuSeal is used for e-sign and audit trail only.
- Do not configure DocuSeal SMTP for this app workflow.
- Outbound email is sent by this app via Microsoft Graph Mail API (`/users/{mailbox}/messages` + `/send`).
- Templates are versioned in repo at `apps/api/templates/email` (`.subject.txt`, `.txt`, optional `.html`).
- Included templates: `signature_request`, `reminder_signature`, `status_update`, `completion_signer`, `completion_internal`, `completion_approval`.
- Per-recipient delivery is audited in SQLite table `outbound_emails` with:
  - `graph_message_id`
  - `internet_message_id`
  - `recipient_email`
  - `idempotency_key`
  - `resend_count`
  - `last_sent_at_utc`
- Completion webhook sends to signer, internal recipients, and Derek (approval recipient) when configured.
- Delivery mode defaults to SharePoint links (`email.artifact_delivery_mode=sharepoint_link`), with optional small-PDF attachment mode.

Manual resend endpoint:
- `POST /grievances/{grievance_id}/notifications/resend`
- Requires JSON body with `template_key` and `idempotency_key`.
- Applies resend cooldown (`email.resend_cooldown_seconds`) to reduce spam.

Graph permissions and least-privilege guidance:
- Mail: `Mail.Send` + `Mail.ReadWrite` application permissions (required to create draft + capture `message_id`).
- SharePoint: prefer `Sites.Selected` with site-specific grants where possible.
- Restrict mailbox scope using Exchange application access policy to the authorized sender mailbox only.
- Do not commit secrets; keep values in ignored files (`.env`, `config/config.yaml`, `config/graph_cert.pem`) or external secret storage.

Run (from repo root so compose path is always correct):
  cd "/home/nicholas-craig/apps/grievance mvp"
  make up
  make ps
  curl -sS http://127.0.0.1:${API_PORT:-8080}/healthz

Equivalent direct run (from grievance-mvp/):
  cd "/home/nicholas-craig/apps/grievance mvp/grievance-mvp"
  docker compose up -d --build

DocuSeal behind Cloudflare (public HTTPS URL + download links):
  # one-time or after host/protocol changes
  cd "/home/nicholas-craig/apps/grievance mvp"
  make sync-docuseal-url

The compose config now sets these DocuSeal env vars:
- `APP_URL=${DOCUSEAL_PROTOCOL}://${DOCUSEAL_HOST}`
- `HOST=${DOCUSEAL_HOST}`
- `FORCE_SSL=true`

This ensures DocuSeal generates absolute URLs from the public domain (not localhost).

Verification:
  # discover a candidate download path (examples: /s/<slug>/download or /submitters/<slug>/download)
  docker exec docuseal_db psql -U docuseal -d docuseal -Atc "SELECT slug FROM submitters ORDER BY id DESC LIMIT 5;"

  # public edge check
  curl -I https://docuseal.cwa3106.org/<download-path>

  # origin check with Host override (bypasses Cloudflare)
  curl -I -H "Host: docuseal.cwa3106.org" http://127.0.0.1:8081/<download-path>

Expected:
- Status `200` (or `404` only when the slug/signature is expired/invalid).
- `Content-Type: application/json` for `/download` index endpoints that return file URL arrays.
- For concrete file URLs, `Content-Type` and `Content-Disposition` headers should be present.

Cloudflare cache bypass (free plan compatible):
- Create a Cache Rule with action: `Bypass cache`.
- Use expression:
  `(http.host eq "docuseal.cwa3106.org" and starts_with(http.request.uri.path, "/submitters/") and contains(http.request.uri.path, "/download")) or (http.host eq "docuseal.cwa3106.org" and starts_with(http.request.uri.path, "/s/") and contains(http.request.uri.path, "/download")) or (http.host eq "docuseal.cwa3106.org" and starts_with(http.request.uri.path, "/file/"))`

Notes:
- All configuration lives in config/config.yaml (single source of truth).
- docker-compose uses `.env` for host bindings, DocuSeal image, and DocuSeal public host/protocol.
