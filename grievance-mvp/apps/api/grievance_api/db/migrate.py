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

        _ensure_column(con, "documents", "template_key", "TEXT")
        _ensure_column(con, "documents", "signed_pdf_path", "TEXT")
        _ensure_column(con, "documents", "audit_zip_path", "TEXT")
        _ensure_column(con, "documents", "sharepoint_generated_url", "TEXT")
        _ensure_column(con, "documents", "sharepoint_signed_url", "TEXT")
        _ensure_column(con, "documents", "sharepoint_audit_url", "TEXT")

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
            "CREATE INDEX IF NOT EXISTS idx_documents_case_id ON documents(case_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_docuseal_submission ON documents(docuseal_submission_id)",
            "DROP INDEX IF EXISTS idx_documents_case_doc_type",
            "CREATE INDEX IF NOT EXISTS idx_documents_case_doc_type ON documents(case_id, doc_type)",
            "CREATE INDEX IF NOT EXISTS idx_events_case_id ON events(case_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_document_id ON events(document_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_receipts_provider_key ON webhook_receipts(provider, receipt_key)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_emails_dedup "
                "ON outbound_emails(case_id, document_scope_id, template_key, recipient_email, idempotency_key)"
            ),
            "CREATE INDEX IF NOT EXISTS idx_outbound_emails_case ON outbound_emails(case_id)",
        ]
        for stmt in index_sql:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                continue

        con.commit()
    finally:
        con.close()
