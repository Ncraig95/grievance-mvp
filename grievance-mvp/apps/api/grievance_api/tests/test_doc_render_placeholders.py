from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from grievance_api.services.docuseal_client import DocuSealClient
from grievance_api.services.pdf_convert import docx_to_pdf
from grievance_api.services.doc_render import (
    _normalize_split_placeholders_in_xml,
    _replace_leftover_placeholders,
    _sanitize_legacy_textinput_checkbox_artifacts,
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

    def test_legacy_textinput_checkbox_artifact_is_stripped(self) -> None:
        xml = (
            '<w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Text3"/>'
            "<w:textInput/></w:ffData></w:fldChar>"
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>☐</w:t></w:r><w:r><w:t>\u2002\u2002\u2002</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        )
        sanitized = _sanitize_legacy_textinput_checkbox_artifacts(xml)
        self.assertNotIn("☐", sanitized)
        self.assertIn("\u2002\u2002\u2002", sanitized)

    def test_legacy_textinput_with_real_text_is_preserved(self) -> None:
        xml = (
            '<w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Text3"/>'
            "<w:textInput/></w:ffData></w:fldChar>"
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>3106</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        )
        sanitized = _sanitize_legacy_textinput_checkbox_artifacts(xml)
        self.assertIn("<w:t>3106</w:t>", sanitized)

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

    def test_settlement_template_long_render_keeps_both_signer_rows_on_one_page(self) -> None:
        template_path = self._find_settlement_template()
        if template_path is None:
            self.skipTest("settlement template not available in test environment")
        if shutil.which("soffice") is None:
            self.skipTest("soffice not available in test environment")

        context = {
            "grievant_name": "Enis Sujak",
            "grievance_number": "2026016",
            "informal_meeting_date": "2026-03-24",
            "company_rep_attending": "Robert Bendle",
            "union_rep_attending": "Nicholas Craig",
            "issue_article": "21",
            "issue_rows": [
                {"text": "Company did not payout full amount of you refer", "line_no": 1},
            ],
            "settlement_rows": [
                {"text": "Resolved by You Refer Escalation. The Company agrees to correct You Refer ID", "line_no": 1},
                {"text": "251217J915543 to", "line_no": 2},
                {"text": "the proper referral type and process the payout correction so the total payout is", "line_no": 3},
                {"text": "$2,000.00,", "line_no": 4},
                {"text": "which will be paid with the next month's You Refer reward drop. Upon completion,", "line_no": 5},
                {"text": "Grievance ID 2026016 will be considered resolved.", "line_no": 6},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "settlement-anchor.docx"
            render_docx(
                str(template_path),
                context,
                str(out_path),
                strip_signature_placeholders=False,
                normalize_split_placeholders=True,
            )
            pdf_path = docx_to_pdf(str(out_path), tmp, timeout_seconds=45)
            pdf_bytes = Path(pdf_path).read_bytes()

        client = DocuSealClient("http://docuseal:3000", "token")
        areas = client._extract_placeholder_areas(pdf_bytes=pdf_bytes)

        self.assertEqual(
            set(areas.keys()),
            {
                (1, "signature"),
                (1, "date"),
                (2, "signature"),
                (2, "date"),
            },
        )
        pages = {int(anchors[0]["page"]) for anchors in areas.values()}
        self.assertEqual(len(pages), 1)

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

    @staticmethod
    def _find_settlement_template() -> Path | None:
        direct_candidates = [
            Path("/app/templates/docx/Settlement Form 3106.docx"),
            Path("Docx Files Template/Settlement Form 3106.docx"),
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                return candidate

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "Docx Files Template" / "Settlement Form 3106.docx"
            if candidate.exists():
                return candidate
        return None


if __name__ == "__main__":
    unittest.main()
