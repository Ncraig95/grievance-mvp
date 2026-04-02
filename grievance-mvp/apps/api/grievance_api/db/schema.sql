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
  sharepoint_case_web_url TEXT,
  officer_status TEXT,
  officer_assignee TEXT,
  officer_notes TEXT,
  officer_source TEXT,
  officer_closed_at_utc TEXT,
  officer_closed_by TEXT,
  tracking_contract TEXT,
  tracking_department TEXT,
  tracking_steward TEXT,
  tracking_occurrence_date TEXT,
  tracking_issue_summary TEXT,
  tracking_first_level_request_sent_date TEXT,
  tracking_second_level_request_sent_date TEXT,
  tracking_third_level_request_sent_date TEXT,
  tracking_fourth_level_request_sent_date TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_intake_request_id
ON cases(intake_request_id);

CREATE INDEX IF NOT EXISTS idx_cases_grievance_id
ON cases(grievance_id);

CREATE INDEX IF NOT EXISTS idx_cases_officer_status
ON cases(officer_status);

CREATE TABLE IF NOT EXISTS grievance_id_sequences (
  year INTEGER PRIMARY KEY,
  last_seq INTEGER NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standalone_form_sequences (
  form_key TEXT NOT NULL,
  year INTEGER NOT NULL,
  last_seq INTEGER NOT NULL,
  updated_at_utc TEXT NOT NULL,
  PRIMARY KEY (form_key, year)
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
  audit_backup_locations_json TEXT,
  completed_at_utc TEXT,
  FOREIGN KEY (case_id) REFERENCES cases (id)
);

CREATE INDEX IF NOT EXISTS idx_documents_case_id
ON documents(case_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_docuseal_submission
ON documents(docuseal_submission_id);

CREATE INDEX IF NOT EXISTS idx_documents_case_doc_type
ON documents(case_id, doc_type);

CREATE TABLE IF NOT EXISTS standalone_submissions (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  form_key TEXT NOT NULL,
  form_title TEXT NOT NULL,
  signer_email TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  template_data_json TEXT NOT NULL,
  filing_year INTEGER,
  filing_sequence INTEGER,
  filing_label TEXT,
  sharepoint_folder_path TEXT,
  sharepoint_folder_web_url TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_submissions_request_id
ON standalone_submissions(request_id);

CREATE TABLE IF NOT EXISTS standalone_documents (
  id TEXT PRIMARY KEY,
  submission_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  form_key TEXT NOT NULL,
  template_key TEXT,
  status TEXT NOT NULL,
  requires_signature INTEGER NOT NULL DEFAULT 1,
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
  FOREIGN KEY (submission_id) REFERENCES standalone_submissions (id)
);

CREATE INDEX IF NOT EXISTS idx_standalone_documents_submission_id
ON standalone_documents(submission_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_documents_docuseal_submission
ON standalone_documents(docuseal_submission_id);

CREATE TABLE IF NOT EXISTS standalone_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id TEXT NOT NULL,
  document_id TEXT,
  ts_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_standalone_events_submission_id
ON standalone_events(submission_id);

CREATE INDEX IF NOT EXISTS idx_standalone_events_document_id
ON standalone_events(document_id);

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

CREATE TABLE IF NOT EXISTS chief_steward_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  principal_id TEXT,
  principal_email TEXT NOT NULL,
  principal_display_name TEXT,
  contract_scope TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  assigned_by TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chief_steward_assignments_email_scope
ON chief_steward_assignments(principal_email, contract_scope);

CREATE INDEX IF NOT EXISTS idx_chief_steward_assignments_principal_id
ON chief_steward_assignments(principal_id);

CREATE TABLE IF NOT EXISTS external_steward_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  display_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  auth_source TEXT,
  auth_issuer TEXT,
  auth_subject TEXT,
  invited_by TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  last_login_at_utc TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_steward_users_email
ON external_steward_users(email);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_steward_users_subject
ON external_steward_users(auth_issuer, auth_subject);

CREATE TABLE IF NOT EXISTS external_steward_case_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  external_steward_user_id INTEGER NOT NULL,
  case_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  assigned_by TEXT NOT NULL,
  FOREIGN KEY (external_steward_user_id) REFERENCES external_steward_users (id),
  FOREIGN KEY (case_id) REFERENCES cases (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_steward_case_assignments_user_case
ON external_steward_case_assignments(external_steward_user_id, case_id);

CREATE INDEX IF NOT EXISTS idx_external_steward_case_assignments_case
ON external_steward_case_assignments(case_id);

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

CREATE TABLE IF NOT EXISTS standalone_outbound_emails (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id TEXT NOT NULL,
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_outbound_emails_dedup
ON standalone_outbound_emails(submission_id, document_scope_id, template_key, recipient_email, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_standalone_outbound_emails_submission
ON standalone_outbound_emails(submission_id);

CREATE TABLE IF NOT EXISTS document_stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  stage_no INTEGER NOT NULL,
  stage_key TEXT NOT NULL,
  status TEXT NOT NULL,
  signer_email TEXT NOT NULL,
  docuseal_submission_id TEXT,
  docuseal_signing_link TEXT,
  source_payload_json TEXT NOT NULL DEFAULT '{}',
  started_at_utc TEXT NOT NULL,
  completed_at_utc TEXT,
  failed_at_utc TEXT,
  UNIQUE(document_id, stage_no),
  FOREIGN KEY (case_id) REFERENCES cases (id),
  FOREIGN KEY (document_id) REFERENCES documents (id)
);

CREATE INDEX IF NOT EXISTS idx_document_stages_submission_id
ON document_stages(docuseal_submission_id);

CREATE INDEX IF NOT EXISTS idx_document_stages_document_id
ON document_stages(document_id);

CREATE TABLE IF NOT EXISTS document_stage_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_stage_id INTEGER NOT NULL,
  artifact_type TEXT NOT NULL,
  storage_backend TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (document_stage_id) REFERENCES document_stages (id)
);

CREATE INDEX IF NOT EXISTS idx_document_stage_artifacts_stage_type
ON document_stage_artifacts(document_stage_id, artifact_type);

CREATE TABLE IF NOT EXISTS document_stage_field_values (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_stage_id INTEGER NOT NULL,
  field_key TEXT NOT NULL,
  field_value TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (document_stage_id) REFERENCES document_stages (id)
);

CREATE INDEX IF NOT EXISTS idx_document_stage_field_values_stage
ON document_stage_field_values(document_stage_id);
