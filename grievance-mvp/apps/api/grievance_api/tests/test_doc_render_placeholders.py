from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from grievance_api.services.doc_render import (
    _normalize_split_placeholders_in_xml,
    _replace_leftover_placeholders,
    _sync_checkbox_content_controls,
    render_docx,
)


class DocRenderPlaceholderTests(unittest.TestCase):
    def test_email_signature_tags_are_stripped_for_signing_output(self) -> None:
        xml = "<w:t>{{Eml_es_:signer1:email}}</w:t>"
        rendered = _replace_leftover_placeholders(
            xml,
            {},
            strip_signature_placeholders=True,
        )
        self.assertEqual(rendered, "<w:t></w:t>")

    def test_email_signature_tags_remain_for_anchor_output(self) -> None:
        xml = "<w:t>{{Eml_es_:signer1:email}}</w:t>"
        rendered = _replace_leftover_placeholders(
            xml,
            {},
            strip_signature_placeholders=False,
        )
        self.assertEqual(rendered, xml)

    def test_text_signature_tags_are_stripped_for_signing_output(self) -> None:
        xml = "<w:t>{{ Txt_es_:signer2:q6_company_statement }}</w:t>"
        rendered = _replace_leftover_placeholders(
            xml,
            {},
            strip_signature_placeholders=True,
        )
        self.assertEqual(rendered, "<w:t></w:t>")

    def test_true_intent_signature_tags_are_stripped_for_signing_output(self) -> None:
        xml = "<w:t>{{ Sig_es_:signer3:signature_true_intent }}</w:t>"
        rendered = _replace_leftover_placeholders(
            xml,
            {},
            strip_signature_placeholders=True,
        )
        self.assertEqual(rendered, "<w:t></w:t>")

    def test_checkbox_content_control_checked_state_follows_rendered_tag_value(self) -> None:
        xml = (
            '<w:sdt><w:sdtPr><w:tag w:val="{{ q10_company_is_yes_mark }}"/>'
            '<w14:checkbox><w14:checked w14:val="0"/>'
            '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
            '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
            "</w14:checkbox></w:sdtPr><w:sdtContent><w:r><w:t>☐</w:t></w:r></w:sdtContent></w:sdt>"
        )
        rendered = _replace_leftover_placeholders(
            xml,
            {"q10_company_is_yes_mark": "☒"},
            strip_signature_placeholders=False,
        )
        synced = _sync_checkbox_content_controls(rendered)
        self.assertIn('w14:checked w14:val="1"', synced)
        self.assertIn("<w:t>☒</w:t>", synced)

    def test_checkbox_content_control_unchecked_state_follows_rendered_tag_value(self) -> None:
        xml = (
            '<w:sdt><w:sdtPr><w:tag w:val="{{ q10_company_is_no_mark }}"/>'
            '<w14:checkbox><w14:checked w14:val="1"/>'
            '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
            '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
            "</w14:checkbox></w:sdtPr><w:sdtContent><w:r><w:t>☒</w:t></w:r></w:sdtContent></w:sdt>"
        )
        rendered = _replace_leftover_placeholders(
            xml,
            {"q10_company_is_no_mark": "☐"},
            strip_signature_placeholders=False,
        )
        synced = _sync_checkbox_content_controls(rendered)
        self.assertIn('w14:checked w14:val="0"', synced)
        self.assertIn("<w:t>☐</w:t>", synced)

    def test_checkbox_content_control_handles_self_closing_text_node(self) -> None:
        xml = (
            '<w:sdt><w:sdtPr><w:tag w:val="{{ q10_company_is_yes_mark }}"/>'
            '<w14:checkbox><w14:checked w14:val="0"/>'
            '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
            '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
            "</w14:checkbox></w:sdtPr><w:sdtContent><w:r><w:t xml:space=\"preserve\"/></w:r></w:sdtContent></w:sdt>"
        )
        rendered = _replace_leftover_placeholders(
            xml,
            {"q10_company_is_yes_mark": "☒"},
            strip_signature_placeholders=False,
        )
        synced = _sync_checkbox_content_controls(rendered)
        self.assertIn('w14:checked w14:val="1"', synced)
        self.assertIn("<w:t xml:space=\"preserve\">☒</w:t>", synced)

    def test_checkbox_sync_does_not_overwrite_non_checkbox_text(self) -> None:
        xml = (
            '<w:sdt><w:sdtPr><w:tag w:val="{{ q1_is_bst_mark }}"/>'
            '<w14:checkbox><w14:checked w14:val="0"/>'
            '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
            '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
            "</w14:checkbox></w:sdtPr><w:sdtContent><w:r><w:t>2026062</w:t></w:r></w:sdtContent></w:sdt>"
        )
        rendered = _replace_leftover_placeholders(
            xml,
            {"q1_is_bst_mark": "☒"},
            strip_signature_placeholders=False,
        )
        synced = _sync_checkbox_content_controls(rendered)
        self.assertIn("<w:t>2026062</w:t>", synced)

    def test_split_placeholder_across_runs_is_normalized(self) -> None:
        xml = (
            "<w:r><w:t>{{ </w:t></w:r>"
            "<w:proofErr w:type=\"spellStart\"/>"
            "<w:r><w:t>grievance_number</w:t></w:r>"
            "<w:proofErr w:type=\"spellEnd\"/>"
            "<w:r><w:t> }}</w:t></w:r>"
        )
        normalized = _normalize_split_placeholders_in_xml(xml)
        self.assertIn("{{ grievance_number }}", normalized)
        self.assertNotIn("{{ </w:t>", normalized)

    def test_statement_template_render_has_no_leftover_placeholders_in_document_or_header(self) -> None:
        template_path = self._find_statement_template()
        if template_path is None:
            self.skipTest("statement_of_occurrence template not available in test environment")

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "rendered.docx"
            render_docx(
                str(template_path),
                {
                    "grievance_id": "2026001",
                    "grievance_number": "2026001",
                    "statement_rows": [{"text": "Test statement row."}],
                },
                str(out_path),
                strip_signature_placeholders=True,
                normalize_split_placeholders=True,
            )

            with zipfile.ZipFile(out_path) as zf:
                document_xml = zf.read("word/document.xml").decode("utf-8", "ignore")
                header2_xml = zf.read("word/header2.xml").decode("utf-8", "ignore")

            self.assertNotIn("{{", document_xml)
            self.assertNotIn("{{", header2_xml)
            self.assertIn("2026001", header2_xml)

    @staticmethod
    def _find_statement_template() -> Path | None:
        direct_candidates = [
            Path("/app/templates/docx/statement_of_occurrence fixed.docx"),
            Path("Docx Files Template/statement_of_occurrence fixed.docx"),
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                return candidate

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "Docx Files Template" / "statement_of_occurrence fixed.docx"
            if candidate.exists():
                return candidate
        return None


if __name__ == "__main__":
    unittest.main()
