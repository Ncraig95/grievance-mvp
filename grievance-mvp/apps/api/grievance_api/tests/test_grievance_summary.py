from __future__ import annotations

import unittest

from grievance_api.services.grievance_summary import build_grievance_summary, summarize_text


class GrievanceSummaryTests(unittest.TestCase):
    def test_uses_rich_template_narrative_before_generic_narrative(self) -> None:
        result = build_grievance_summary(
            {
                "narrative": "Generic intake narrative",
                "template_data": {
                    "q3_union_statement": "Management denied Jane Smith overtime on 2026-04-05 despite the posted schedule.",
                },
            }
        )

        self.assertEqual(result.source, "template_data.q3_union_statement")
        self.assertEqual(
            result.full_text,
            "Management denied Jane Smith overtime on 2026-04-05 despite the posted schedule.",
        )
        self.assertEqual(result.summary, result.full_text)

    def test_statement_narrative_beats_action_taken_and_current_status(self) -> None:
        result = build_grievance_summary(
            {
                "narrative": "Original statement submitted by the member.",
                "template_data": {
                    "action_taken": "Most recent action was a first level request.",
                    "current_status": "Waiting on a company response.",
                },
            }
        )

        self.assertEqual(result.source, "narrative")
        self.assertEqual(result.full_text, "Original statement submitted by the member.")
        self.assertEqual(result.summary, "Original statement submitted by the member.")

    def test_can_exclude_action_taken_and_current_status_for_tracker_display(self) -> None:
        result = build_grievance_summary(
            {
                "template_data": {
                    "action_taken": "Most recent action was a first level request.",
                    "current_status": "Waiting on a company response.",
                },
            },
            include_low_priority=False,
        )

        self.assertIsNone(result.source)
        self.assertIsNone(result.full_text)
        self.assertIsNone(result.summary)

    def test_short_text_passes_through(self) -> None:
        text = "Member was denied requested vacation in violation of the contract."

        self.assertEqual(summarize_text(text), text)

    def test_long_narrative_is_condensed_with_names_or_dates(self) -> None:
        text = (
            "On 2026-04-05, Jane Smith was denied overtime after management changed the schedule without notice. "
            "The steward requested records from the supervisor and asked that the posted overtime rotation be honored. "
            "Management has not provided the rotation list, payroll records, or an explanation for skipping the member."
        )

        summary = summarize_text(text, max_chars=140)

        self.assertIsNotNone(summary)
        self.assertLessEqual(len(summary or ""), 140)
        self.assertIn("2026-04-05", summary or "")
        self.assertIn("Jane Smith", summary or "")

    def test_boilerplate_narrative_falls_back_to_real_issue_text(self) -> None:
        result = build_grievance_summary(
            {
                "narrative": "Non-discipline grievance brief",
                "template_data": {
                    "issue_or_condition_involved": "The company bypassed the senior qualified employee for a temporary upgrade.",
                },
            }
        )

        self.assertEqual(result.source, "template_data.issue_or_condition_involved")
        self.assertEqual(result.summary, "The company bypassed the senior qualified employee for a temporary upgrade.")

    def test_punctuation_and_length_limits_are_stable(self) -> None:
        text = " ".join(
            [
                "Management",
                "changed",
                "the",
                "assignment",
                "without",
                "notice",
                "and",
                "the",
                "member",
                "lost",
                "scheduled",
                "hours",
            ]
            * 8
        )

        summary = summarize_text(text, max_chars=80)

        self.assertIsNotNone(summary)
        self.assertLessEqual(len(summary or ""), 80)
        self.assertTrue((summary or "").endswith("..."))


if __name__ == "__main__":
    unittest.main()
