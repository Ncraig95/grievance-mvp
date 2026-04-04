from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from grievance_api.services.doc_render import render_docx
from grievance_api.services.docuseal_client import DocuSealClient
from grievance_api.services.pdf_convert import docx_to_pdf


class NonDisciplineTemplateTests(unittest.TestCase):
    def test_template_renders_cleanly_and_exposes_signature_anchors(self) -> None:
        template_path = self._find_template()
        if template_path is None:
            self.skipTest("non_discipline_grievance_brief template not available in test environment")
        if shutil.which("soffice") is None:
            self.skipTest("soffice not available in test environment")

        context = {
            "grievant_name": "Taylor Jones",
            "local_number": "3106",
            "local_grievance_number": "Local-26-001",
            "location": "Jacksonville, FL",
            "grievant_or_work_group": "Taylor Jones",
            "grievant_home_address": "123 Main St, Jacksonville, FL 32202",
            "date_grievance_occurred": "2026-04-02",
            "date_grievance_filed": "2026-04-03",
            "date_grievance_appealed_to_executive_level": "2026-04-10",
            "issue_or_condition_involved": "Management denied agreed scheduling rights.",
            "action_taken": "Steward requested immediate correction and meeting.",
            "chronology_of_facts": "04/02 event occurred. 04/03 grievance filed.",
            "analysis_of_grievance": "The facts and contract language support the union position.",
            "current_status": "Condition remains unresolved.",
            "union_position": "Union requests a full corrective remedy.",
            "company_position": "Management claims the action was operationally necessary.",
            "potential_witnesses": "Taylor Jones, Chris Smith",
            "recommendation": "Advance the grievance and seek full make-whole relief.",
            "attachment_1": "Exhibit A - Timeline",
            "attachment_2": "Exhibit B - Witness statement",
        }

        with zipfile.ZipFile(template_path) as zf:
            template_xml = zf.read("word/document.xml").decode("utf-8", "ignore")

        self.assertIn("{{ issue_or_condition_involved }}", template_xml)
        self.assertIn("{{ action_taken }}", template_xml)
        self.assertIn("{{ potential_witnesses }}", template_xml)
        self.assertIn("{{ recommendation }}", template_xml)
        self.assertIn("{{Sig_es_:signer1:signature}}", template_xml)
        self.assertIn("{{Dte_es_:signer1:date}}", template_xml)

        with tempfile.TemporaryDirectory() as tmp:
            anchor_path = Path(tmp) / "non-discipline-anchor.docx"
            rendered_path = Path(tmp) / "non-discipline-rendered.docx"
            render_docx(
                str(template_path),
                context,
                str(anchor_path),
                strip_signature_placeholders=False,
                normalize_split_placeholders=True,
            )
            render_docx(
                str(template_path),
                context,
                str(rendered_path),
                strip_signature_placeholders=True,
                normalize_split_placeholders=True,
            )

            with zipfile.ZipFile(anchor_path) as zf:
                anchor_xml = zf.read("word/document.xml").decode("utf-8", "ignore")
            with zipfile.ZipFile(rendered_path) as zf:
                rendered_xml = zf.read("word/document.xml").decode("utf-8", "ignore")

            self.assertIn("Sig_es_:signer1:signature", anchor_xml)
            self.assertIn("Dte_es_:signer1:date", anchor_xml)
            self.assertNotIn("{{", rendered_xml)
            self.assertIn("Management denied agreed scheduling rights.", rendered_xml)
            self.assertIn("Steward requested immediate correction and meeting.", rendered_xml)
            self.assertIn("Advance the grievance and seek full make-whole relief.", rendered_xml)

            anchor_pdf = docx_to_pdf(
                str(anchor_path),
                tmp,
                45,
                engine="libreoffice",
                graph_uploader=None,
                graph_site_hostname="",
                graph_site_path="",
                graph_library="",
                graph_temp_folder_path="",
            )
            areas = DocuSealClient("http://docuseal", "token")._extract_placeholder_areas(
                pdf_bytes=Path(anchor_pdf).read_bytes()
            )
            self.assertEqual(sorted(areas.keys()), [(1, "date"), (1, "signature")])

    @staticmethod
    def _find_template() -> Path | None:
        direct_candidates = [
            Path("/app/templates/docx/Non Discipline guide to staff 2010.docx"),
            Path("Docx Files Template/Non Discipline guide to staff 2010.docx"),
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                return candidate

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "Docx Files Template" / "Non Discipline guide to staff 2010.docx"
            if candidate.exists():
                return candidate
        return None


if __name__ == "__main__":
    unittest.main()
