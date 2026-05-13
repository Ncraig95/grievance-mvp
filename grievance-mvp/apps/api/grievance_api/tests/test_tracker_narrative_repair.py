from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from grievance_api.db.migrate import migrate
from grievance_api.services.tracker_narrative_repair import repair_tracker_narratives


class TrackerNarrativeRepairTests(unittest.TestCase):
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
        tracking_issue_summary: str,
        payload: dict[str, object],
    ) -> None:
        self.con.execute(
            """
            INSERT INTO cases(
              id, grievance_id, created_at_utc, status, approval_status,
              member_name, member_email, intake_request_id, intake_payload_json,
              tracking_issue_summary
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                f"2026{case_id[-3:]}",
                "2026-04-20T12:00:00+00:00",
                "approved",
                "approved",
                f"Member {case_id}",
                f"{case_id.lower()}@example.org",
                f"forms-{case_id}",
                json.dumps(payload),
                tracking_issue_summary,
            ),
        )

    def test_dry_run_reports_candidates_without_updates(self) -> None:
        self._seed_case(
            case_id="C001",
            tracking_issue_summary="Most recent action was a first level request.",
            payload={
                "request_id": "forms-C001",
                "narrative": "Original statement submitted by the member.",
                "template_data": {
                    "action_taken": "Most recent action was a first level request.",
                },
            },
        )
        self.con.commit()

        result = repair_tracker_narratives(db_path=self.db_path, dry_run=True)
        self._reopen()

        self.assertTrue(result.dry_run)
        self.assertEqual(result.affected_case_count, 1)
        self.assertEqual(result.changes[0].case_id, "C001")
        self.assertEqual(result.changes[0].new_tracking_issue_summary, "Original statement submitted by the member.")
        row = self.con.execute("SELECT tracking_issue_summary FROM cases WHERE id='C001'").fetchone()
        self.assertEqual(row[0], "Most recent action was a first level request.")
        event_count = self.con.execute(
            "SELECT COUNT(1) FROM events WHERE event_type='tracker_narrative_repaired'"
        ).fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_apply_updates_only_exact_low_priority_matches(self) -> None:
        self._seed_case(
            case_id="C001",
            tracking_issue_summary="Most recent action was a first level request.",
            payload={
                "request_id": "forms-C001",
                "narrative": "Original statement submitted by the member.",
                "template_data": {
                    "action_taken": "Most recent action was a first level request.",
                },
            },
        )
        self._seed_case(
            case_id="C002",
            tracking_issue_summary="Officer-entered summary should stay.",
            payload={
                "request_id": "forms-C002",
                "narrative": "Second original statement.",
                "template_data": {
                    "action_taken": "Most recent action was a first level request.",
                },
            },
        )
        self.con.commit()

        result = repair_tracker_narratives(db_path=self.db_path, dry_run=False)
        self._reopen()

        self.assertFalse(result.dry_run)
        self.assertEqual(result.affected_case_count, 1)
        rows = dict(
            self.con.execute(
                "SELECT id, tracking_issue_summary FROM cases WHERE id IN ('C001', 'C002')"
            ).fetchall()
        )
        self.assertEqual(rows["C001"], "Original statement submitted by the member.")
        self.assertEqual(rows["C002"], "Officer-entered summary should stay.")
        event_row = self.con.execute(
            "SELECT details_json FROM events WHERE event_type='tracker_narrative_repaired'"
        ).fetchone()
        details = json.loads(event_row[0])
        self.assertEqual(details["previous_source"], "template_data.action_taken")
        self.assertEqual(details["new_source"], "narrative")

    def test_rows_without_recoverable_original_narrative_are_reported_unresolved(self) -> None:
        self._seed_case(
            case_id="C003",
            tracking_issue_summary="Most recent action was a first level request.",
            payload={
                "request_id": "forms-C003",
                "narrative": "Most recent action was a first level request.",
                "template_data": {
                    "action_taken": "Most recent action was a first level request.",
                },
            },
        )
        self.con.commit()

        result = repair_tracker_narratives(db_path=self.db_path, dry_run=False)
        self._reopen()

        self.assertEqual(result.affected_case_count, 0)
        self.assertEqual(result.unresolved_case_count, 1)
        self.assertEqual(result.unresolved[0].case_id, "C003")
        row = self.con.execute("SELECT tracking_issue_summary FROM cases WHERE id='C003'").fetchone()
        self.assertEqual(row[0], "Most recent action was a first level request.")


if __name__ == "__main__":
    unittest.main()
