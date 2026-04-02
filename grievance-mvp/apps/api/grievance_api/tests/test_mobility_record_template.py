from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from grievance_api.services.doc_render import render_docx
from grievance_api.services.docuseal_client import DocuSealClient
from grievance_api.services.pdf_convert import docx_to_pdf


class MobilityRecordTemplateTests(unittest.TestCase):
    def test_template_renders_cleanly_and_exposes_expected_staged_anchors(self) -> None:
        template_path = self._find_template()
        if template_path is None:
            self.skipTest("mobility_record_of_grievance template not available in test environment")
        if shutil.which("soffice") is None:
            self.skipTest("soffice not available in test environment")

        context = {
            "cw_grievance_number": "2026001",
            "district_grievance_number": "D-2026-001",
            "date_grievance_occurred": "2026-04-02",
            "department": "Mobility Care",
            "specific_location_state": "Jacksonville, FL",
            "local_number": "3106",
            "employee_work_group_name": "John Doe",
            "job_title": "Retail Sales Consultant",
            "ncs_date": "2021-07-12",
            "union_statement": "Company failed to honor the agreed scheduling rotation.",
            "contract_articles": "Article 10, Article 18",
            "date_informal": "2026-04-03",
            "date_first_step_requested": "2026-04-04",
            "date_first_step_held": "2026-04-08",
        }

        with zipfile.ZipFile(template_path) as zf:
            template_xml = zf.read("word/document.xml").decode("utf-8", "ignore")

        for token in (
            "{{Txt_es_:signer1:union_position_first_level}}",
            "{{Txt_es_:signer2:company_statement_first_level}}",
            "{{Sig_es_:signer2:signature_company_disp_l1}}",
            "{{Dte_es_:signer2:cd1}}",
            "{{Txt_es_:signer3:union_disposition_second_level}}",
            "{{Sig_es_:signer3:signature_union_pos_l2}}",
        ):
            self.assertIn(token, template_xml)

        with tempfile.TemporaryDirectory() as tmp:
            anchor_path = Path(tmp) / "mobility-record-anchor.docx"
            rendered_path = Path(tmp) / "mobility-record-rendered.docx"
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

            self.assertIn("Txt_es_:signer2:company_position_second_level", anchor_xml)
            self.assertIn("Sig_es_:signer3:signature_union_disp_l2", anchor_xml)
            self.assertIn("Dte_es_:signer3:ud2", anchor_xml)
            self.assertNotIn("{{", rendered_xml)
            self.assertIn("2026001", rendered_xml)
            self.assertIn("Jacksonville, FL", rendered_xml)
            self.assertIn("Company failed to honor the agreed scheduling rotation.", rendered_xml)

            anchor_pdf = docx_to_pdf(
                str(anchor_path),
                tmp,
                timeout_seconds=45,
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

        self.assertEqual(
            set(areas.keys()),
            {
                (1, "text:union_position_first_level"),
                (1, "signature:signature_union_pos_l1"),
                (1, "date:u1"),
                (2, "text:company_statement_first_level"),
                (2, "text:proposed_disposition_first_level"),
                (2, "signature:signature_company_disp_l1"),
                (2, "date:cd1"),
                (2, "text:proposed_disposition_second_level"),
                (2, "signature:signature_company_disp_l2"),
                (2, "date:cd2"),
                (2, "text:company_position_first_level"),
                (2, "signature:signature_company_pos_l1"),
                (2, "date:p1"),
                (2, "text:company_position_second_level"),
                (2, "signature:cp2s"),
                (2, "date:p2"),
                (3, "text:union_disposition_first_level"),
                (3, "signature:signature_union_disp_l1"),
                (3, "date:ud1"),
                (3, "text:union_disposition_second_level"),
                (3, "signature:signature_union_disp_l2"),
                (3, "date:ud2"),
                (3, "text:union_position_second_level"),
                (3, "signature:signature_union_pos_l2"),
                (3, "date:u2"),
            },
        )

    @staticmethod
    def _find_template() -> Path | None:
        direct_candidates = [
            Path("/app/templates/docx/mobility_record_of_grievance_revised.docx"),
            Path("Docx Files Template/mobility_record_of_grievance_revised.docx"),
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                return candidate

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "Docx Files Template" / "mobility_record_of_grievance_revised.docx"
            if candidate.exists():
                return candidate
        return None


if __name__ == "__main__":
    unittest.main()
