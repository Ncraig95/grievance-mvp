from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from grievance_api.db.migrate import migrate
from grievance_api.services.grievance_number_repair import repair_grievance_numbers


class GrievanceNumberRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        migrate(self.db_path)
        self.con = sqlite3.connect(self.db_path)
        self.addCleanup(self._cleanup)
        self._seed_case(
            case_id="C-OLD-1",
            grievance_id="2026015",
            grievance_number=None,
            member_name="Old Duplicate One",
        )
        self._seed_case(
            case_id="C-OLD-2",
            grievance_id="2026015",
            grievance_number=None,
            member_name="Old Duplicate Two",
        )
        self._seed_case(
            case_id="C-26",
            grievance_id="2026026",
            grievance_number=None,
            member_name="Dean Anderson",
        )
        self._seed_case(
            case_id="C-27",
            grievance_id="2026027",
            grievance_number="2026027",
            member_name="Tarvus Graham",
        )
        self._seed_document("D-26", "C-26", "2026026 Dean Anderson - Core Southeastern")
        self._seed_document("D-27", "C-27", "2026027 Tarvus Graham - Core Southeastern")
        self._seed_event("C-26", "2026026", "2026026 Dean Anderson - Core Southeastern")
        self._seed_event("C-27", "2026027", "2026027 Tarvus Graham - Core Southeastern")
        self.con.execute(
            """
            INSERT INTO grievance_id_sequences(year, last_seq, updated_at_utc)
            VALUES(?,?,?)
            """,
            (2026, 27, "2026-04-13T00:00:00+00:00"),
        )
        self.con.commit()

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
        grievance_number: str | None,
        member_name: str,
    ) -> None:
        folder_name = f"{grievance_id} {member_name} - Core Southeastern"
        self.con.execute(
            """
            INSERT INTO cases(
              id, grievance_id, created_at_utc, status, approval_status, grievance_number,
              member_name, member_email, intake_request_id, intake_payload_json,
              sharepoint_case_folder, sharepoint_case_web_url
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                grievance_id,
                "2026-04-13T00:00:00+00:00",
                "awaiting_signatures",
                "pending",
                grievance_number,
                member_name,
                f"{case_id.lower()}@example.org",
                f"forms-{case_id}",
                json.dumps({"request_id": f"forms-{case_id}", "grievance_id": grievance_id}),
                folder_name,
                f"https://sharepoint.local/{folder_name.replace(' ', '%20')}",
            ),
        )

    def _seed_document(self, document_id: str, case_id: str, folder_name: str) -> None:
        base_url = f"https://sharepoint.local/{folder_name.replace(' ', '%20')}"
        self.con.execute(
            """
            INSERT INTO documents(
              id, case_id, created_at_utc, doc_type, template_key, status, requires_signature,
              signer_order_json, sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                document_id,
                case_id,
                "2026-04-13T00:00:00+00:00",
                "statement_of_occurrence",
                None,
                "completed",
                1,
                "[]",
                f"{base_url}/Generated/test.pdf",
                f"{base_url}/Signed/test_signed.pdf",
                f"{base_url}/Audit/test_audit.pdf",
            ),
        )

    def _seed_event(self, case_id: str, grievance_id: str, folder_name: str) -> None:
        payload = {
            "grievance_id": grievance_id,
            "folder_name": folder_name,
            "sharepoint_case_folder": folder_name,
            "folder_web_url": f"https://sharepoint.local/{folder_name.replace(' ', '%20')}",
        }
        self.con.execute(
            """
            INSERT INTO events(case_id, ts_utc, event_type, details_json)
            VALUES(?,?,?,?)
            """,
            (
                case_id,
                "2026-04-13T00:00:00+00:00",
                "sharepoint_upload_target_resolved",
                json.dumps(payload),
            ),
        )

    def test_repair_shifts_cutoff_and_later_cases_and_updates_metadata(self) -> None:
        result = repair_grievance_numbers(db_path=self.db_path)
        self._reopen()

        self.assertEqual(result.affected_case_count, 2)
        self.assertEqual(result.sequence_before, 27)
        self.assertEqual(result.sequence_after, 28)
        self.assertEqual(
            [(change.case_id, change.old_grievance_id, change.new_grievance_id) for change in result.changes],
            [
                ("C-27", "2026027", "2026028"),
                ("C-26", "2026026", "2026027"),
            ],
        )

        row_26 = self.con.execute(
            """
            SELECT grievance_id, grievance_number, sharepoint_case_folder, sharepoint_case_web_url
            FROM cases WHERE id='C-26'
            """
        ).fetchone()
        self.assertEqual(row_26[0], "2026027")
        self.assertIsNone(row_26[1])
        self.assertIn("2026027 Dean Anderson - Core Southeastern", row_26[2])
        self.assertIn("2026027%20Dean%20Anderson", row_26[3])

        row_27 = self.con.execute(
            """
            SELECT grievance_id, grievance_number, sharepoint_case_folder, sharepoint_case_web_url
            FROM cases WHERE id='C-27'
            """
        ).fetchone()
        self.assertEqual(row_27[0], "2026028")
        self.assertEqual(row_27[1], "2026028")
        self.assertIn("2026028 Tarvus Graham - Core Southeastern", row_27[2])
        self.assertIn("2026028%20Tarvus%20Graham", row_27[3])

        doc_26 = self.con.execute(
            """
            SELECT sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url
            FROM documents WHERE case_id='C-26'
            """
        ).fetchone()
        self.assertIn("2026027%20Dean%20Anderson", doc_26[0])
        self.assertIn("2026027%20Dean%20Anderson", doc_26[1])
        self.assertIn("2026027%20Dean%20Anderson", doc_26[2])

        doc_27 = self.con.execute(
            """
            SELECT sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url
            FROM documents WHERE case_id='C-27'
            """
        ).fetchone()
        self.assertIn("2026028%20Tarvus%20Graham", doc_27[0])
        self.assertIn("2026028%20Tarvus%20Graham", doc_27[1])
        self.assertIn("2026028%20Tarvus%20Graham", doc_27[2])

        events = self.con.execute(
            "SELECT case_id, details_json FROM events WHERE case_id IN ('C-26', 'C-27') ORDER BY case_id"
        ).fetchall()
        self.assertIn("2026027", events[0][1])
        self.assertNotIn("2026026", events[0][1])
        self.assertIn("2026028", events[1][1])
        self.assertNotIn("2026027 Tarvus", events[1][1])

        seq_row = self.con.execute(
            "SELECT last_seq FROM grievance_id_sequences WHERE year=2026"
        ).fetchone()
        self.assertEqual(seq_row[0], 28)

        old_rows = self.con.execute(
            "SELECT grievance_id FROM cases WHERE id IN ('C-OLD-1', 'C-OLD-2') ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in old_rows], ["2026015", "2026015"])

    def test_repair_fails_fast_when_cutoff_row_is_already_moved(self) -> None:
        repair_grievance_numbers(db_path=self.db_path)
        self._reopen()

        with self.assertRaises(RuntimeError) as ctx:
            repair_grievance_numbers(db_path=self.db_path)

        self.assertIn("cutoff grievance_id not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
