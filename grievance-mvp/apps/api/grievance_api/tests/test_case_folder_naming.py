from __future__ import annotations

import unittest

from grievance_api.services.case_folder_naming import build_case_folder_member_name, resolve_contract_label


class CaseFolderNamingTests(unittest.TestCase):
    def test_build_case_folder_member_name_with_contract(self) -> None:
        self.assertEqual(build_case_folder_member_name("John Doe", "COJ"), "John Doe - COJ")

    def test_build_case_folder_member_name_without_contract(self) -> None:
        self.assertEqual(build_case_folder_member_name("John Doe", ""), "John Doe")

    def test_build_case_folder_member_name_sanitizes_sharepoint_chars(self) -> None:
        self.assertEqual(
            build_case_folder_member_name(' John/ Doe ', 'AT&T:Public*Works'),
            "John Doe - AT&T Public Works",
        )

    def test_resolve_contract_label(self) -> None:
        self.assertEqual(resolve_contract_label('{"contract":"COJ"}'), "COJ")
        self.assertEqual(resolve_contract_label('{"contractType":"AT&T"}'), "AT&T")
        self.assertEqual(resolve_contract_label('{"contract_type":"COJ"}'), "COJ")

    def test_resolve_contract_label_handles_invalid_payload(self) -> None:
        self.assertIsNone(resolve_contract_label(None))
        self.assertIsNone(resolve_contract_label(""))
        self.assertIsNone(resolve_contract_label("not-json"))
        self.assertIsNone(resolve_contract_label('{"contract":"   "}'))


if __name__ == "__main__":
    unittest.main()
