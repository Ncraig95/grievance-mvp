from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.models import (
    OfficerCaseBulkDeleteRequest,
    OfficerCaseBulkUpdateRequest,
    OfficerCaseCreateRequest,
    OfficerCaseUpdateRequest,
)
from grievance_api.web.routes_officers import (
    bulk_delete_officer_cases,
    bulk_update_officer_cases,
    create_officer_case,
    delete_officer_case,
    officer_cases,
    officers_page,
    update_officer_case,
)


class _Request:
    def __init__(self, *, state, host: str = "127.0.0.1") -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host=host)


class OfficerTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    @staticmethod
    def _cfg():
        return SimpleNamespace(
            officer_tracking=SimpleNamespace(
                roster=("Officer A", "Officer B", "grievance@cwa3106.com"),
            )
        )

    async def test_migrate_adds_officer_columns_to_legacy_cases_table(self) -> None:
        legacy_path = str(Path(self.tmpdir.name) / "legacy.sqlite3")
        con = sqlite3.connect(legacy_path)
        try:
            con.execute(
                """
                CREATE TABLE cases (
                  id TEXT PRIMARY KEY,
                  grievance_id TEXT NOT NULL,
                  created_at_utc TEXT NOT NULL,
                  status TEXT NOT NULL,
                  member_name TEXT NOT NULL,
                  intake_request_id TEXT NOT NULL,
                  intake_payload_json TEXT NOT NULL
                )
                """
            )
            con.commit()
        finally:
            con.close()

        migrate(legacy_path)

        con = sqlite3.connect(legacy_path)
        try:
            cols = {str(row[1]) for row in con.execute("PRAGMA table_info(cases)").fetchall()}
        finally:
            con.close()

        self.assertIn("officer_status", cols)
        self.assertIn("officer_assignee", cols)
        self.assertIn("tracking_department", cols)
        self.assertIn("tracking_second_level_request_sent_date", cols)

    async def test_officer_cases_uses_existing_payload_fallbacks_when_tracking_fields_are_blank(self) -> None:
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "C1",
                "2026015",
                "2026-03-27T15:00:00+00:00",
                "awaiting_signatures",
                "pending",
                None,
                "Nick Craig",
                "nick@example.org",
                "forms-1",
                json.dumps(
                    {
                        "request_id": "forms-1",
                        "grievant_email": "nick@example.org",
                        "incident_date": "2026-03-20",
                        "narrative": "Basic summary",
                        "template_data": {
                            "q2_department": "Utilities",
                            "q5_union_rep_name_attuid": "Steward Smith",
                            "issue_text": "Contract issue details",
                        },
                    }
                ),
            ),
        )
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        result = await officer_cases(request)

        self.assertEqual(result.count, 1)
        row = result.rows[0]
        self.assertEqual(row.department, "Utilities")
        self.assertEqual(row.steward, "Steward Smith")
        self.assertEqual(row.occurrence_date, "2026-03-20")
        self.assertEqual(row.issue_summary, "Contract issue details")
        self.assertEqual(row.display_grievance, "2026015")
        self.assertEqual(row.officer_source, "digital_intake")
        self.assertEqual(row.officer_status, "open")

    async def test_manual_paper_create_builds_case_without_documents_and_audits_event(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        created = await create_officer_case(
            OfficerCaseCreateRequest(
                grievance_number="2026-100",
                member_name="Pat Member",
                member_email="pat@example.org",
                department="Mobility",
                steward="Steward Jones",
                occurrence_date="2026-03-28",
                issue_summary="Paper grievance summary",
                first_level_request_sent_date="2026-03-29",
                officer_assignee="Officer A",
                updated_by="Officer A",
            ),
            request,
        )

        case_row = await self.db.fetchone(
            "SELECT status, officer_source, intake_payload_json FROM cases WHERE id=?",
            (created.case_id,),
        )
        documents = await self.db.fetchall("SELECT id FROM documents WHERE case_id=?", (created.case_id,))
        event_row = await self.db.fetchone(
            "SELECT event_type, details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            (created.case_id,),
        )

        self.assertEqual(created.workflow_status, "manual_tracking")
        self.assertEqual(created.officer_source, "paper_manual")
        self.assertTrue(created.grievance_id.startswith("G"))
        self.assertEqual(case_row[0], "manual_tracking")
        self.assertEqual(case_row[1], "paper_manual")
        self.assertEqual(documents, [])
        self.assertEqual(event_row[0], "officer_case_created")
        self.assertEqual(json.loads(event_row[1])["updated_by"], "Officer A")
        self.assertEqual(json.loads(case_row[2])["documents"], [])

    async def test_update_officer_case_updates_fields_and_manages_close_reopen_stamps(self) -> None:
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json,
                 officer_status, officer_assignee, officer_notes
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "C1",
                "2026015",
                "2026-03-27T15:00:00+00:00",
                "manual_tracking",
                "pending",
                "2026-015",
                "Nick Craig",
                "nick@example.org",
                "officer-manual-1",
                json.dumps({"request_id": "officer-manual-1", "documents": []}),
                "open",
                "Officer A",
                "Starting note",
            ),
        )
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        updated = await update_officer_case(
            "C1",
            OfficerCaseUpdateRequest(
                department="Core",
                steward="Steward Updated",
                issue_summary="Updated notes",
                officer_assignee="Officer B",
                officer_notes="Closed out",
                officer_status="closed",
                updated_by="Officer B",
            ),
            request,
        )

        db_row = await self.db.fetchone(
            """SELECT tracking_department, tracking_steward, tracking_issue_summary,
                      officer_assignee, officer_notes, officer_status,
                      officer_closed_at_utc, officer_closed_by
               FROM cases WHERE id=?""",
            ("C1",),
        )
        event_row = await self.db.fetchone(
            "SELECT event_type, details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            ("C1",),
        )

        self.assertEqual(updated.officer_status, "closed")
        self.assertEqual(db_row[0], "Core")
        self.assertEqual(db_row[1], "Steward Updated")
        self.assertEqual(db_row[2], "Updated notes")
        self.assertEqual(db_row[3], "Officer B")
        self.assertEqual(db_row[4], "Closed out")
        self.assertEqual(db_row[5], "closed")
        self.assertIsNotNone(db_row[6])
        self.assertEqual(db_row[7], "Officer B")
        self.assertEqual(event_row[0], "officer_case_updated")
        self.assertEqual(json.loads(event_row[1])["changes"]["officer_status"], "closed")

        reopened = await update_officer_case(
            "C1",
            OfficerCaseUpdateRequest(
                officer_status="open",
                updated_by="Officer B",
            ),
            request,
        )
        reopened_row = await self.db.fetchone(
            "SELECT officer_status, officer_closed_at_utc, officer_closed_by FROM cases WHERE id=?",
            ("C1",),
        )

        self.assertEqual(reopened.officer_status, "open")
        self.assertEqual(reopened_row[0], "open")
        self.assertIsNone(reopened_row[1])
        self.assertIsNone(reopened_row[2])

    async def test_officer_routes_require_local_or_private_network_access(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db), host="8.8.8.8")

        with self.assertRaises(HTTPException) as page_exc:
            await officers_page(request)
        with self.assertRaises(HTTPException) as cases_exc:
            await officer_cases(request)
        with self.assertRaises(HTTPException) as bulk_delete_exc:
            await bulk_delete_officer_cases(OfficerCaseBulkDeleteRequest(case_ids=["missing"]), request)
        with self.assertRaises(HTTPException) as bulk_exc:
            await bulk_update_officer_cases(OfficerCaseBulkUpdateRequest(case_ids=["missing"], officer_status="closed"), request)
        with self.assertRaises(HTTPException) as delete_exc:
            await delete_officer_case("missing", request)

        self.assertEqual(page_exc.exception.status_code, 403)
        self.assertEqual(cases_exc.exception.status_code, 403)
        self.assertEqual(bulk_delete_exc.exception.status_code, 403)
        self.assertEqual(bulk_exc.exception.status_code, 403)
        self.assertEqual(delete_exc.exception.status_code, 403)

    async def test_bulk_update_officer_cases_updates_selected_rows_only(self) -> None:
        for case_id, grievance_id, member_name, officer_status in (
            ("C1", "2026101", "Member One", "open"),
            ("C2", "2026102", "Member Two", "waiting"),
            ("C3", "2026103", "Member Three", "open"),
        ):
            await self.db.exec(
                """INSERT INTO cases(
                     id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                     member_name, member_email, intake_request_id, intake_payload_json,
                     officer_status, officer_assignee, officer_notes
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case_id,
                    grievance_id,
                    "2026-03-27T15:00:00+00:00",
                    "manual_tracking",
                    "pending",
                    None,
                    member_name,
                    f"{case_id.lower()}@example.org",
                    f"officer-manual-{case_id}",
                    json.dumps({"request_id": f"officer-manual-{case_id}", "documents": []}),
                    officer_status,
                    None,
                    None,
                ),
            )

        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        result = await bulk_update_officer_cases(
            OfficerCaseBulkUpdateRequest(
                case_ids=["C1", "C2"],
                officer_status="closed",
                officer_assignee="Officer B",
                first_level_request_sent_date="2026-03-30",
                officer_notes="Bulk closed",
                updated_by="Officer B",
            ),
            request,
        )

        rows = await self.db.fetchall(
            """SELECT id, officer_status, officer_assignee, officer_notes,
                      tracking_first_level_request_sent_date, officer_closed_at_utc, officer_closed_by
               FROM cases ORDER BY id""",
        )
        event_rows = await self.db.fetchall(
            "SELECT case_id, event_type, details_json FROM events ORDER BY id",
        )

        self.assertEqual(result.selected_case_count, 2)
        self.assertEqual(result.updated_case_count, 2)
        self.assertEqual(result.case_ids, ["C1", "C2"])
        self.assertEqual(
            result.changed_fields,
            ["first_level_request_sent_date", "officer_assignee", "officer_notes", "officer_status"],
        )
        self.assertEqual(rows[0][0], "C1")
        self.assertEqual(rows[0][1], "closed")
        self.assertEqual(rows[0][2], "Officer B")
        self.assertEqual(rows[0][3], "Bulk closed")
        self.assertEqual(rows[0][4], "2026-03-30")
        self.assertIsNotNone(rows[0][5])
        self.assertEqual(rows[0][6], "Officer B")
        self.assertEqual(rows[1][0], "C2")
        self.assertEqual(rows[1][1], "closed")
        self.assertEqual(rows[1][2], "Officer B")
        self.assertEqual(rows[1][3], "Bulk closed")
        self.assertEqual(rows[1][4], "2026-03-30")
        self.assertIsNotNone(rows[1][5])
        self.assertEqual(rows[1][6], "Officer B")
        self.assertEqual(rows[2][0], "C3")
        self.assertEqual(rows[2][1], "open")
        self.assertIsNone(rows[2][2])
        self.assertIsNone(rows[2][3])
        self.assertIsNone(rows[2][4])
        self.assertIsNone(rows[2][5])
        self.assertIsNone(rows[2][6])
        self.assertEqual(len(event_rows), 2)
        self.assertEqual(event_rows[0][0], "C1")
        self.assertEqual(event_rows[1][0], "C2")
        self.assertTrue(json.loads(event_rows[0][2])["bulk_update"])

    async def test_bulk_delete_officer_cases_removes_only_selected_rows(self) -> None:
        for case_id, grievance_id in (("C1", "2026201"), ("C2", "2026202"), ("C3", "2026203")):
            await self.db.exec(
                """INSERT INTO cases(
                     id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                     member_name, member_email, intake_request_id, intake_payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    case_id,
                    grievance_id,
                    "2026-03-27T15:00:00+00:00",
                    "awaiting_signatures",
                    "pending",
                    None,
                    f"Member {case_id}",
                    f"{case_id.lower()}@example.org",
                    f"forms-{case_id}",
                    json.dumps({"request_id": f"forms-{case_id}", "documents": []}),
                ),
            )
            await self.db.exec(
                """INSERT INTO documents(
                     id, case_id, created_at_utc, doc_type, template_key, status,
                     requires_signature, signer_order_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    f"DOC_{case_id}",
                    case_id,
                    "2026-03-27T15:00:01+00:00",
                    "statement_of_occurrence",
                    "statement_of_occurrence",
                    "sent_for_signature",
                    1,
                    json.dumps([f"{case_id.lower()}@example.org"]),
                ),
            )
            await self.db.exec(
                """INSERT INTO events(case_id, document_id, ts_utc, event_type, details_json)
                   VALUES(?,?,?,?,?)""",
                (
                    case_id,
                    f"DOC_{case_id}",
                    "2026-03-27T15:00:06+00:00",
                    "test_event",
                    "{}",
                ),
            )

        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        result = await bulk_delete_officer_cases(
            OfficerCaseBulkDeleteRequest(case_ids=["C1", "C2"]),
            request,
        )

        remaining_cases = await self.db.fetchall("SELECT id FROM cases ORDER BY id")
        remaining_docs = await self.db.fetchall("SELECT id FROM documents ORDER BY id")
        remaining_events = await self.db.fetchall("SELECT case_id FROM events ORDER BY case_id")

        self.assertEqual(result.selected_case_count, 2)
        self.assertEqual(result.deleted_case_count, 2)
        self.assertEqual(result.deleted_case_ids, ["C1", "C2"])
        self.assertEqual(result.deleted_document_count, 2)
        self.assertEqual(result.deleted_event_count, 2)
        self.assertEqual(remaining_cases, [("C3",)])
        self.assertEqual(remaining_docs, [("DOC_C3",)])
        self.assertEqual(remaining_events, [("C3",)])

    async def test_delete_officer_case_removes_only_selected_case_and_related_rows(self) -> None:
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
               member_name, member_email, intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "TEST_BY_NAME",
                "2026998",
                "2026-03-27T15:00:00+00:00",
                "awaiting_signatures",
                "pending",
                None,
                "Test User",
                "test@example.org",
                "forms-plain",
                json.dumps({"request_id": "forms-plain", "documents": []}),
            ),
        )
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "REAL1",
                "2026999",
                "2026-03-27T16:00:00+00:00",
                "awaiting_signatures",
                "pending",
                None,
                "Real Member",
                "real@example.org",
                "forms-prod-1",
                json.dumps({"request_id": "forms-prod-1", "documents": []}),
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "DOC_TEST",
                "TEST_BY_NAME",
                "2026-03-27T15:00:01+00:00",
                "statement_of_occurrence",
                "statement_of_occurrence",
                "sent_for_signature",
                1,
                json.dumps(["test@example.org"]),
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "DOC_REAL",
                "REAL1",
                "2026-03-27T16:00:01+00:00",
                "statement_of_occurrence",
                "statement_of_occurrence",
                "sent_for_signature",
                1,
                json.dumps(["real@example.org"]),
            ),
        )
        await self.db.exec(
            """INSERT INTO document_stages(
                 case_id, document_id, stage_no, stage_key, status, signer_email, started_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "TEST_BY_NAME",
                "DOC_TEST",
                1,
                "member_signature",
                "sent_for_signature",
                "test@example.org",
                "2026-03-27T15:00:02+00:00",
            ),
        )
        stage_row = await self.db.fetchone(
            "SELECT id FROM document_stages WHERE case_id=? AND document_id=?",
            ("TEST_BY_NAME", "DOC_TEST"),
        )
        stage_id = int(stage_row[0])
        await self.db.exec(
            """INSERT INTO document_stage_artifacts(
                 document_stage_id, artifact_type, storage_backend, storage_path, sha256, size_bytes, created_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                stage_id,
                "pdf",
                "disk",
                "/tmp/test.pdf",
                "abc123",
                42,
                "2026-03-27T15:00:03+00:00",
            ),
        )
        await self.db.exec(
            """INSERT INTO document_stage_field_values(
                 document_stage_id, field_key, field_value, created_at_utc
               ) VALUES(?,?,?,?)""",
            (
                stage_id,
                "member_name",
                "Test User",
                "2026-03-27T15:00:04+00:00",
            ),
        )
        await self.db.exec(
            """INSERT INTO outbound_emails(
                 case_id, document_scope_id, template_key, recipient_email, idempotency_key,
                 status, created_at_utc, updated_at_utc, metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "TEST_BY_NAME",
                "DOC_TEST",
                "reminder_signature",
                "test@example.org",
                "cleanup-test",
                "sent",
                "2026-03-27T15:00:05+00:00",
                "2026-03-27T15:00:05+00:00",
                "{}",
            ),
        )
        await self.db.exec(
            """INSERT INTO events(case_id, document_id, ts_utc, event_type, details_json)
               VALUES(?,?,?,?,?)""",
            (
                "TEST_BY_NAME",
                "DOC_TEST",
                "2026-03-27T15:00:06+00:00",
                "test_event",
                "{}",
            ),
        )
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        result = await delete_officer_case("TEST_BY_NAME", request)

        remaining_cases = await self.db.fetchall("SELECT id FROM cases ORDER BY id")
        remaining_docs = await self.db.fetchall("SELECT id FROM documents ORDER BY id")
        remaining_stages = await self.db.fetchall("SELECT id FROM document_stages")
        remaining_stage_artifacts = await self.db.fetchall("SELECT id FROM document_stage_artifacts")
        remaining_stage_fields = await self.db.fetchall("SELECT id FROM document_stage_field_values")
        remaining_events = await self.db.fetchall("SELECT id FROM events")
        remaining_emails = await self.db.fetchall("SELECT id FROM outbound_emails")

        self.assertEqual(result.case_id, "TEST_BY_NAME")
        self.assertEqual(result.grievance_id, "2026998")
        self.assertEqual(result.display_grievance, "2026998")
        self.assertEqual(result.deleted_case_count, 1)
        self.assertEqual(result.deleted_document_count, 1)
        self.assertEqual(result.deleted_document_stage_count, 1)
        self.assertEqual(result.deleted_stage_artifact_count, 1)
        self.assertEqual(result.deleted_stage_field_value_count, 1)
        self.assertEqual(result.deleted_event_count, 1)
        self.assertEqual(result.deleted_outbound_email_count, 1)
        self.assertEqual(remaining_cases, [("REAL1",)])
        self.assertEqual(remaining_docs, [("DOC_REAL",)])
        self.assertEqual(remaining_stages, [])
        self.assertEqual(remaining_stage_artifacts, [])
        self.assertEqual(remaining_stage_fields, [])
        self.assertEqual(remaining_events, [])
        self.assertEqual(remaining_emails, [])


if __name__ == "__main__":
    unittest.main()
