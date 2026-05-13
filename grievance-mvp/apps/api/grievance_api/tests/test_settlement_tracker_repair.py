from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from grievance_api.db.migrate import migrate
from grievance_api.services.settlement_tracker_repair import repair_settlement_tracker_closures


class SettlementTrackerRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        migrate(self.db_path)
        self.con = sqlite3.connect(self.db_path)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.con.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def _reopen(self) -> None:
        self.con.close()
        self.con = sqlite3.connect(self.db_path)

    def _seed_case(
        self,
        *,
        case_id: str,
        grievance_id: str,
        officer_status: str | None,
        officer_closed_at_utc: str | None = None,
        officer_closed_by: str | None = None,
    ) -> None:
        self.con.execute(
            """
            INSERT INTO cases(
              id, grievance_id, created_at_utc, status, approval_status,
              member_name, member_email, intake_request_id, intake_payload_json,
              officer_status, officer_closed_at_utc, officer_closed_by
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                grievance_id,
                "2026-04-20T12:00:00+00:00",
                "approved",
                "approved",
                f"Member {case_id}",
                f"{case_id.lower()}@example.org",
                f"forms-{case_id}",
                json.dumps({"request_id": f"forms-{case_id}", "contract": "AT&T Mobility"}),
                officer_status,
                officer_closed_at_utc,
                officer_closed_by,
            ),
        )

    def _seed_document(
        self,
        *,
        document_id: str,
        case_id: str,
        doc_type: str = "settlement_form_3106",
        template_key: str | None = "settlement_form_3106",
        status: str = "approved",
        completed_at_utc: str | None = "2026-04-20T13:00:00+00:00",
        docuseal_submission_id: str | None = "submission-1",
        requires_signature: int = 1,
        created_at_utc: str = "2026-04-20T12:30:00+00:00",
    ) -> None:
        self.con.execute(
            """
            INSERT INTO documents(
              id, case_id, created_at_utc, doc_type, template_key, status,
              requires_signature, docuseal_submission_id, completed_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                document_id,
                case_id,
                created_at_utc,
                doc_type,
                template_key,
                status,
                requires_signature,
                docuseal_submission_id,
                completed_at_utc,
            ),
        )

    def test_dry_run_reports_matching_settlements_without_updates(self) -> None:
        self._seed_case(case_id="C1", grievance_id="2026001", officer_status="open")
        self._seed_document(document_id="D1", case_id="C1")
        self.con.commit()

        result = repair_settlement_tracker_closures(db_path=self.db_path, dry_run=True)
        self._reopen()

        self.assertTrue(result.dry_run)
        self.assertEqual(result.affected_case_count, 1)
        self.assertEqual(result.changes[0].case_id, "C1")
        row = self.con.execute("SELECT officer_status FROM cases WHERE id='C1'").fetchone()
        self.assertEqual(row[0], "open")
        event_count = self.con.execute(
            "SELECT COUNT(1) FROM events WHERE event_type='settlement_tracker_auto_closed'"
        ).fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_apply_closes_each_case_once_using_latest_completed_settlement(self) -> None:
        self._seed_case(case_id="C1", grievance_id="2026001", officer_status="waiting")
        self._seed_document(
            document_id="D-old",
            case_id="C1",
            completed_at_utc="2026-04-20T13:00:00+00:00",
            docuseal_submission_id="submission-old",
        )
        self._seed_document(
            document_id="D-new",
            case_id="C1",
            completed_at_utc="2026-04-21T13:00:00+00:00",
            docuseal_submission_id="submission-new",
        )
        self.con.commit()

        result = repair_settlement_tracker_closures(db_path=self.db_path, dry_run=False)
        self._reopen()

        self.assertFalse(result.dry_run)
        self.assertEqual(result.affected_case_count, 1)
        self.assertEqual(result.changes[0].document_id, "D-new")
        row = self.con.execute(
            "SELECT officer_status, officer_closed_at_utc, officer_closed_by FROM cases WHERE id='C1'"
        ).fetchone()
        self.assertEqual(row[0], "closed")
        self.assertTrue(str(row[1] or "").strip())
        self.assertEqual(row[2], "DocuSeal automation")
        event_row = self.con.execute(
            "SELECT document_id, details_json FROM events WHERE event_type='settlement_tracker_auto_closed'"
        ).fetchone()
        self.assertEqual(event_row[0], "D-new")
        details = json.loads(event_row[1])
        self.assertEqual(details["previous_officer_status"], "waiting")
        self.assertEqual(details["source"], "repair_settlement_tracker_closures")

    def test_already_closed_cases_are_ignored(self) -> None:
        self._seed_case(
            case_id="C1",
            grievance_id="2026001",
            officer_status="closed",
            officer_closed_at_utc="2026-04-01T00:00:00+00:00",
            officer_closed_by="Manual Officer",
        )
        self._seed_document(document_id="D1", case_id="C1")
        self.con.commit()

        result = repair_settlement_tracker_closures(db_path=self.db_path, dry_run=False)
        self._reopen()

        self.assertEqual(result.affected_case_count, 0)
        row = self.con.execute(
            "SELECT officer_status, officer_closed_at_utc, officer_closed_by FROM cases WHERE id='C1'"
        ).fetchone()
        self.assertEqual(row, ("closed", "2026-04-01T00:00:00+00:00", "Manual Officer"))

    def test_incomplete_and_ops_cleared_settlement_documents_are_ignored(self) -> None:
        self._seed_case(case_id="C-incomplete", grievance_id="2026001", officer_status="open")
        self._seed_document(
            document_id="D-incomplete",
            case_id="C-incomplete",
            status="sent_for_signature",
            completed_at_utc=None,
            docuseal_submission_id="submission-incomplete",
        )
        self._seed_case(case_id="C-ops", grievance_id="2026002", officer_status="open")
        self._seed_document(
            document_id="D-ops",
            case_id="C-ops",
            status="ops_cleared",
            completed_at_utc=None,
            docuseal_submission_id="submission-ops",
        )
        self.con.commit()

        result = repair_settlement_tracker_closures(db_path=self.db_path, dry_run=False)
        self._reopen()

        self.assertEqual(result.affected_case_count, 0)
        rows = self.con.execute("SELECT officer_status FROM cases ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in rows], ["open", "open"])


if __name__ == "__main__":
    unittest.main()
