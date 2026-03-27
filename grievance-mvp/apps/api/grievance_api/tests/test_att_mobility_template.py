from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from grievance_api.services.doc_render import render_docx
from grievance_api.services.docuseal_client import DocuSealClient
from grievance_api.services.pdf_convert import docx_to_pdf


class AttMobilityTemplateTests(unittest.TestCase):
    def test_template_renders_cleanly_and_exposes_only_one_signature_anchor(self) -> None:
        template_path = self._find_template()
        if template_path is None:
            self.skipTest("AT&T Mobility bargaining template not available in test environment")

        context = {
            "local_number": "3106",
            "demand_from_local": "CWA 3106",
            "submitting_member_title": "Steward",
            "submitting_member_name": "Jane Doe",
            "demand_rows": [
                {"text": "Increase staffing for mobility teams.", "line_no": 1},
                {"text": "Backfill every uncovered shift.", "line_no": 2},
            ],
            "reason_rows": [
                {"text": "Current staffing levels delay customer support.", "line_no": 1},
                {"text": "Technicians are covering too many zones.", "line_no": 2},
            ],
            "specific_examples_rows": [
                {"text": "Three shifts ran short this week.", "line_no": 1},
                {"text": "Weekend dispatch fell behind twice.", "line_no": 2},
            ],
            "work_phone": "555-1111",
            "home_phone": "555-2222",
            "non_work_email": "jane@example.org",
        }

        with zipfile.ZipFile(template_path) as zf:
            template_xml = zf.read("word/document.xml").decode("utf-8", "ignore")
            footer_xml = zf.read("word/footer1.xml").decode("utf-8", "ignore")

        self.assertIn("{%tr for line in demand_rows %}", template_xml)
        self.assertIn("{%tr for line in reason_rows %}", template_xml)
        self.assertIn("{%tr for line in specific_examples_rows %}", template_xml)
        self.assertNotIn("{%p for line in demand_rows %}", template_xml)
        self.assertIn('PAGE', footer_xml)

        with tempfile.TemporaryDirectory() as tmp:
            anchor_path = Path(tmp) / "anchor.docx"
            rendered_path = Path(tmp) / "rendered.docx"
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
            self.assertIn("Txt_es_:signer1:article_affected", anchor_xml)
            self.assertNotIn("Dte_es_:", anchor_xml)
            self.assertNotIn("{{", rendered_xml)
            self.assertNotIn("{%tr", rendered_xml)
            self.assertIn("Backfill every uncovered shift.", rendered_xml)
            self.assertIn("Technicians are covering too many zones.", rendered_xml)
            self.assertIn("Weekend dispatch fell behind twice.", rendered_xml)

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
            self.assertEqual(sorted(areas.keys()), [(1, "signature"), (1, "text:article_affected")])

    @staticmethod
    def _find_template() -> Path | None:
        direct_candidates = [
            Path("/app/templates/docx/AT&TMobility2023BargainingSuggestion_Form.docx"),
            Path("Docx Files Template/AT&TMobility2023BargainingSuggestion_Form.docx"),
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                return candidate

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "Docx Files Template" / "AT&TMobility2023BargainingSuggestion_Form.docx"
            if candidate.exists():
                return candidate
        return None


if __name__ == "__main__":
    unittest.main()
