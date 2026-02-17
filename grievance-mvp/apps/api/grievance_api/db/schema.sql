PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  status TEXT NOT NULL,                  -- created|processing|completed|failed
  member_name TEXT NOT NULL,
  intake_request_id TEXT NOT NULL,       -- idempotency key from client
  intake_payload_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_intake_request_id
ON cases(intake_request_id);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  doc_type TEXT NOT NULL,                -- e.g., 'grievance_form', 'attachment'
  status TEXT NOT NULL,                  -- created|sent_for_signature|signed|pending_approval|approved|failed
  requires_signature BOOLEAN NOT NULL DEFAULT 0,
  signer_order_json TEXT,                -- JSON array of signer emails
  docx_path TEXT,
  pdf_path TEXT,
  pdf_sha256 TEXT,
  docuseal_submission_id TEXT,
  docuseal_signing_link TEXT,
  completed_at_utc TEXT,
  FOREIGN KEY (case_id) REFERENCES cases (id)
);

CREATE INDEX IF NOT EXISTS idx_documents_case_id
ON documents(case_id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  document_id TEXT,
  ts_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_case_id
ON events(case_id);

CREATE TABLE IF NOT EXISTS webhook_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,                -- docuseal
  receipt_key TEXT NOT NULL,             -- idempotency key derived from webhook
  ts_utc TEXT NOT NULL,
  raw_body TEXT NOT NULL,
  handled INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_receipts_provider_key
ON webhook_receipts(provider, receipt_key);
