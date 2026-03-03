from __future__ import annotations

import unittest
from types import SimpleNamespace

from grievance_api.core.config import LayoutPolicyConfig, RenderingConfig
from grievance_api.web.models import IntakeRequest
from grievance_api.web.routes_intake import (
    _apply_layout_policy_context,
    _apply_statement_defaults,
    _build_template_context,
    _format_context_date_value,
)


class LayoutPolicyTests(unittest.TestCase):
    def test_grievance_number_falls_back_to_grievance_id(self) -> None:
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={
                    "statement_of_occurrence": LayoutPolicyConfig(
                        enabled=True,
                        grievance_number_fallback="grievance_id",
                        single_line_ellipsis=True,
                        max_chars={"grievant_name": 32},
                    )
                },
            )
        )
        context = {
            "grievance_id": "2026001",
            "grievance_number": "",
            "grievant_name": "Jane Doe",
        }

        meta = _apply_layout_policy_context(
            cfg=cfg,
            doc_type="statement_of_occurrence",
            context=context,
        )

        self.assertTrue(meta["policy_applied"])
        self.assertTrue(meta["fallback_applied"])
        self.assertEqual(context["grievance_number"], "2026001")

    def test_existing_grievance_number_is_not_overwritten(self) -> None:
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={
                    "statement_of_occurrence": LayoutPolicyConfig(
                        enabled=True,
                        grievance_number_fallback="grievance_id",
                        single_line_ellipsis=True,
                        max_chars={},
                    )
                },
            )
        )
        context = {
            "grievance_id": "2026001",
            "grievance_number": "GR-0009",
        }

        meta = _apply_layout_policy_context(
            cfg=cfg,
            doc_type="statement_of_occurrence",
            context=context,
        )

        self.assertTrue(meta["policy_applied"])
        self.assertFalse(meta["fallback_applied"])
        self.assertEqual(context["grievance_number"], "GR-0009")

    def test_overlength_fields_are_clamped_with_ellipsis(self) -> None:
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={
                    "statement_of_occurrence": LayoutPolicyConfig(
                        enabled=True,
                        grievance_number_fallback="grievance_id",
                        single_line_ellipsis=True,
                        max_chars={"grievant_name": 10},
                    )
                },
            )
        )
        context = {
            "grievance_id": "2026001",
            "grievance_number": "",
            "grievant_name": "Very Long Person Name",
        }

        meta = _apply_layout_policy_context(
            cfg=cfg,
            doc_type="statement_of_occurrence",
            context=context,
        )

        self.assertEqual(context["grievant_name"], "Very Long…")
        self.assertEqual(meta["clamped_fields"], ["grievant_name"])

    def test_non_policy_document_type_is_unchanged(self) -> None:
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={
                    "statement_of_occurrence": LayoutPolicyConfig(
                        enabled=True,
                        grievance_number_fallback="grievance_id",
                        single_line_ellipsis=True,
                        max_chars={"grievant_name": 10},
                    )
                },
            )
        )
        context = {
            "grievance_id": "2026001",
            "grievance_number": "",
            "grievant_name": "Very Long Person Name",
        }

        meta = _apply_layout_policy_context(
            cfg=cfg,
            doc_type="other_document",
            context=context,
        )

        self.assertFalse(meta["policy_applied"])
        self.assertEqual(context["grievance_number"], "")
        self.assertEqual(context["grievant_name"], "Very Long Person Name")

    def test_clamp_applies_to_normalized_alias_keys(self) -> None:
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={
                    "statement_of_occurrence": LayoutPolicyConfig(
                        enabled=True,
                        grievance_number_fallback="grievance_id",
                        single_line_ellipsis=True,
                        max_chars={"grievants_uid": 6},
                    )
                },
            )
        )
        context = {
            "grievance_id": "2026001",
            "grievance_number": "",
            "grievants_uid": "ABCDEFGHIJ",
            "grievants uid": "ABCDEFGHIJ",
        }

        meta = _apply_layout_policy_context(
            cfg=cfg,
            doc_type="statement_of_occurrence",
            context=context,
        )

        self.assertTrue(meta["policy_applied"])
        self.assertIn("grievants_uid", meta["clamped_fields"])
        self.assertEqual(context["grievants_uid"], "ABCDE…")
        self.assertEqual(context["grievants uid"], "ABCDE…")

    def test_grievants_uid_alias_default_uses_supplied_value(self) -> None:
        payload = IntakeRequest(
            request_id="req-1",
            contract="City of Jacksonville",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            grievant_email="jane@example.com",
            narrative="test",
        )
        context: dict[str, object] = {"grievants_uid": "UID-1234567890"}

        _apply_statement_defaults(
            context=context,
            payload=payload,
            grievance_id="2026001",
            grievance_number=None,
        )

        self.assertEqual(context["grievants_uid"], "UID-1234567890")
        self.assertEqual(context["grievants uid"], "UID-1234567890")

    def test_date_field_formatter_handles_iso_datetime(self) -> None:
        self.assertEqual(_format_context_date_value("2026-02-20T14:55:03Z"), "2026-02-20")
        self.assertEqual(_format_context_date_value("02/20/2026"), "2026-02-20")
        self.assertEqual(_format_context_date_value("not-a-date"), "not-a-date")

    def test_template_context_normalizes_date_fields_from_template_data(self) -> None:
        payload = IntakeRequest(
            request_id="req-2",
            contract="City of Jacksonville",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            grievant_email="jane@example.com",
            incident_date="2026-02-20T07:15:00Z",
            narrative="test",
            template_data={
                "seniority_date": "02/21/2026",
                "ncs_date": "02-22-2026",
            },
        )
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={},
            )
        )

        context, meta = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C123",
            grievance_id="2026001",
            document_id="D123",
            doc_type="statement_of_occurrence",
            grievance_number=None,
        )

        self.assertFalse(meta["policy_applied"])
        self.assertEqual(context["incident_date"], "2026-02-20")
        self.assertEqual(context["seniority_date"], "2026-02-21")
        self.assertEqual(context["ncs_date"], "2026-02-22")

    def test_settlement_context_builds_auto_expanding_rows(self) -> None:
        payload = IntakeRequest(
            request_id="req-3",
            contract="CWA",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            grievant_email="jane@example.com",
            narrative="Base narrative",
            template_data={
                "issue_text": (
                    "Issue details that are long enough to wrap to the next rendered row in "
                    "the Settlement Form dynamic section."
                ),
                "settlement_text": (
                    "Settlement details that are also long enough to wrap to another row so "
                    "we validate row expansion."
                ),
                "issue_article": "5.2",
            },
        )
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={},
            )
        )

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C124",
            grievance_id="2026002",
            document_id="D124",
            doc_type="settlement_form_3106",
            grievance_number="2026002",
        )

        issue_rows = context["issue_rows"]
        settlement_rows = context["settlement_rows"]
        self.assertIsInstance(issue_rows, list)
        self.assertIsInstance(settlement_rows, list)
        self.assertGreaterEqual(len(issue_rows), 2)
        self.assertGreaterEqual(len(settlement_rows), 2)
        self.assertEqual(context["issue_article"], "5.2")

    def test_settlement_context_uses_article_fallback_and_blank_row_default(self) -> None:
        payload = IntakeRequest(
            request_id="req-4",
            contract="CWA",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            grievant_email="jane@example.com",
            narrative="Narrative",
            template_data={"article": "12.1"},
        )
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={},
            )
        )

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C125",
            grievance_id="2026003",
            document_id="D125",
            doc_type="settlement_form_3106",
            grievance_number="2026003",
        )

        self.assertEqual(context["issue_article"], "12.1")
        self.assertEqual(context["settlement_rows"], [{"text": "", "line_no": 1}])

    def test_settlement_context_uses_template_grievance_number_when_case_number_blank(self) -> None:
        payload = IntakeRequest(
            request_id="req-5",
            contract="CWA",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            grievant_email="jane@example.com",
            narrative="Narrative",
            template_data={
                "grievance_number": "GR-9001",
                "issue_text": "Issue details",
            },
        )
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={},
            )
        )

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C126",
            grievance_id="2026005",
            document_id="D126",
            doc_type="settlement_form_3106",
            grievance_number=None,
        )

        self.assertEqual(context["grievance_number"], "GR-9001")

    def test_settlement_context_falls_back_grievance_number_to_grievance_id(self) -> None:
        payload = IntakeRequest(
            request_id="req-6",
            contract="CWA",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            grievant_email="jane@example.com",
            narrative="Narrative",
            template_data={"issue_text": "Issue details"},
        )
        cfg = SimpleNamespace(
            rendering=RenderingConfig(
                normalize_split_placeholders=True,
                layout_policies={},
            )
        )

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C127",
            grievance_id="2026006",
            document_id="D127",
            doc_type="settlement_form_3106",
            grievance_number=None,
        )

        self.assertEqual(context["grievance_number"], "2026006")

if __name__ == "__main__":
    unittest.main()
