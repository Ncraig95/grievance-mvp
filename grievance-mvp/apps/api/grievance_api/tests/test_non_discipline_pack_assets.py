from __future__ import annotations

import json
import unittest
from pathlib import Path


class NonDisciplinePackAssetTests(unittest.TestCase):
    def test_catalog_and_generated_payload_include_non_discipline_brief(self) -> None:
        repo_root = self._repo_root()
        catalog_path = repo_root / "scripts" / "power-platform" / "forms.catalog.json"
        payload_path = repo_root / "scripts" / "power-platform" / "output" / "non_discipline_brief" / "non_discipline_brief.http-body.json"

        with catalog_path.open("r", encoding="utf-8") as fh:
            catalog = json.load(fh)
        form = next((item for item in catalog["forms"] if item["key"] == "non_discipline_brief"), None)
        self.assertIsNotNone(form)
        self.assertEqual(form["documentCommand"], "non_discipline_brief")

        with payload_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["document_command"], "non_discipline_brief")
        self.assertEqual(payload["contract"], "CWA")
        self.assertEqual(payload["narrative"], "Non-discipline grievance brief")

        template_data = payload["template_data"]
        for key in (
            "grievant_name",
            "local_number",
            "local_grievance_number",
            "location",
            "grievant_or_work_group",
            "grievant_home_address",
            "date_grievance_occurred",
            "date_grievance_filed",
            "date_grievance_appealed_to_executive_level",
            "issue_or_condition_involved",
            "action_taken",
            "chronology_of_facts",
            "analysis_of_grievance",
            "current_status",
            "union_position",
            "company_position",
            "potential_witnesses",
            "recommendation",
            "signer_email",
        ):
            self.assertIn(key, template_data)

    @staticmethod
    def _repo_root() -> Path:
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "scripts" / "power-platform" / "forms.catalog.json").exists():
                return parent
        raise AssertionError("repo root not found")


if __name__ == "__main__":
    unittest.main()
