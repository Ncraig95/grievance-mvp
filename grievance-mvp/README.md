Grievance Intake + e-sign + SharePoint drop (MVP scaffold)

What this is:
- FastAPI orchestrator (self-hosted on Ubuntu, localhost-only)
- DocuSeal (container) for signing (signed PDF + certificate/audit log)
- SQLite for minimal state
- Microsoft Graph app-only auth (certificate-based) for SharePoint uploads

What is NOT complete yet (intentional MVP scaffolding):
- DocuSeal API client: create submission + download completed artifacts (stubs)
- DocuSeal webhook verification (stub)
- SharePoint upload paths need your exact site + library wiring in config

Run (after Docker is installed and you set DOCUSEAL_IMAGE):
  docker compose up -d --build
  curl -sS http://127.0.0.1:${API_PORT:-8080}/healthz

Notes:
- All configuration lives in config/config.yaml (single source of truth).
- docker-compose only uses .env for host port bindings and the DocuSeal image.
