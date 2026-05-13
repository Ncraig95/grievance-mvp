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

CREATE TABLE IF NOT EXISTS hosted_form_settings (
  form_key TEXT PRIMARY KEY,
  visibility TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
  setting_key TEXT PRIMARY KEY,
  setting_json TEXT NOT NULL,
  updated_by TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  assignee TEXT,
  officer_notes TEXT,
  paid INTEGER NOT NULL DEFAULT 0,
  paid_at_utc TEXT,
  reminder_due_at_utc TEXT NOT NULL,
  reminder_attempted_at_utc TEXT,
  reminder_sent_at_utc TEXT,
  reminder_error TEXT,
  referrer_name TEXT NOT NULL,
  referrer_address TEXT NOT NULL,
  referrer_phone TEXT NOT NULL,
  referrer_email TEXT,
  referrer_group TEXT NOT NULL,
  referred_name TEXT NOT NULL,
  referred_group TEXT,
  referred_att_uid TEXT,
  referral_notes TEXT,
  submitter_ip_hash TEXT,
  submitter_user_agent_hash TEXT,
  source_payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_referrals_request_id
ON referrals(request_id);

CREATE INDEX IF NOT EXISTS idx_referrals_status_due
ON referrals(status, reminder_due_at_utc);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer_group
ON referrals(referrer_group);

CREATE INDEX IF NOT EXISTS idx_referrals_referred_group
ON referrals(referred_group);

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

CREATE TABLE IF NOT EXISTS officer_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  principal_id TEXT,
  principal_email TEXT NOT NULL,
  principal_display_name TEXT,
  officer_title TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_officer_profiles_email
ON officer_profiles(principal_email);

CREATE INDEX IF NOT EXISTS idx_officer_profiles_principal_id
ON officer_profiles(principal_id);

CREATE TABLE IF NOT EXISTS internal_role_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  principal_id TEXT,
  principal_email TEXT NOT NULL,
  principal_display_name TEXT,
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  assigned_by TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_internal_role_assignments_email_role
ON internal_role_assignments(principal_email, role);

CREATE INDEX IF NOT EXISTS idx_internal_role_assignments_principal_id
ON internal_role_assignments(principal_id);

CREATE INDEX IF NOT EXISTS idx_internal_role_assignments_status
ON internal_role_assignments(status);

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

CREATE TABLE IF NOT EXISTS outreach_contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  first_name TEXT,
  last_name TEXT,
  full_name TEXT,
  work_location TEXT,
  work_group TEXT,
  group_name TEXT,
  subgroup_name TEXT,
  department TEXT,
  bargaining_unit TEXT,
  local_number TEXT,
  steward_name TEXT,
  rep_name TEXT,
  membership_type TEXT,
  employment_status TEXT,
  status_detail TEXT,
  status_bucket TEXT,
  status_source_text TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  notes TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  extra_fields_json TEXT NOT NULL DEFAULT '{}',
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_contacts_email
ON outreach_contacts(email);

CREATE INDEX IF NOT EXISTS idx_outreach_contacts_location
ON outreach_contacts(work_location);

CREATE INDEX IF NOT EXISTS idx_outreach_contacts_work_group
ON outreach_contacts(work_group);

CREATE INDEX IF NOT EXISTS idx_outreach_contacts_group_name
ON outreach_contacts(group_name);

CREATE INDEX IF NOT EXISTS idx_outreach_contacts_subgroup_name
ON outreach_contacts(subgroup_name);

CREATE INDEX IF NOT EXISTS idx_outreach_contacts_status_bucket
ON outreach_contacts(status_bucket);

CREATE TABLE IF NOT EXISTS outreach_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_key TEXT NOT NULL,
  name TEXT NOT NULL,
  template_type TEXT NOT NULL,
  subject_template TEXT NOT NULL,
  body_template TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  seeded INTEGER NOT NULL DEFAULT 0,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_templates_key
ON outreach_templates(template_key);

CREATE TABLE IF NOT EXISTS outreach_stops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  location_name TEXT NOT NULL,
  visit_date_local TEXT NOT NULL,
  start_time_local TEXT NOT NULL,
  end_time_local TEXT NOT NULL,
  timezone TEXT NOT NULL,
  audience_location TEXT,
  audience_work_group TEXT,
  audience_group_name TEXT,
  audience_subgroup_name TEXT,
  audience_status_bucket TEXT,
  notice_subject TEXT,
  reminder_subject TEXT,
  notice_send_at_utc TEXT NOT NULL,
  reminder_send_at_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_stops_unique
ON outreach_stops(location_name, visit_date_local);

CREATE INDEX IF NOT EXISTS idx_outreach_stops_status_notice
ON outreach_stops(status, notice_send_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_stops_status_reminder
ON outreach_stops(status, reminder_send_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_stops_status_bucket
ON outreach_stops(status, audience_status_bucket);

CREATE INDEX IF NOT EXISTS idx_outreach_stops_group_name
ON outreach_stops(status, audience_group_name);

CREATE INDEX IF NOT EXISTS idx_outreach_stops_subgroup_name
ON outreach_stops(status, audience_subgroup_name);

CREATE TABLE IF NOT EXISTS outreach_import_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  header_fingerprint TEXT NOT NULL,
  normalized_headers_json TEXT NOT NULL DEFAULT '[]',
  mapping_json TEXT NOT NULL DEFAULT '{}',
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_import_profiles_fingerprint
ON outreach_import_profiles(header_fingerprint);

CREATE TABLE IF NOT EXISTS outreach_suppressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  contact_id INTEGER,
  reason TEXT NOT NULL DEFAULT 'unsubscribe',
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (contact_id) REFERENCES outreach_contacts (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_suppressions_email
ON outreach_suppressions(email);

CREATE TABLE IF NOT EXISTS outreach_send_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stop_id INTEGER,
  template_id INTEGER,
  contact_id INTEGER,
  recipient_email TEXT NOT NULL,
  email_type TEXT NOT NULL,
  subject TEXT NOT NULL,
  text_body TEXT NOT NULL,
  html_body TEXT,
  merge_data_json TEXT NOT NULL DEFAULT '{}',
  scheduled_for_utc TEXT,
  attempted_at_utc TEXT,
  sent_at_utc TEXT,
  failed_at_utc TEXT,
  status TEXT NOT NULL,
  graph_message_id TEXT,
  internet_message_id TEXT,
  error_text TEXT,
  unsubscribe_token_hash TEXT,
  open_token_hash TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  FOREIGN KEY (stop_id) REFERENCES outreach_stops (id),
  FOREIGN KEY (template_id) REFERENCES outreach_templates (id),
  FOREIGN KEY (contact_id) REFERENCES outreach_contacts (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_send_log_unique_delivery
ON outreach_send_log(stop_id, email_type, recipient_email)
WHERE email_type IN ('notice', 'reminder');

CREATE INDEX IF NOT EXISTS idx_outreach_send_log_stop
ON outreach_send_log(stop_id, created_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_send_log_contact
ON outreach_send_log(contact_id, created_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_send_log_open_token
ON outreach_send_log(open_token_hash);

CREATE TABLE IF NOT EXISTS outreach_tracked_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  send_log_id INTEGER NOT NULL,
  destination_url TEXT NOT NULL,
  tracking_token_hash TEXT NOT NULL,
  link_label TEXT,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (send_log_id) REFERENCES outreach_send_log (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_tracked_links_token
ON outreach_tracked_links(tracking_token_hash);

CREATE INDEX IF NOT EXISTS idx_outreach_tracked_links_send
ON outreach_tracked_links(send_log_id, created_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_tracked_links_destination
ON outreach_tracked_links(destination_url);

CREATE TABLE IF NOT EXISTS outreach_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  send_log_id INTEGER NOT NULL,
  tracked_link_id INTEGER,
  event_type TEXT NOT NULL,
  occurred_at_utc TEXT NOT NULL,
  ip_hash TEXT,
  user_agent_hash TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (send_log_id) REFERENCES outreach_send_log (id),
  FOREIGN KEY (tracked_link_id) REFERENCES outreach_tracked_links (id)
);

CREATE INDEX IF NOT EXISTS idx_outreach_events_send
ON outreach_events(send_log_id, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_events_type
ON outreach_events(event_type, occurred_at_utc);

CREATE INDEX IF NOT EXISTS idx_outreach_events_link
ON outreach_events(tracked_link_id, occurred_at_utc);

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

CREATE TABLE IF NOT EXISTS pay_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'guest',
  status TEXT NOT NULL DEFAULT 'active',
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  invited_by TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_users_email
ON pay_users(email);

CREATE INDEX IF NOT EXISTS idx_pay_users_status
ON pay_users(status);

CREATE TABLE IF NOT EXISTS pay_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  principal_id TEXT,
  principal_email TEXT NOT NULL,
  principal_display_name TEXT,
  pay_basis TEXT NOT NULL DEFAULT 'expense_only',
  base_wage_input_type TEXT NOT NULL DEFAULT 'hourly',
  base_wage_amount REAL NOT NULL DEFAULT 0,
  weekly_basis_hours REAL NOT NULL DEFAULT 40,
  commission_month_1_amount REAL NOT NULL DEFAULT 0,
  commission_month_2_amount REAL NOT NULL DEFAULT 0,
  commission_month_3_amount REAL NOT NULL DEFAULT 0,
  commission_average_monthly REAL NOT NULL DEFAULT 0,
  commission_hourly_rate REAL NOT NULL DEFAULT 0,
  calculated_hourly_rate REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  updated_by TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_profiles_email
ON pay_profiles(principal_email);

CREATE INDEX IF NOT EXISTS idx_pay_profiles_principal_id
ON pay_profiles(principal_id);

CREATE INDEX IF NOT EXISTS idx_pay_profiles_status
ON pay_profiles(status);

CREATE TABLE IF NOT EXISTS pay_periods (
  id TEXT PRIMARY KEY,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  revision INTEGER NOT NULL DEFAULT 1,
  locked_by TEXT,
  locked_at_utc TEXT,
  completed_at_utc TEXT,
  president_email TEXT,
  sharepoint_folder_path TEXT,
  sharepoint_folder_web_url TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_periods_range_revision
ON pay_periods(period_start, period_end, revision);

CREATE INDEX IF NOT EXISTS idx_pay_periods_status
ON pay_periods(status);

CREATE TABLE IF NOT EXISTS pay_wage_scales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  effective_date TEXT NOT NULL,
  weekly_basis_hours REAL NOT NULL,
  target_scale TEXT NOT NULL DEFAULT '36',
  actual_scale TEXT NOT NULL DEFAULT 'base',
  target_weekly_amount REAL NOT NULL,
  actual_weekly_amount REAL,
  target_multiplier REAL NOT NULL DEFAULT 1.2,
  notes TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  updated_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_pay_wage_scales_effective
ON pay_wage_scales(effective_date, weekly_basis_hours);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_wage_scales_unique_row
ON pay_wage_scales(effective_date, weekly_basis_hours, target_scale, actual_scale);

CREATE TABLE IF NOT EXISTS pay_irs_rate_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rate_year TEXT NOT NULL,
  effective_date TEXT NOT NULL,
  cents_per_mile REAL NOT NULL,
  rate_per_mile REAL NOT NULL,
  source_url TEXT NOT NULL,
  source_title TEXT,
  detected_at_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  approved_by TEXT,
  approved_at_utc TEXT,
  updated_at_utc TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_irs_rate_candidates_unique
ON pay_irs_rate_candidates(rate_year, effective_date, cents_per_mile, source_url);

CREATE INDEX IF NOT EXISTS idx_pay_irs_rate_candidates_status_year
ON pay_irs_rate_candidates(status, rate_year);

CREATE TABLE IF NOT EXISTS pay_compensation_stubs (
  id TEXT PRIMARY KEY,
  user_email TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  base_wage_input_type TEXT NOT NULL DEFAULT 'hourly',
  base_wage_amount REAL NOT NULL DEFAULT 0,
  weekly_basis_hours REAL NOT NULL DEFAULT 40,
  commission_month_1_amount REAL NOT NULL DEFAULT 0,
  commission_month_2_amount REAL NOT NULL DEFAULT 0,
  commission_month_3_amount REAL NOT NULL DEFAULT 0,
  commission_average_monthly REAL NOT NULL DEFAULT 0,
  commission_hourly_rate REAL NOT NULL DEFAULT 0,
  calculated_hourly_rate REAL NOT NULL DEFAULT 0,
  original_filename TEXT NOT NULL,
  stored_filename TEXT NOT NULL,
  local_path TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  scan_status TEXT NOT NULL,
  scan_result TEXT NOT NULL,
  sharepoint_url TEXT,
  sharepoint_path TEXT,
  notes TEXT,
  created_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pay_compensation_stubs_user_date
ON pay_compensation_stubs(user_email, created_at_utc);

CREATE TABLE IF NOT EXISTS pay_entries (
  id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  user_email TEXT NOT NULL,
  display_name TEXT,
  entry_date TEXT NOT NULL,
  local_number TEXT,
  address TEXT,
  hourly_rate REAL NOT NULL DEFAULT 0,
  lost_wage_input_type TEXT NOT NULL DEFAULT 'hourly',
  lost_wage_amount REAL NOT NULL DEFAULT 0,
  lost_wage_hourly_rate REAL NOT NULL DEFAULT 0,
  compensation_stub_id TEXT,
  hours REAL NOT NULL DEFAULT 0,
  mileage_miles REAL NOT NULL DEFAULT 0,
  mileage_rate REAL NOT NULL DEFAULT 0,
  mileage_amount REAL NOT NULL DEFAULT 0,
  rentals_amount REAL NOT NULL DEFAULT 0,
  meals_amount REAL NOT NULL DEFAULT 0,
  hotel_amount REAL NOT NULL DEFAULT 0,
  miscellaneous_amount REAL NOT NULL DEFAULT 0,
  president_diff_hours REAL NOT NULL DEFAULT 0,
  president_diff_rate REAL NOT NULL DEFAULT 0,
  president_diff_amount REAL NOT NULL DEFAULT 0,
  wage_scale_id INTEGER,
  notes TEXT,
  locked_at_utc TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  FOREIGN KEY (period_id) REFERENCES pay_periods (id),
  FOREIGN KEY (wage_scale_id) REFERENCES pay_wage_scales (id),
  FOREIGN KEY (compensation_stub_id) REFERENCES pay_compensation_stubs (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_entries_period_user_date
ON pay_entries(period_id, user_email, entry_date);

CREATE INDEX IF NOT EXISTS idx_pay_entries_period
ON pay_entries(period_id);

CREATE INDEX IF NOT EXISTS idx_pay_entries_user_date
ON pay_entries(user_email, entry_date);

CREATE INDEX IF NOT EXISTS idx_pay_entries_compensation_stub
ON pay_entries(compensation_stub_id);

CREATE TABLE IF NOT EXISTS pay_attachments (
  id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  attachment_type TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  stored_filename TEXT NOT NULL,
  local_path TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  scan_status TEXT NOT NULL,
  scan_result TEXT NOT NULL,
  sharepoint_url TEXT,
  sharepoint_path TEXT,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (period_id) REFERENCES pay_periods (id),
  FOREIGN KEY (entry_id) REFERENCES pay_entries (id)
);

CREATE INDEX IF NOT EXISTS idx_pay_attachments_entry
ON pay_attachments(entry_id);

CREATE INDEX IF NOT EXISTS idx_pay_attachments_period
ON pay_attachments(period_id);

CREATE TABLE IF NOT EXISTS pay_packets (
  id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  voucher_paths_json TEXT NOT NULL DEFAULT '[]',
  voucher_pdf_paths_json TEXT NOT NULL DEFAULT '[]',
  unsigned_packet_path TEXT,
  unsigned_packet_sha256 TEXT,
  docuseal_submission_id TEXT,
  docuseal_signing_link TEXT,
  signed_packet_path TEXT,
  audit_zip_path TEXT,
  sharepoint_unsigned_url TEXT,
  sharepoint_signed_url TEXT,
  sharepoint_audit_url TEXT,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  completed_at_utc TEXT,
  FOREIGN KEY (period_id) REFERENCES pay_periods (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_packets_period_revision
ON pay_packets(period_id, revision);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_packets_docuseal_submission
ON pay_packets(docuseal_submission_id);

CREATE TABLE IF NOT EXISTS pay_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_id TEXT,
  entry_id TEXT,
  packet_id TEXT,
  ts_utc TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (period_id) REFERENCES pay_periods (id),
  FOREIGN KEY (entry_id) REFERENCES pay_entries (id),
  FOREIGN KEY (packet_id) REFERENCES pay_packets (id)
);

CREATE INDEX IF NOT EXISTS idx_pay_events_period
ON pay_events(period_id, ts_utc);

CREATE INDEX IF NOT EXISTS idx_pay_events_packet
ON pay_events(packet_id, ts_utc);

CREATE TABLE IF NOT EXISTS pay_demo_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at_utc TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_display_name TEXT,
  actor_role TEXT NOT NULL,
  demo_step INTEGER NOT NULL DEFAULT 0,
  demo_cycle_title TEXT,
  screen TEXT NOT NULL DEFAULT 'demo',
  category TEXT NOT NULL DEFAULT 'suggestion',
  comment TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE INDEX IF NOT EXISTS idx_pay_demo_feedback_status_created
ON pay_demo_feedback(status, created_at_utc);

CREATE INDEX IF NOT EXISTS idx_pay_demo_feedback_actor
ON pay_demo_feedback(actor_email, created_at_utc);
