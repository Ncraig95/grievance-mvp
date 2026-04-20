from __future__ import annotations

import unittest

from grievance_api.web.routes_intake import _normalize_name_fields


class IntakeNameNormalizationTests(unittest.TestCase):
    def test_preserves_existing_first_last(self) -> None:
        payload = {
            "grievant_firstname": "Jane",
            "grievant_lastname": "Doe",
            "grievant_name": "Ignored Name",
        }
        out = _normalize_name_fields(payload)
        self.assertEqual(out["grievant_firstname"], "Jane")
        self.assertEqual(out["grievant_lastname"], "Doe")

    def test_splits_grievant_name_when_first_last_missing(self) -> None:
        payload = {
            "grievant_name": "John Q Public",
        }
        out = _normalize_name_fields(payload)
        self.assertEqual(out["grievant_firstname"], "John")
        self.assertEqual(out["grievant_lastname"], "Q Public")

    def test_uses_template_data_grievant_name(self) -> None:
        payload = {
            "template_data": {
                "grievant_name": "SingleName",
            }
        }
        out = _normalize_name_fields(payload)
        self.assertEqual(out["grievant_firstname"], "SingleName")
        self.assertEqual(out["grievant_lastname"], "Unknown")

    def test_uses_template_data_q2_employee_name_for_3g3a_forms(self) -> None:
        payload = {
            "template_data": {
                "q2_employee_name": "Taylor Jones",
            }
        }
        out = _normalize_name_fields(payload)
        self.assertEqual(out["grievant_firstname"], "Taylor")
        self.assertEqual(out["grievant_lastname"], "Jones")


if __name__ == "__main__":
    unittest.main()
