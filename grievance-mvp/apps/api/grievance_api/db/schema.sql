PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS grievances (
  id TEXT PRIMARY KEY,
  created_at_utc TEXT NOT NULL,
  status TEXT NOT NULL,                  -- created|sent_for_signature|completed|failed
  signer_email TEXT NOT NULL,
  signer_lastname TEXT NOT NULL,

  intake_request_id TEXT NOT NULL,       -- idempotency key from client
  intake_payload_json TEXT NOT NULL,

  docx_path TEXT NOT NULL,
  pdf_path TEXT NOT NULL,

  pdf_sha256 TEXT NOT NULL,
  docuseal_submission_id TEXT,           -- set after create submission
  docuseal_signing_link TEXT,            -- optional convenience

  completed_at_utc TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_grievances_intake_request_id
ON grievances(intake_request_id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  grievance_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_grievance_id
ON events(grievance_id);

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
