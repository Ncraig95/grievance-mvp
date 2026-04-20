from __future__ import annotations

import unittest

from grievance_api.web.routes_intake import _apply_3g3a_defaults, _clear_3g3a_stage_interactive_marks


class ThreeGThreeACheckboxMappingTests(unittest.TestCase):
    def test_contract_bst_sets_q1_bst_marker_when_q1_choice_omitted(self) -> None:
        context: dict[str, object] = {"contract": "BST"}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q1_is_bst_mark"], "☒")
        self.assertEqual(context["q1_is_billing_mark"], "☐")
        self.assertEqual(context["q1_is_utility_operations_mark"], "☐")
        self.assertEqual(context["q1_is_other_mark"], "☐")

    def test_contract_utilities_sets_q1_utility_operations_marker(self) -> None:
        context: dict[str, object] = {"contract": "Utilities"}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q1_is_bst_mark"], "☐")
        self.assertEqual(context["q1_is_billing_mark"], "☐")
        self.assertEqual(context["q1_is_utility_operations_mark"], "☒")
        self.assertEqual(context["q1_is_other_mark"], "☐")

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

    def test_q5_second_level_date_is_docuseal_stage2_marker(self) -> None:
        context: dict[str, object] = {"q5_second_level_meeting_date": "2026-02-23"}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(
            context["q5_second_level_meeting_date"],
            "{{Dte_es_:signer2:q5_l2_date}}",
        )

    def test_q5_primary_dates_default_to_occurred_date_when_missing(self) -> None:
        context: dict[str, object] = {"q1_occurred_date": "2026-02-22"}
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q5_informal_meeting_date"], "2026-02-22")
        self.assertEqual(context["q5_3g3r_issued_date"], "2026-02-22")

    def test_q10_bool_alias_values_map_to_checkbox_marks(self) -> None:
        context: dict[str, object] = {
            "q10_company_true_intent_choice": "true",
            "q10_union_true_intent_choice": "0",
        }
        _apply_3g3a_defaults(context=context, grievance_id="2026001")
        self.assertEqual(context["q10_company_is_yes_mark"], "☒")
        self.assertEqual(context["q10_company_is_no_mark"], "☐")
        self.assertEqual(context["q10_union_is_yes_mark"], "☐")
        self.assertEqual(context["q10_union_is_no_mark"], "☒")

    def test_extension_preserves_user_supplied_union_statement(self) -> None:
        context: dict[str, object] = {
            "q3_union_statement": "user supplied value",
        }
        _apply_3g3a_defaults(
            context=context,
            grievance_id="2026001",
        )
        self.assertEqual(context["q3_union_statement"], "user supplied value")

    def test_clear_stage_interactive_marks_resets_q8_and_q10_marks(self) -> None:
        context: dict[str, object] = {
            "q1_is_bst_mark": "☒",
            "q8_is_accepted_mark": "☒",
            "q8_is_rejected_mark": "☐",
            "q8_is_appealed_mark": "☒",
            "q8_is_requested_mediation_mark": "☒",
            "q10_company_is_yes_mark": "☒",
            "q10_company_is_no_mark": "☐",
            "q10_union_is_yes_mark": "☒",
            "q10_union_is_no_mark": "☐",
        }
        _clear_3g3a_stage_interactive_marks(context=context)
        self.assertEqual(context["q1_is_bst_mark"], "☒")
        self.assertEqual(context["q8_is_accepted_mark"], "☐")
        self.assertEqual(context["q8_is_rejected_mark"], "☐")
        self.assertEqual(context["q8_is_appealed_mark"], "☐")
        self.assertEqual(context["q8_is_requested_mediation_mark"], "☐")
        self.assertEqual(context["q10_company_is_yes_mark"], "☐")
        self.assertEqual(context["q10_company_is_no_mark"], "☐")
        self.assertEqual(context["q10_union_is_yes_mark"], "☐")
        self.assertEqual(context["q10_union_is_no_mark"], "☐")

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
