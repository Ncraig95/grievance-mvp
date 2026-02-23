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
        self.assertGreaterEqual(area["h"], 132.0 / 792.0)


if __name__ == "__main__":
    unittest.main()
