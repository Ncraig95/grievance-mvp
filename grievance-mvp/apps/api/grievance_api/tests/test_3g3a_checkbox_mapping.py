from __future__ import annotations

import unittest

from grievance_api.web.routes_intake import _apply_3g3a_defaults


class ThreeGThreeACheckboxMappingTests(unittest.TestCase):
    def test_q1_choice_sets_single_checked_marker(self) -> None:
        context: dict[str, object] = {"q1_choice": "BST"}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q1_is_bst_mark"], "☒")
        self.assertEqual(context["q1_is_billing_mark"], "☐")
        self.assertEqual(context["q1_is_utility_operations_mark"], "☐")
        self.assertEqual(context["q1_is_other_mark"], "☐")
        self.assertEqual(context["q1_grievance_type"], "")

    def test_q1_other_text_only_prints_when_other_is_selected(self) -> None:
        context: dict[str, object] = {
            "q1_choice": "Other",
            "q1_grievance_type": "Legacy Routing",
        }
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q1_is_other_mark"], "☒")
        self.assertEqual(context["q1_grievance_type"], "Legacy Routing")

    def test_q8_and_q10_choice_aliases_work(self) -> None:
        context: dict[str, object] = {
            "q8_union_disposition": "Requested Mediation",
            "q10_true_intent_exists": "No",
        }
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q8_is_requested_mediation_mark"], "☒")
        self.assertEqual(context["q8_is_accepted_mark"], "☐")
        self.assertEqual(context["q10_company_is_no_mark"], "☒")
        self.assertEqual(context["q10_company_is_yes_mark"], "☐")
        self.assertEqual(context["q10_union_is_no_mark"], "☒")
        self.assertEqual(context["q10_union_is_yes_mark"], "☐")

    def test_q10_company_and_union_can_differ(self) -> None:
        context: dict[str, object] = {
            "q10_company_true_intent_choice": "Yes",
            "q10_union_true_intent_choice": "No",
        }
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q10_company_is_yes_mark"], "☒")
        self.assertEqual(context["q10_company_is_no_mark"], "☐")
        self.assertEqual(context["q10_union_is_no_mark"], "☒")
        self.assertEqual(context["q10_union_is_yes_mark"], "☐")

    def test_local_grievance_number_falls_back_to_grievance_id(self) -> None:
        context: dict[str, object] = {}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["local_grievance_number"], "2026001")

    def test_local_grievance_number_preserves_explicit_value(self) -> None:
        context: dict[str, object] = {"local_grievance_number": "LOC-77"}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["local_grievance_number"], "LOC-77")

    def test_long_text_fields_are_wrapped_and_clamped(self) -> None:
        very_long = ("word " * 600).strip()
        context: dict[str, object] = {
            "q3_union_statement": very_long,
            "q4_contract_basis": very_long,
        }
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        q3 = str(context["q3_union_statement"])
        q4 = str(context["q4_contract_basis"])
        self.assertIn("\n", q3)
        self.assertIn("\n", q4)
        self.assertLessEqual(len(q3), 1800)
        self.assertLessEqual(len(q4), 700)


if __name__ == "__main__":
    unittest.main()
