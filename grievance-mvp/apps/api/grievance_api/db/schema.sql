PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  grievance_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  approval_status TEXT NOT NULL DEFAULT 'pending',
  approver_email TEXT,
  approved_at_utc TEXT,
  approval_notes TEXT,
  grievance_number TEXT,
  member_name TEXT NOT NULL,
  member_email TEXT,
  intake_request_id TEXT NOT NULL,
  intake_payload_json TEXT NOT NULL,
  sharepoint_case_folder TEXT,
  sharepoint_case_web_url TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_intake_request_id
ON cases(intake_request_id);

CREATE INDEX IF NOT EXISTS idx_cases_grievance_id
ON cases(grievance_id);

CREATE TABLE IF NOT EXISTS grievance_id_sequences (
  year INTEGER PRIMARY KEY,
  last_seq INTEGER NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  doc_type TEXT NOT NULL,
  template_key TEXT,
  status TEXT NOT NULL,
  requires_signature INTEGER NOT NULL DEFAULT 0,
  signer_order_json TEXT,
  docx_path TEXT,
  pdf_path TEXT,
  pdf_sha256 TEXT,
  docuseal_submission_id TEXT,
  docuseal_signing_link TEXT,
  signed_pdf_path TEXT,
  audit_zip_path TEXT,
  sharepoint_generated_url TEXT,
  sharepoint_signed_url TEXT,
  sharepoint_audit_url TEXT,
  completed_at_utc TEXT,
  FOREIGN KEY (case_id) REFERENCES cases (id)
);

CREATE INDEX IF NOT EXISTS idx_documents_case_id
ON documents(case_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_docuseal_submission
ON documents(docuseal_submission_id);

CREATE INDEX IF NOT EXISTS idx_documents_case_doc_type
ON documents(case_id, doc_type);

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

CREATE INDEX IF NOT EXISTS idx_events_document_id
ON events(document_id);

CREATE TABLE IF NOT EXISTS webhook_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  receipt_key TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  raw_body TEXT NOT NULL,
  handled INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_receipts_provider_key
ON webhook_receipts(provider, receipt_key);

CREATE TABLE IF NOT EXISTS outbound_emails (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  document_scope_id TEXT NOT NULL DEFAULT '',
  template_key TEXT NOT NULL,
  recipient_email TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL,
  graph_message_id TEXT,
  internet_message_id TEXT,
  resend_count INTEGER NOT NULL DEFAULT 0,
  created_at_utc TEXT NOT NULL,
  last_sent_at_utc TEXT,
  updated_at_utc TEXT NOT NULL,
  metadata_json TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_emails_dedup
ON outbound_emails(case_id, document_scope_id, template_key, recipient_email, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_outbound_emails_case
ON outbound_emails(case_id);
