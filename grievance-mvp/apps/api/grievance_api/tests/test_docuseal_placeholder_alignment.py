from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from grievance_api.services.docuseal_client import DocuSealClient


class DocuSealPlaceholderAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = DocuSealClient("http://docuseal:3000", "token")

    def test_extract_placeholder_areas_matches_tokens_without_braces(self) -> None:
        bbox = """
<doc>
  <page width="612.000000" height="792.000000">
    <word xMin="122.612000" yMin="479.826000" xMax="225.068000" yMax="489.130000">Sig_es_:signer1:signature</word>
    <word xMin="400.817500" yMin="480.290000" xMax="480.040000" yMax="489.012500">Dte_es_:signer1:date</word>
  </page>
</doc>
""".strip()
        with patch("grievance_api.services.docuseal_client.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout=bbox, stderr="")
            areas = self.client._extract_placeholder_areas(pdf_bytes=b"fake")

        self.assertIn((1, "signature"), areas)
        self.assertIn((1, "date"), areas)
        self.assertEqual(areas[(1, "signature")][0]["page"], 0)
        self.assertAlmostEqual(areas[(1, "signature")][0]["page_w"], 612.0)
        self.assertAlmostEqual(areas[(1, "signature")][0]["page_h"], 792.0)

    def test_extract_placeholder_areas_matches_tokens_with_braces_and_spaces(self) -> None:
        bbox = """
<doc>
  <page width="612.000000" height="792.000000">
    <word xMin="100.0" yMin="100.0" xMax="220.0" yMax="112.0">{{ Sig_es_:signer2:signature }}</word>
    <word xMin="100.0" yMin="120.0" xMax="220.0" yMax="132.0">{{Dte_es_:signer2:date}}</word>
    <word xMin="100.0" yMin="140.0" xMax="220.0" yMax="152.0">{{ Eml_es_:signer2:email }}</word>
    <word xMin="100.0" yMin="160.0" xMax="380.0" yMax="178.0">{{ Txt_es_:signer2:q6_company_statement }}</word>
  </page>
</doc>
""".strip()
        with patch("grievance_api.services.docuseal_client.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout=bbox, stderr="")
            areas = self.client._extract_placeholder_areas(pdf_bytes=b"fake")

        self.assertIn((2, "signature"), areas)
        self.assertIn((2, "date"), areas)
        self.assertIn((2, "email"), areas)
        self.assertIn((2, "text:q6_company_statement"), areas)

    def test_normalize_area_uses_text_dimension_hints(self) -> None:
        area = self.client._normalize_area(
            raw={
                "x_min": 100.0,
                "y_min": 200.0,
                "x_max": 180.0,
                "y_max": 212.0,
                "page": 0,
                "page_w": 612.0,
                "page_h": 792.0,
            },
            field_type="text",
            attachment_uuid="att",
            field_name="q6_company_statement",
        )
        self.assertGreaterEqual(area["w"], 430.0 / 612.0)
        self.assertGreaterEqual(area["h"], 46.0 / 792.0)
        self.assertLessEqual(area["h"], 56.0 / 792.0)

    def test_normalize_area_uses_date_dimension_hints(self) -> None:
        area = self.client._normalize_area(
            raw={
                "x_min": 420.0,
                "y_min": 360.0,
                "x_max": 490.0,
                "y_max": 372.0,
                "page": 0,
                "page_w": 612.0,
                "page_h": 792.0,
            },
            field_type="date",
            attachment_uuid="att",
            field_name="q5_l2_date",
        )
        self.assertGreaterEqual(area["w"], 140.0 / 612.0)
        self.assertGreaterEqual(area["h"], 20.0 / 792.0)
        self.assertLessEqual(area["h"], 28.0 / 792.0)

    def test_normalize_area_q7_name_field_is_shifted_up(self) -> None:
        raw = {
            "x_min": 90.0,
            "y_min": 478.0,
            "x_max": 250.0,
            "y_max": 486.0,
            "page": 0,
            "page_w": 612.0,
            "page_h": 792.0,
        }
        default_area = self.client._normalize_area(
            raw=raw,
            field_type="text",
            attachment_uuid="att",
            field_name="generic_name_field",
        )
        tuned_area = self.client._normalize_area(
            raw=raw,
            field_type="text",
            attachment_uuid="att",
            field_name="q7_company_rep_name_attuid",
        )
        self.assertLess(tuned_area["y"], default_area["y"])
        self.assertLessEqual(tuned_area["h"], default_area["h"])

    def test_extract_placeholder_areas_supports_true_intent_signature_and_date(self) -> None:
        bbox = """
<doc>
  <page width="612.000000" height="792.000000">
    <word xMin="120.0" yMin="200.0" xMax="330.0" yMax="212.0">Sig_es_:signer3:signature_true_intent</word>
    <word xMin="360.0" yMin="200.0" xMax="520.0" yMax="212.0">Dte_es_:signer3:date_true_intent</word>
  </page>
</doc>
""".strip()
        with patch("grievance_api.services.docuseal_client.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout=bbox, stderr="")
            areas = self.client._extract_placeholder_areas(pdf_bytes=b"fake")

        self.assertIn((3, "signature:signature_true_intent"), areas)
        self.assertIn((3, "date:date_true_intent"), areas)

    def test_alignment_sets_required_for_text_except_true_intent(self) -> None:
        areas = {
            (1, "text:q6_company_statement"): [
                {
                    "x_min": 100.0,
                    "y_min": 300.0,
                    "x_max": 220.0,
                    "y_max": 312.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "text:q10_company_true_intent_note"): [
                {
                    "x_min": 100.0,
                    "y_min": 340.0,
                    "x_max": 240.0,
                    "y_max": 352.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "signature:signature_true_intent"): [
                {
                    "x_min": 260.0,
                    "y_min": 380.0,
                    "x_max": 360.0,
                    "y_max": 392.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
        }
        template_obj = {
            "submitters": [{"uuid": "sub1"}],
            "schema": [{"attachment_uuid": "att"}],
        }

        with patch.object(self.client, "_extract_placeholder_areas", return_value=areas):
            with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                    mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: template_obj)
                    self.client._apply_placeholder_field_alignment(template_id="123", pdf_bytes=b"fake")

        self.assertTrue(mock_patch.called)
        fields = mock_patch.call_args.kwargs["json"]["fields"]
        by_name = {str(field.get("name", "")): field for field in fields}

        self.assertTrue(by_name["q6_company_statement"]["required"])
        self.assertEqual(by_name["q6_company_statement"]["preferences"], {"multiline": True})
        self.assertFalse(by_name["q10_company_true_intent_note"]["required"])

        signature_true_intent = next(
            field
            for field in fields
            if field.get("type") == "signature" and str(field.get("name", "")).lower() == "signature_true_intent"
        )
        self.assertFalse(signature_true_intent["required"])


if __name__ == "__main__":
    unittest.main()
