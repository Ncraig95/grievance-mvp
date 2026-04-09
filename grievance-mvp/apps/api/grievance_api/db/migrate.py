from __future__ import annotations

import pathlib
import sqlite3


SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    cur = con.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in cur.fetchall()}


def _ensure_column(con: sqlite3.Connection, table: str, column_name: str, column_sql: str) -> None:
    cols = _table_columns(con, table)
    if column_name in cols:
        return
    con.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_sql}")


def _safe_executescript(con: sqlite3.Connection, schema: str) -> None:
    statements = [stmt.strip() for stmt in schema.split(";") if stmt.strip()]
    for stmt in statements:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError as exc:
            text = str(exc)
            # Existing legacy tables can break new index creation until columns are added.
            if "no such column" in text or "already exists" in text:
                continue
            raise


def migrate(db_path: str) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    con = sqlite3.connect(db_path)
    try:
        _safe_executescript(con, schema)

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS grievance_id_sequences (
              year INTEGER PRIMARY KEY,
              last_seq INTEGER NOT NULL,
              updated_at_utc TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS standalone_form_sequences (
              form_key TEXT NOT NULL,
              year INTEGER NOT NULL,
              last_seq INTEGER NOT NULL,
              updated_at_utc TEXT NOT NULL,
              PRIMARY KEY (form_key, year)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS hosted_form_settings (
              form_key TEXT PRIMARY KEY,
              visibility TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_by TEXT,
              updated_at_utc TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
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
              UNIQUE(document_id, stage_no)
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS document_stage_artifacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_stage_id INTEGER NOT NULL,
              artifact_type TEXT NOT NULL,
              storage_backend TEXT NOT NULL,
              storage_path TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              size_bytes INTEGER NOT NULL DEFAULT 0,
              created_at_utc TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS document_stage_field_values (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_stage_id INTEGER NOT NULL,
              field_key TEXT NOT NULL,
              field_value TEXT NOT NULL,
              created_at_utc TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS standalone_submissions (
              id TEXT PRIMARY KEY,
              request_id TEXT NOT NULL,
              form_key TEXT NOT NULL,
              form_title TEXT NOT NULL,
              signer_email TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at_utc TEXT NOT NULL,
              template_data_json TEXT NOT NULL,
              sharepoint_folder_path TEXT,
              sharepoint_folder_web_url TEXT
            )
            """
        )

        con.execute(
            """
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
              completed_at_utc TEXT
            )
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS standalone_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              submission_id TEXT NOT NULL,
              document_id TEXT,
              ts_utc TEXT NOT NULL,
              event_type TEXT NOT NULL,
              details_json TEXT NOT NULL
            )
            """
        )

        con.execute(
            """
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
            )
            """
        )

        # Backwards-compatible column evolution
        _ensure_column(con, "cases", "grievance_id", "TEXT")
        _ensure_column(con, "cases", "approval_status", "TEXT NOT NULL DEFAULT 'pending'")
        _ensure_column(con, "cases", "approver_email", "TEXT")
        _ensure_column(con, "cases", "approved_at_utc", "TEXT")
        _ensure_column(con, "cases", "approval_notes", "TEXT")
        _ensure_column(con, "cases", "grievance_number", "TEXT")
        _ensure_column(con, "cases", "member_email", "TEXT")
        _ensure_column(con, "cases", "sharepoint_case_folder", "TEXT")
        _ensure_column(con, "cases", "sharepoint_case_web_url", "TEXT")
        _ensure_column(con, "cases", "officer_status", "TEXT")
        _ensure_column(con, "cases", "officer_assignee", "TEXT")
        _ensure_column(con, "cases", "officer_notes", "TEXT")
        _ensure_column(con, "cases", "officer_source", "TEXT")
        _ensure_column(con, "cases", "officer_closed_at_utc", "TEXT")
        _ensure_column(con, "cases", "officer_closed_by", "TEXT")
        _ensure_column(con, "cases", "tracking_contract", "TEXT")
        _ensure_column(con, "cases", "tracking_department", "TEXT")
        _ensure_column(con, "cases", "tracking_steward", "TEXT")
        _ensure_column(con, "cases", "tracking_occurrence_date", "TEXT")
        _ensure_column(con, "cases", "tracking_issue_summary", "TEXT")
        _ensure_column(con, "cases", "tracking_first_level_request_sent_date", "TEXT")
        _ensure_column(con, "cases", "tracking_second_level_request_sent_date", "TEXT")
        _ensure_column(con, "cases", "tracking_third_level_request_sent_date", "TEXT")
        _ensure_column(con, "cases", "tracking_fourth_level_request_sent_date", "TEXT")

        _ensure_column(con, "chief_steward_assignments", "principal_id", "TEXT")
        _ensure_column(con, "chief_steward_assignments", "principal_email", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "chief_steward_assignments", "principal_display_name", "TEXT")
        _ensure_column(con, "chief_steward_assignments", "contract_scope", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "chief_steward_assignments", "created_at_utc", "TEXT")
        _ensure_column(con, "chief_steward_assignments", "updated_at_utc", "TEXT")
        _ensure_column(con, "chief_steward_assignments", "assigned_by", "TEXT")

        _ensure_column(con, "external_steward_users", "email", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "external_steward_users", "display_name", "TEXT")
        _ensure_column(con, "external_steward_users", "status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(con, "external_steward_users", "auth_source", "TEXT")
        _ensure_column(con, "external_steward_users", "auth_issuer", "TEXT")
        _ensure_column(con, "external_steward_users", "auth_subject", "TEXT")
        _ensure_column(con, "external_steward_users", "invited_by", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "external_steward_users", "created_at_utc", "TEXT")
        _ensure_column(con, "external_steward_users", "updated_at_utc", "TEXT")
        _ensure_column(con, "external_steward_users", "last_login_at_utc", "TEXT")

        _ensure_column(con, "external_steward_case_assignments", "external_steward_user_id", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(con, "external_steward_case_assignments", "case_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "external_steward_case_assignments", "created_at_utc", "TEXT")
        _ensure_column(con, "external_steward_case_assignments", "updated_at_utc", "TEXT")
        _ensure_column(con, "external_steward_case_assignments", "assigned_by", "TEXT NOT NULL DEFAULT ''")

        _ensure_column(con, "documents", "template_key", "TEXT")
        _ensure_column(con, "documents", "signed_pdf_path", "TEXT")
        _ensure_column(con, "documents", "audit_zip_path", "TEXT")
        _ensure_column(con, "documents", "sharepoint_generated_url", "TEXT")
        _ensure_column(con, "documents", "sharepoint_signed_url", "TEXT")
        _ensure_column(con, "documents", "sharepoint_audit_url", "TEXT")
        _ensure_column(con, "documents", "audit_backup_locations_json", "TEXT")

        _ensure_column(con, "standalone_submissions", "request_id", "TEXT")
        _ensure_column(con, "standalone_submissions", "form_key", "TEXT")
        _ensure_column(con, "standalone_submissions", "form_title", "TEXT")
        _ensure_column(con, "standalone_submissions", "signer_email", "TEXT")
        _ensure_column(con, "standalone_submissions", "status", "TEXT")
        _ensure_column(con, "standalone_submissions", "created_at_utc", "TEXT")
        _ensure_column(con, "standalone_submissions", "template_data_json", "TEXT")
        _ensure_column(con, "standalone_submissions", "filing_year", "INTEGER")
        _ensure_column(con, "standalone_submissions", "filing_sequence", "INTEGER")
        _ensure_column(con, "standalone_submissions", "filing_label", "TEXT")
        _ensure_column(con, "standalone_submissions", "sharepoint_folder_path", "TEXT")
        _ensure_column(con, "standalone_submissions", "sharepoint_folder_web_url", "TEXT")

        _ensure_column(con, "standalone_documents", "submission_id", "TEXT")
        _ensure_column(con, "standalone_documents", "created_at_utc", "TEXT")
        _ensure_column(con, "standalone_documents", "form_key", "TEXT")
        _ensure_column(con, "standalone_documents", "template_key", "TEXT")
        _ensure_column(con, "standalone_documents", "status", "TEXT")
        _ensure_column(con, "standalone_documents", "requires_signature", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(con, "standalone_documents", "signer_order_json", "TEXT")
        _ensure_column(con, "standalone_documents", "docx_path", "TEXT")
        _ensure_column(con, "standalone_documents", "pdf_path", "TEXT")
        _ensure_column(con, "standalone_documents", "pdf_sha256", "TEXT")
        _ensure_column(con, "standalone_documents", "docuseal_submission_id", "TEXT")
        _ensure_column(con, "standalone_documents", "docuseal_signing_link", "TEXT")
        _ensure_column(con, "standalone_documents", "signed_pdf_path", "TEXT")
        _ensure_column(con, "standalone_documents", "audit_zip_path", "TEXT")
        _ensure_column(con, "standalone_documents", "sharepoint_generated_url", "TEXT")
        _ensure_column(con, "standalone_documents", "sharepoint_signed_url", "TEXT")
        _ensure_column(con, "standalone_documents", "sharepoint_audit_url", "TEXT")
        _ensure_column(con, "standalone_documents", "completed_at_utc", "TEXT")

        _ensure_column(con, "standalone_events", "submission_id", "TEXT")
        _ensure_column(con, "standalone_events", "document_id", "TEXT")

        _ensure_column(con, "standalone_outbound_emails", "submission_id", "TEXT")
        _ensure_column(con, "standalone_outbound_emails", "document_scope_id", "TEXT NOT NULL DEFAULT ''")

        _ensure_column(con, "hosted_form_settings", "form_key", "TEXT")
        _ensure_column(con, "hosted_form_settings", "visibility", "TEXT NOT NULL DEFAULT 'public'")
        _ensure_column(con, "hosted_form_settings", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(con, "hosted_form_settings", "updated_by", "TEXT")
        _ensure_column(con, "hosted_form_settings", "updated_at_utc", "TEXT")

        doc_cols = _table_columns(con, "documents")
        if "pdf_sha256" not in doc_cols and "pdf_sa256" in doc_cols:
            _ensure_column(con, "documents", "pdf_sha256", "TEXT")
            con.execute("UPDATE documents SET pdf_sha256=pdf_sa256 WHERE pdf_sha256 IS NULL")

        _ensure_column(con, "events", "case_id", "TEXT")
        _ensure_column(con, "events", "document_id", "TEXT")
        event_cols = _table_columns(con, "events")
        if "grievance_id" in event_cols and "case_id" in event_cols:
            con.execute(
                "UPDATE events SET case_id=grievance_id WHERE (case_id IS NULL OR case_id='') AND grievance_id IS NOT NULL"
            )

        _ensure_column(con, "outbound_emails", "case_id", "TEXT")
        _ensure_column(con, "outbound_emails", "document_scope_id", "TEXT NOT NULL DEFAULT ''")

        out_cols = _table_columns(con, "outbound_emails")
        if "grievance_id" in out_cols and "case_id" in out_cols:
            con.execute(
                "UPDATE outbound_emails SET case_id=grievance_id "
                "WHERE (case_id IS NULL OR case_id='') AND grievance_id IS NOT NULL"
            )

        con.execute("UPDATE cases SET grievance_id=id WHERE grievance_id IS NULL OR grievance_id='' ")

        # Ensure indexes after all columns are present.
        index_sql = [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cases_intake_request_id ON cases(intake_request_id)",
            "CREATE INDEX IF NOT EXISTS idx_cases_grievance_id ON cases(grievance_id)",
            "CREATE INDEX IF NOT EXISTS idx_cases_officer_status ON cases(officer_status)",
            "CREATE INDEX IF NOT EXISTS idx_documents_case_id ON documents(case_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_docuseal_submission ON documents(docuseal_submission_id)",
            "DROP INDEX IF EXISTS idx_documents_case_doc_type",
            "CREATE INDEX IF NOT EXISTS idx_documents_case_doc_type ON documents(case_id, doc_type)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_submissions_request_id ON standalone_submissions(request_id)",
            "CREATE INDEX IF NOT EXISTS idx_standalone_documents_submission_id ON standalone_documents(submission_id)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_documents_docuseal_submission "
                "ON standalone_documents(docuseal_submission_id)"
            ),
            "CREATE INDEX IF NOT EXISTS idx_events_case_id ON events(case_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_document_id ON events(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_standalone_events_submission_id ON standalone_events(submission_id)",
            "CREATE INDEX IF NOT EXISTS idx_standalone_events_document_id ON standalone_events(document_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_receipts_provider_key ON webhook_receipts(provider, receipt_key)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_emails_dedup "
                "ON outbound_emails(case_id, document_scope_id, template_key, recipient_email, idempotency_key)"
            ),
            "CREATE INDEX IF NOT EXISTS idx_outbound_emails_case ON outbound_emails(case_id)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_standalone_outbound_emails_dedup "
                "ON standalone_outbound_emails(submission_id, document_scope_id, template_key, recipient_email, idempotency_key)"
            ),
            "CREATE INDEX IF NOT EXISTS idx_standalone_outbound_emails_submission ON standalone_outbound_emails(submission_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_document_stages_doc_stage ON document_stages(document_id, stage_no)",
            "CREATE INDEX IF NOT EXISTS idx_document_stages_submission_id ON document_stages(docuseal_submission_id)",
            "CREATE INDEX IF NOT EXISTS idx_document_stages_document_id ON document_stages(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_document_stage_artifacts_stage_type ON document_stage_artifacts(document_stage_id, artifact_type)",
            "CREATE INDEX IF NOT EXISTS idx_document_stage_field_values_stage ON document_stage_field_values(document_stage_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_steward_users_email ON external_steward_users(email)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_steward_users_subject "
                "ON external_steward_users(auth_issuer, auth_subject)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_steward_case_assignments_user_case "
                "ON external_steward_case_assignments(external_steward_user_id, case_id)"
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_external_steward_case_assignments_case "
                "ON external_steward_case_assignments(case_id)"
            ),
        ]
        for stmt in index_sql:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                continue

        con.commit()
    finally:
        con.close()
