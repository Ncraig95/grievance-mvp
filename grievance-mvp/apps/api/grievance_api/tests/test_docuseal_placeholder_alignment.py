from __future__ import annotations

import requests
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from grievance_api.services.docuseal_client import DocuSealClient


class DocuSealPlaceholderAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = DocuSealClient("http://docuseal:3000", "token")

    @staticmethod
    def _sample_3g3a_stage3_words() -> list[dict]:
        page_w = 612.0
        page_h = 792.0

        def _word(text: str, x0: float, y0: float, x1: float, y1: float) -> dict:
            return {
                "text": text,
                "x_min": x0,
                "y_min": y0,
                "x_max": x1,
                "y_max": y1,
                "page": 0,
                "page_w": page_w,
                "page_h": page_h,
            }

        return [
            _word("☐", 42.5, 493.9, 49.5, 500.9),
            _word("Accepted", 51.8, 493.6, 80.9, 501.4),
            _word("☐", 86.5, 493.9, 93.5, 500.9),
            _word("Rejected", 98.1, 493.6, 125.7, 501.4),
            _word("☐", 42.5, 503.1, 49.5, 510.1),
            _word("Appealed", 51.8, 502.8, 81.2, 510.6),
            _word("☐", 42.5, 512.2, 49.5, 519.2),
            _word("Requested", 51.8, 511.8, 85.4, 519.6),
            _word("Mediation", 87.5, 511.8, 117.8, 519.6),
            _word("☐", 146.6, 575.2, 153.6, 582.2),
            _word("Yes", 157.6, 574.8, 169.6, 582.6),
            _word("☐", 175.3, 575.2, 182.3, 582.2),
            _word("No", 186.3, 574.8, 195.2, 582.6),
            _word("☐", 409.2, 575.2, 416.2, 582.2),
            _word("Yes", 420.2, 574.8, 432.2, 582.6),
            _word("☐", 437.9, 575.2, 444.9, 582.2),
            _word("No", 448.9, 574.8, 457.8, 582.6),
        ]

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

    def test_extract_placeholder_areas_reassembles_wrapped_date_token(self) -> None:
        bbox = """
<doc>
  <page width="612.000000" height="792.000000">
    <word xMin="498.700000" yMin="597.115000" xMax="578.171000" yMax="604.920000">{{Dte_es_:signer1:date_tr</word>
    <word xMin="498.700000" yMin="605.365000" xMax="532.237000" yMax="613.170000">ue_intent}}</word>
  </page>
</doc>
""".strip()
        with patch("grievance_api.services.docuseal_client.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout=bbox, stderr="")
            areas = self.client._extract_placeholder_areas(pdf_bytes=b"fake")

        self.assertIn((1, "date:date_true_intent"), areas)
        merged = areas[(1, "date:date_true_intent")][0]
        self.assertAlmostEqual(merged["y_min"], 597.115, places=3)
        self.assertAlmostEqual(merged["y_max"], 604.920, places=3)

    def test_inject_3g3a_missing_date_anchor_for_stage2_signature_row(self) -> None:
        placeholder_areas = {
            (1, "signature"): [
                {
                    "x_min": 303.380000,
                    "y_min": 477.815000,
                    "x_max": 393.582000,
                    "y_max": 485.620000,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "text:q7_company_rep_name_attuid"): [
                {
                    "x_min": 91.550000,
                    "y_min": 477.815000,
                    "x_max": 249.176000,
                    "y_max": 485.620000,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "date:q5_l2_date"): [
                {
                    "x_min": 400.420000,
                    "y_min": 332.610000,
                    "x_max": 511.228000,
                    "y_max": 341.530000,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
        }

        out = self.client._inject_3g3a_missing_date_anchors(
            placeholder_areas=placeholder_areas,
            form_key="bst_grievance_form_3g3a",
        )
        self.assertIn((1, "date"), out)
        injected = out[(1, "date")][0]
        self.assertEqual(injected["page"], 0)
        self.assertGreater(injected["x_min"], placeholder_areas[(1, "signature")][0]["x_max"])
        self.assertAlmostEqual(injected["y_min"], placeholder_areas[(1, "signature")][0]["y_min"], places=3)

    def test_inject_3g3a_missing_date_anchor_supports_extension_form(self) -> None:
        placeholder_areas = {
            (1, "signature"): [
                {
                    "x_min": 303.38,
                    "y_min": 477.815,
                    "x_max": 393.582,
                    "y_max": 485.62,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "text:q7_company_rep_name_attuid"): [
                {
                    "x_min": 110.0,
                    "y_min": 498.0,
                    "x_max": 250.0,
                    "y_max": 510.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
        }

        out = self.client._inject_3g3a_missing_date_anchors(
            placeholder_areas=placeholder_areas,
            form_key="bst_grievance_form_3g3a_extension",
        )
        self.assertIn((1, "date"), out)

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

    def test_normalize_area_statement_signer_dates_are_form_scoped(self) -> None:
        raw = {
            "x_min": 420.0,
            "y_min": 360.0,
            "x_max": 490.0,
            "y_max": 372.0,
            "page": 0,
            "page_w": 612.0,
            "page_h": 792.0,
        }
        generic_area = self.client._normalize_area(
            raw=raw,
            field_type="date",
            attachment_uuid="att",
            field_name="signer1_date",
            form_key="bst_grievance_form_3g3a",
        )
        statement_area = self.client._normalize_area(
            raw=raw,
            field_type="date",
            attachment_uuid="att",
            field_name="signer1_date",
            form_key="statement_of_occurrence",
        )
        self.assertGreaterEqual(generic_area["w"], 120.0 / 612.0)
        self.assertGreaterEqual(generic_area["h"], 26.0 / 792.0)
        self.assertLess(statement_area["w"], generic_area["w"])
        self.assertLess(statement_area["h"], generic_area["h"])
        self.assertGreater(statement_area["y"], generic_area["y"])

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

    def test_normalize_area_article_affected_field_uses_tighter_hint(self) -> None:
        raw = {
            "x_min": 303.65,
            "y_min": 121.7975,
            "x_max": 472.1855,
            "y_max": 133.5155,
            "page": 0,
            "page_w": 612.0,
            "page_h": 792.0,
        }
        generic_area = self.client._normalize_area(
            raw=raw,
            field_type="text",
            attachment_uuid="att",
            field_name="generic_text_field",
        )
        article_area = self.client._normalize_area(
            raw=raw,
            field_type="text",
            attachment_uuid="att",
            field_name="article_affected",
        )
        self.assertLess(article_area["h"], generic_area["h"])
        self.assertLess(article_area["w"], generic_area["w"])
        self.assertLess(article_area["y"], generic_area["y"])

    def test_normalize_exact_area_preserves_table_cell_box(self) -> None:
        raw = {
            "x_min": 200.0,
            "y_min": 500.0,
            "x_max": 410.0,
            "y_max": 520.0,
            "page": 0,
            "page_w": 612.0,
            "page_h": 792.0,
        }
        generic_sig = self.client._normalize_area(
            raw=raw,
            field_type="signature",
            attachment_uuid="att",
            field_name="signer1_signature",
        )
        exact = self.client._normalize_exact_area(
            raw=raw,
            attachment_uuid="att",
        )
        self.assertAlmostEqual(exact["x"], 200.0 / 612.0, places=6)
        self.assertAlmostEqual(exact["y"], 500.0 / 792.0, places=6)
        self.assertAlmostEqual(exact["w"], 210.0 / 612.0, places=6)
        self.assertAlmostEqual(exact["h"], 20.0 / 792.0, places=6)
        self.assertGreater(exact["y"], generic_sig["y"])
        self.assertLess(exact["h"], generic_sig["h"])

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
        self.assertEqual(
            by_name["q6_company_statement"]["preferences"],
            {"multiline": True, "align": "left", "valign": "top"},
        )
        self.assertTrue(by_name["signer1_date"]["readonly"])
        self.assertRegex(str(by_name["signer1_date"]["default_value"]), r"^\d{4}-\d{2}-\d{2}$")
        self.assertFalse(by_name["q10_company_true_intent_note"]["required"])

        signature_true_intent = next(
            field
            for field in fields
            if field.get("type") == "signature" and str(field.get("name", "")).lower() == "signature_true_intent"
        )
        self.assertFalse(signature_true_intent["required"])

    def test_build_3g3a_stage_radio_fields_stage3_maps_union_groups(self) -> None:
        with patch.object(self.client, "_extract_pdf_words", return_value=self._sample_3g3a_stage3_words()):
            fields = self.client._build_3g3a_stage_radio_fields(
                pdf_bytes=b"fake",
                submitter_uuid="sub1",
                attachment_uuid="att",
                include_q8_disposition=True,
                include_company_true_intent=False,
                include_union_true_intent=True,
            )

        self.assertEqual(len(fields), 2)
        by_name = {str(field.get("name") or ""): field for field in fields}
        self.assertEqual(by_name["q8_union_disposition_choice"]["type"], "radio")
        self.assertEqual(by_name["q10_union_true_intent_choice"]["type"], "radio")
        self.assertNotIn("q10_company_true_intent_choice", by_name)

        q8_values = [str(opt.get("value") or "") for opt in by_name["q8_union_disposition_choice"]["options"]]
        self.assertListEqual(q8_values, ["Accepted", "Rejected", "Appealed", "Requested Mediation"])
        self.assertEqual(len(by_name["q8_union_disposition_choice"]["areas"]), 4)
        self.assertEqual(len(by_name["q10_union_true_intent_choice"]["areas"]), 2)
        self.assertEqual(by_name["q10_union_true_intent_choice"]["default_value"], "No")
        self.assertTrue(all(float(area.get("x", 1.0)) < 0.5 for area in by_name["q10_union_true_intent_choice"]["areas"]))

        for field in fields:
            option_uuids = {str(opt.get("uuid") or "") for opt in field["options"]}
            for area in field["areas"]:
                self.assertIn(str(area.get("option_uuid") or ""), option_uuids)

    def test_build_3g3a_stage_radio_fields_stage2_maps_company_true_intent_only(self) -> None:
        with patch.object(self.client, "_extract_pdf_words", return_value=self._sample_3g3a_stage3_words()):
            fields = self.client._build_3g3a_stage_radio_fields(
                pdf_bytes=b"fake",
                submitter_uuid="sub1",
                attachment_uuid="att",
                include_q8_disposition=False,
                include_company_true_intent=True,
                include_union_true_intent=False,
            )

        self.assertEqual(len(fields), 1)
        field = fields[0]
        self.assertEqual(field["name"], "q10_company_true_intent_choice")
        self.assertEqual(field["type"], "radio")
        self.assertEqual([str(o.get("value") or "") for o in field["options"]], ["Yes", "No"])
        self.assertEqual(len(field["areas"]), 2)
        self.assertEqual(field["default_value"], "No")
        self.assertTrue(all(float(area.get("x", 0.0)) > 0.5 for area in field["areas"]))

    def test_alignment_adds_3g3a_stage3_radio_fields(self) -> None:
        areas = {
            (1, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 440.0,
                    "x_max": 500.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 440.0,
                    "x_max": 260.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "text:q8_union_rep_name_attuid"): [
                {
                    "x_min": 100.0,
                    "y_min": 480.0,
                    "x_max": 240.0,
                    "y_max": 492.0,
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
            with patch.object(
                self.client,
                "_resolve_signature_table_overrides",
                return_value=({}, "generic_fallback", "layout_mode_generic", {}),
            ):
                with patch.object(self.client, "_extract_pdf_words", return_value=self._sample_3g3a_stage3_words()):
                    with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                        with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                            mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: template_obj)
                            mock_patch.return_value = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
                            self.client._apply_placeholder_field_alignment(
                                template_id="123",
                                pdf_bytes=b"fake",
                                form_key="bst_grievance_form_3g3a",
                            )

        fields = mock_patch.call_args.kwargs["json"]["fields"]
        by_name = {str(field.get("name") or ""): field for field in fields}
        self.assertIn("q8_union_disposition_choice", by_name)
        self.assertIn("q10_union_true_intent_choice", by_name)
        self.assertNotIn("q10_company_true_intent_choice", by_name)
        self.assertEqual(by_name["q8_union_disposition_choice"]["type"], "radio")
        self.assertEqual(by_name["q10_union_true_intent_choice"]["type"], "radio")
        self.assertEqual(by_name["q10_union_true_intent_choice"]["default_value"], "No")
        self.assertTrue(all(float(area.get("x", 1.0)) < 0.5 for area in by_name["q10_union_true_intent_choice"]["areas"]))

    def test_alignment_adds_3g3a_stage2_company_true_intent_radio_only(self) -> None:
        areas = {
            (1, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 440.0,
                    "x_max": 500.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 440.0,
                    "x_max": 260.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "text:q7_company_rep_name_attuid"): [
                {
                    "x_min": 100.0,
                    "y_min": 480.0,
                    "x_max": 240.0,
                    "y_max": 492.0,
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
            with patch.object(
                self.client,
                "_resolve_signature_table_overrides",
                return_value=({}, "generic_fallback", "layout_mode_generic", {}),
            ):
                with patch.object(self.client, "_extract_pdf_words", return_value=self._sample_3g3a_stage3_words()):
                    with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                        with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                            mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: template_obj)
                            mock_patch.return_value = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
                            self.client._apply_placeholder_field_alignment(
                                template_id="123",
                                pdf_bytes=b"fake",
                                form_key="bst_grievance_form_3g3a",
                            )

        fields = mock_patch.call_args.kwargs["json"]["fields"]
        by_name = {str(field.get("name") or ""): field for field in fields}
        self.assertIn("q10_company_true_intent_choice", by_name)
        self.assertNotIn("q10_union_true_intent_choice", by_name)
        self.assertNotIn("q8_union_disposition_choice", by_name)
        self.assertEqual(by_name["q10_company_true_intent_choice"]["type"], "radio")
        self.assertEqual(by_name["q10_company_true_intent_choice"]["default_value"], "No")
        self.assertTrue(all(float(area.get("x", 0.0)) > 0.5 for area in by_name["q10_company_true_intent_choice"]["areas"]))

    def test_alignment_treats_extension_as_3g3a_family(self) -> None:
        areas = {
            (1, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 440.0,
                    "x_max": 500.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 440.0,
                    "x_max": 260.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "text:q7_company_rep_name_attuid"): [
                {
                    "x_min": 100.0,
                    "y_min": 480.0,
                    "x_max": 240.0,
                    "y_max": 492.0,
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
            with patch.object(
                self.client,
                "_resolve_signature_table_overrides",
                return_value=({}, "generic_fallback", "layout_mode_generic", {}),
            ):
                with patch.object(self.client, "_extract_pdf_words", return_value=self._sample_3g3a_stage3_words()):
                    with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                        with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                            mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: template_obj)
                            mock_patch.return_value = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
                            self.client._apply_placeholder_field_alignment(
                                template_id="123",
                                pdf_bytes=b"fake",
                                form_key="bst_grievance_form_3g3a_extension",
                            )

        fields = mock_patch.call_args.kwargs["json"]["fields"]
        by_name = {str(field.get("name") or ""): field for field in fields}
        self.assertIn("q10_company_true_intent_choice", by_name)

    def test_alignment_expands_template_submitters_when_signer_placeholders_exceed_current_count(self) -> None:
        areas = {
            (1, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 440.0,
                    "x_max": 260.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (2, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 470.0,
                    "x_max": 260.0,
                    "y_max": 482.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (3, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 500.0,
                    "x_max": 260.0,
                    "y_max": 512.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
        }
        template_two_submitters = {
            "submitters": [{"uuid": "sub1", "name": "Company"}, {"uuid": "sub2", "name": "Steward"}],
            "schema": [{"attachment_uuid": "att"}],
        }
        template_three_submitters = {
            "submitters": [
                {"uuid": "sub1", "name": "Company"},
                {"uuid": "sub2", "name": "Steward"},
                {"uuid": "sub3", "name": "Grievant"},
            ],
            "schema": [{"attachment_uuid": "att"}],
        }

        with patch.object(self.client, "_extract_placeholder_areas", return_value=areas):
            with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                    mock_get.side_effect = [
                        SimpleNamespace(status_code=200, json=lambda: template_two_submitters),
                        SimpleNamespace(status_code=200, json=lambda: template_three_submitters),
                    ]
                    mock_patch.side_effect = [
                        SimpleNamespace(status_code=200, json=lambda: {"ok": True}),
                        SimpleNamespace(status_code=200, json=lambda: {"ok": True}),
                    ]

                    self.client._apply_placeholder_field_alignment(template_id="123", pdf_bytes=b"fake")

        self.assertEqual(mock_patch.call_count, 2)
        expand_payload = mock_patch.call_args_list[0].kwargs["json"]
        self.assertEqual(len(expand_payload["submitters"]), 3)
        self.assertEqual(expand_payload["submitters"][0]["name"], "Company")
        self.assertEqual(expand_payload["submitters"][1]["name"], "Steward")
        self.assertEqual(expand_payload["submitters"][2]["name"], "Third Party")

        field_payload = mock_patch.call_args_list[1].kwargs["json"]
        self.assertEqual(len(field_payload["fields"]), 3)
        field_submitters = {field["submitter_uuid"] for field in field_payload["fields"]}
        self.assertSetEqual(field_submitters, {"sub1", "sub2", "sub3"})

    def test_alignment_assigns_unique_default_names_for_signature_and_date_fields(self) -> None:
        areas = {
            (1, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 440.0,
                    "x_max": 260.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 440.0,
                    "x_max": 500.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (2, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 470.0,
                    "x_max": 260.0,
                    "y_max": 482.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (2, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 470.0,
                    "x_max": 500.0,
                    "y_max": 482.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (3, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 500.0,
                    "x_max": 260.0,
                    "y_max": 512.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (3, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 500.0,
                    "x_max": 500.0,
                    "y_max": 512.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
        }
        template_obj = {
            "submitters": [
                {"uuid": "sub1", "name": "Company"},
                {"uuid": "sub2", "name": "Steward"},
                {"uuid": "sub3", "name": "Grievant"},
            ],
            "schema": [{"attachment_uuid": "att"}],
        }

        with patch.object(self.client, "_extract_placeholder_areas", return_value=areas):
            with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                    mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: template_obj)
                    mock_patch.return_value = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
                    self.client._apply_placeholder_field_alignment(template_id="123", pdf_bytes=b"fake")

        fields = mock_patch.call_args.kwargs["json"]["fields"]
        self.assertEqual(len(fields), 6)
        names = [str(field.get("name") or "") for field in fields]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("signer1_signature", names)
        self.assertIn("signer2_signature", names)
        self.assertIn("signer3_signature", names)
        self.assertIn("signer1_date", names)
        self.assertIn("signer2_date", names)
        self.assertIn("signer3_date", names)

    def test_create_submission_retries_when_success_response_has_no_submission_id(self) -> None:
        with patch.object(self.client, "_clone_and_replace_template", return_value="83"):
            with patch.object(self.client, "_resolve_signer_email_fields", return_value={}):
                with patch("grievance_api.services.docuseal_client.requests.post") as mock_post:
                    mock_post.side_effect = [
                        SimpleNamespace(status_code=200, json=lambda: []),
                        SimpleNamespace(status_code=200, json=lambda: {"id": "sub123", "slug": "abc"}),
                    ]
                    submission = self.client.create_submission(
                        pdf_bytes=b"%PDF",
                        alignment_pdf_bytes=b"%PDF",
                        signers=["one@example.org", "two@example.org", "three@example.org"],
                        title="settlement",
                        template_id=2,
                    )

        self.assertEqual(submission.submission_id, "sub123")
        self.assertEqual(mock_post.call_count, 2)

    def test_create_submission_uses_random_submitters_order_for_settlement_form(self) -> None:
        client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            submitters_order="preserved",
            submitters_order_by_form={"settlement_form_3106": "random"},
        )
        with patch.object(client, "_clone_and_replace_template", return_value="83"):
            with patch.object(client, "_resolve_signer_email_fields", return_value={}):
                with patch("grievance_api.services.docuseal_client.requests.post") as mock_post:
                    mock_post.return_value = SimpleNamespace(
                        status_code=200,
                        json=lambda: {"id": "sub124", "slug": "xyz"},
                    )
                    submission = client.create_submission(
                        pdf_bytes=b"%PDF",
                        alignment_pdf_bytes=b"%PDF",
                        signers=["one@example.org", "two@example.org", "three@example.org"],
                        title="settlement",
                        template_id=2,
                        form_key="settlement_form_3106",
                    )

        self.assertEqual(submission.submission_id, "sub124")
        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload.get("submitters_order"), "random")

    def test_clone_and_replace_replays_cookie_and_https_origin_for_proxy_web_base(self) -> None:
        class _FakeResponse:
            def __init__(self, *, status_code: int = 200, text: str = "", json_data: dict | None = None) -> None:
                self.status_code = status_code
                self.text = text
                self._json_data = json_data or {}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise requests.HTTPError(f"status={self.status_code}")

            def json(self) -> dict:
                return self._json_data

        class _FakeSession:
            def __init__(self) -> None:
                self.cookies = requests.cookies.RequestsCookieJar()
                self.cookies.set("_docu_seal_session", "session123")
                self.get_calls: list[dict] = []
                self.post_calls: list[dict] = []

            def get(self, url: str, **kwargs) -> _FakeResponse:
                self.get_calls.append({"url": url, **kwargs})
                if url.endswith("/sign_in"):
                    return _FakeResponse(
                        text='<form><input type="hidden" name="authenticity_token" value="sign-in-token" /></form>'
                    )
                if url.endswith("/templates/2/edit"):
                    return _FakeResponse(text='<meta name="csrf-token" content="edit-token" />')
                raise AssertionError(f"unexpected GET {url}")

            def post(self, url: str, **kwargs) -> _FakeResponse:
                self.post_calls.append({"url": url, **kwargs})
                if url.endswith("/sign_in"):
                    return _FakeResponse(status_code=302)
                if url.endswith("/clone_and_replace"):
                    return _FakeResponse(json_data={"id": "83"})
                raise AssertionError(f"unexpected POST {url}")

        client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            web_base_url="http://docuseal_proxy:8080",
            web_email="nick.craig@cwa3106.com",
            web_password="secret",
        )
        fake_session = _FakeSession()

        with patch("grievance_api.services.docuseal_client.requests.Session", return_value=fake_session):
            with patch.object(client, "_apply_placeholder_field_alignment") as mock_align:
                template_id = client._clone_and_replace_template(
                    base_template_id="2",
                    upload_pdf_bytes=b"%PDF",
                    alignment_pdf_bytes=None,
                    title="statement",
                    form_key="statement_of_occurrence",
                )

        self.assertEqual(template_id, "83")
        sign_in_headers = fake_session.post_calls[0]["headers"]
        self.assertEqual(sign_in_headers["Origin"], "https://docuseal_proxy")
        self.assertEqual(sign_in_headers["Referer"], "https://docuseal_proxy/sign_in")
        self.assertEqual(sign_in_headers["Cookie"], "_docu_seal_session=session123")

        edit_headers = fake_session.get_calls[1]["headers"]
        self.assertEqual(edit_headers["Cookie"], "_docu_seal_session=session123")
        self.assertEqual(edit_headers["Referer"], "https://docuseal_proxy/sign_in")

        clone_headers = fake_session.post_calls[1]["headers"]
        self.assertEqual(clone_headers["Origin"], "https://docuseal_proxy")
        self.assertEqual(clone_headers["Referer"], "https://docuseal_proxy/templates/2/edit")
        self.assertEqual(clone_headers["Cookie"], "_docu_seal_session=session123")
        self.assertEqual(clone_headers["X-CSRF-Token"], "edit-token")
        mock_align.assert_called_once()

    def test_clone_and_replace_retries_alignment_when_verified_template_is_missing_named_fields(self) -> None:
        class _FakeResponse:
            def __init__(self, *, status_code: int = 200, text: str = "", json_data: dict | None = None) -> None:
                self.status_code = status_code
                self.text = text
                self._json_data = json_data or {}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise requests.HTTPError(f"status={self.status_code}")

            def json(self) -> dict:
                return self._json_data

        class _FakeSession:
            def __init__(self) -> None:
                self.cookies = requests.cookies.RequestsCookieJar()
                self.cookies.set("_docu_seal_session", "session123")

            def get(self, url: str, **kwargs) -> _FakeResponse:
                if url.endswith("/sign_in"):
                    return _FakeResponse(
                        text='<form><input type="hidden" name="authenticity_token" value="sign-in-token" /></form>'
                    )
                if url.endswith("/templates/2/edit"):
                    return _FakeResponse(text='<meta name="csrf-token" content="edit-token" />')
                raise AssertionError(f"unexpected GET {url}")

            def post(self, url: str, **kwargs) -> _FakeResponse:
                if url.endswith("/sign_in"):
                    return _FakeResponse(status_code=302)
                if url.endswith("/clone_and_replace"):
                    return _FakeResponse(json_data={"id": "83"})
                raise AssertionError(f"unexpected POST {url}")

        client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            web_base_url="http://docuseal_proxy:8080",
            web_email="nick.craig@cwa3106.com",
            web_password="secret",
        )
        expected_fields = [
            {
                "name": "signer1_date",
                "type": "date",
                "submitter_uuid": "sub1",
                "areas": [{"x": 0.63, "y": 0.76, "w": 0.18, "h": 0.02, "page": 0, "attachment_uuid": "att"}],
            },
            {
                "name": "signer1_signature",
                "type": "signature",
                "submitter_uuid": "sub1",
                "areas": [{"x": 0.17, "y": 0.75, "w": 0.23, "h": 0.04, "page": 0, "attachment_uuid": "att"}],
            },
        ]
        invalid_template = {
            "fields": [
                {
                    "name": "",
                    "type": "signature",
                    "submitter_uuid": "sub1",
                    "areas": [{"x": 0.10, "y": 0.43, "w": 0.34, "h": 0.05, "page": 0, "attachment_uuid": "att"}],
                }
            ]
        }
        valid_template = {"fields": expected_fields}

        with patch("grievance_api.services.docuseal_client.requests.Session", return_value=_FakeSession()):
            with patch.object(client, "_extract_placeholder_areas", return_value={(1, "signature"): [{"page": 0}]}):
                with patch.object(client, "_apply_placeholder_field_alignment", side_effect=[expected_fields, expected_fields]) as mock_align:
                    with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                        mock_get.side_effect = [
                            SimpleNamespace(status_code=200, json=lambda: invalid_template),
                            SimpleNamespace(status_code=200, json=lambda: valid_template),
                        ]
                        template_id = client._clone_and_replace_template(
                            base_template_id="2",
                            upload_pdf_bytes=b"%PDF",
                            alignment_pdf_bytes=b"%PDF",
                            title="statement",
                            form_key="statement_of_occurrence",
                        )

        self.assertEqual(template_id, "83")
        self.assertEqual(mock_align.call_count, 2)

    def test_template_alignment_validation_tolerates_docuseal_float_rounding(self) -> None:
        expected_fields = [
            {
                "name": "signer1_date",
                "type": "date",
                "submitter_uuid": "sub1",
                "areas": [
                    {"x": 0.632190, "y": 0.247680, "w": 0.183007, "h": 0.015619, "page": 1, "attachment_uuid": "att"},
                    {"x": 0.632190, "y": 0.348437, "w": 0.183007, "h": 0.015619, "page": 1, "attachment_uuid": "att"},
                    {"x": 0.632190, "y": 0.382781, "w": 0.183007, "h": 0.015619, "page": 1, "attachment_uuid": "att"},
                ],
            }
        ]
        actual_template = {
            "fields": [
                {
                    "name": "signer1_date",
                    "type": "date",
                    "submitter_uuid": "sub1",
                    "areas": [
                        {"x": 0.632190, "y": 0.247680, "w": 0.183007, "h": 0.015619, "page": 1, "attachment_uuid": "att"},
                        {"x": 0.632190, "y": 0.348438, "w": 0.183007, "h": 0.015619, "page": 1, "attachment_uuid": "att"},
                        {"x": 0.632190, "y": 0.382781, "w": 0.183007, "h": 0.015619, "page": 1, "attachment_uuid": "att"},
                    ],
                }
            ]
        }

        with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
            mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: actual_template)
            self.client._validate_template_field_alignment(
                template_id="197",
                expected_fields=expected_fields,
            )

    def test_clone_and_replace_raises_when_verified_template_stays_misaligned(self) -> None:
        class _FakeResponse:
            def __init__(self, *, status_code: int = 200, text: str = "", json_data: dict | None = None) -> None:
                self.status_code = status_code
                self.text = text
                self._json_data = json_data or {}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise requests.HTTPError(f"status={self.status_code}")

            def json(self) -> dict:
                return self._json_data

        class _FakeSession:
            def __init__(self) -> None:
                self.cookies = requests.cookies.RequestsCookieJar()
                self.cookies.set("_docu_seal_session", "session123")

            def get(self, url: str, **kwargs) -> _FakeResponse:
                if url.endswith("/sign_in"):
                    return _FakeResponse(
                        text='<form><input type="hidden" name="authenticity_token" value="sign-in-token" /></form>'
                    )
                if url.endswith("/templates/2/edit"):
                    return _FakeResponse(text='<meta name="csrf-token" content="edit-token" />')
                raise AssertionError(f"unexpected GET {url}")

            def post(self, url: str, **kwargs) -> _FakeResponse:
                if url.endswith("/sign_in"):
                    return _FakeResponse(status_code=302)
                if url.endswith("/clone_and_replace"):
                    return _FakeResponse(json_data={"id": "83"})
                raise AssertionError(f"unexpected POST {url}")

        client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            web_base_url="http://docuseal_proxy:8080",
            web_email="nick.craig@cwa3106.com",
            web_password="secret",
        )
        expected_fields = [
            {
                "name": "signer1_date",
                "type": "date",
                "submitter_uuid": "sub1",
                "areas": [{"x": 0.63, "y": 0.76, "w": 0.18, "h": 0.02, "page": 0, "attachment_uuid": "att"}],
            },
            {
                "name": "signer1_signature",
                "type": "signature",
                "submitter_uuid": "sub1",
                "areas": [{"x": 0.17, "y": 0.75, "w": 0.23, "h": 0.04, "page": 0, "attachment_uuid": "att"}],
            },
        ]
        invalid_template = {
            "fields": [
                {
                    "name": "",
                    "type": "signature",
                    "submitter_uuid": "sub1",
                    "areas": [{"x": 0.10, "y": 0.43, "w": 0.34, "h": 0.05, "page": 0, "attachment_uuid": "att"}],
                }
            ]
        }

        with patch("grievance_api.services.docuseal_client.requests.Session", return_value=_FakeSession()):
            with patch.object(client, "_extract_placeholder_areas", return_value={(1, "signature"): [{"page": 0}]}):
                with patch.object(client, "_apply_placeholder_field_alignment", side_effect=[expected_fields, expected_fields]) as mock_align:
                    with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                        mock_get.side_effect = [
                            SimpleNamespace(status_code=200, json=lambda: invalid_template),
                            SimpleNamespace(status_code=200, json=lambda: invalid_template),
                        ]
                        with self.assertRaisesRegex(RuntimeError, "alignment verification failed"):
                            client._clone_and_replace_template(
                                base_template_id="2",
                                upload_pdf_bytes=b"%PDF",
                                alignment_pdf_bytes=b"%PDF",
                                title="statement",
                                form_key="statement_of_occurrence",
                            )

        self.assertEqual(mock_align.call_count, 2)

    def test_table_trace_overrides_align_rows_and_columns(self) -> None:
        areas = {
            (1, "signature"): [
                {"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}
            ],
            (1, "date"): [
                {"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}
            ],
            (2, "signature"): [
                {"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}
            ],
            (2, "date"): [
                {"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}
            ],
            (3, "signature"): [
                {"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}
            ],
            (3, "date"): [
                {"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}
            ],
        }
        segments = {
            0: {
                "page_w": 612.0,
                "page_h": 792.0,
                "vertical": [
                    {"x": 240.0, "y_min": 520.0, "y_max": 700.0},
                    {"x": 420.0, "y_min": 520.0, "y_max": 700.0},
                    {"x": 530.0, "y_min": 520.0, "y_max": 700.0},
                ],
                "horizontal": [
                    {"y": 520.0, "x_min": 240.0, "x_max": 530.0},
                    {"y": 580.0, "x_min": 240.0, "x_max": 530.0},
                    {"y": 640.0, "x_min": 240.0, "x_max": 530.0},
                    {"y": 700.0, "x_min": 240.0, "x_max": 530.0},
                ],
            }
        }

        with patch.object(self.client, "_extract_pdf_table_segments", return_value=segments):
            overrides, reason = self.client._build_table_trace_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertIn(reason, {"trace_settlement_rows_success", "trace_success"})
        self.assertEqual(len(overrides), 6)
        sig1 = overrides[(1, "signature")][0]
        sig2 = overrides[(2, "signature")][0]
        sig3 = overrides[(3, "signature")][0]
        date1 = overrides[(1, "date")][0]
        self.assertLess(sig1["x_min"], sig1["x_max"])
        self.assertLess(date1["x_min"], date1["x_max"])
        self.assertLess(sig1["x_max"], date1["x_min"])
        self.assertLess(sig2["y_min"], sig3["y_min"])
        self.assertLess(sig3["y_min"], sig1["y_min"])

    def test_resolve_overrides_uses_map_fallback_when_trace_fails(self) -> None:
        mapped_client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            signature_table_maps={
                "settlement_form_3106": {
                    "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                    "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                    "signer2_signature": {"page": 0, "x": 0.40, "y": 0.74, "w": 0.30, "h": 0.05},
                    "signer2_date": {"page": 0, "x": 0.72, "y": 0.74, "w": 0.20, "h": 0.05},
                    "signer3_signature": {"page": 0, "x": 0.40, "y": 0.81, "w": 0.30, "h": 0.05},
                    "signer3_date": {"page": 0, "x": 0.72, "y": 0.81, "w": 0.20, "h": 0.05},
                }
            },
        )
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        with patch.object(mapped_client, "_build_table_trace_overrides", return_value=({}, "trace_no_page_segments")):
            overrides, strategy, reason, metrics = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(strategy, "map_fallback")
        self.assertEqual(reason, "map_success")
        self.assertEqual(len(overrides), 6)
        self.assertIn("map_reason", metrics)
        sig1 = overrides[(1, "signature")][0]
        self.assertAlmostEqual(sig1["x_min"], 0.40 * 612.0, places=3)

    def test_resolve_overrides_uses_generic_when_trace_and_map_unavailable(self) -> None:
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        with patch.object(self.client, "_build_table_trace_overrides", return_value=({}, "trace_no_page_segments")):
            overrides, strategy, reason, metrics = self.client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(overrides, {})
        self.assertEqual(strategy, "generic_fallback")
        self.assertIn("trace_reason=trace_no_page_segments", reason)
        self.assertIn("trace_rejected", metrics)

    def test_normalize_area_att_mobility_signature_uses_tighter_form_hint(self) -> None:
        raw = {
            "x_min": 238.35,
            "y_min": 444.5475,
            "x_max": 379.5015,
            "y_max": 456.2655,
            "page": 0,
            "page_w": 612.0,
            "page_h": 792.0,
        }
        generic_area = self.client._normalize_area(
            raw=raw,
            field_type="signature",
            attachment_uuid="att",
            field_name="signer1_signature",
            form_key="generic_form",
        )
        tuned_area = self.client._normalize_area(
            raw=raw,
            field_type="signature",
            attachment_uuid="att",
            field_name="signer1_signature",
            form_key="att_mobility_bargaining_suggestion",
        )
        self.assertLess(tuned_area["h"], generic_area["h"])
        self.assertLess(tuned_area["y"], generic_area["y"])

    def test_guard_fails_overlap_and_uses_map_fallback(self) -> None:
        mapped_client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            signature_table_maps={
                "settlement_form_3106": {
                    "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                    "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                    "signer2_signature": {"page": 0, "x": 0.40, "y": 0.74, "w": 0.30, "h": 0.05},
                    "signer2_date": {"page": 0, "x": 0.72, "y": 0.74, "w": 0.20, "h": 0.05},
                    "signer3_signature": {"page": 0, "x": 0.40, "y": 0.81, "w": 0.30, "h": 0.05},
                    "signer3_date": {"page": 0, "x": 0.72, "y": 0.81, "w": 0.20, "h": 0.05},
                }
            },
        )
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        overlapping_trace = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 540.0, "x_max": 430.0, "y_max": 576.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 420.0, "y_min": 540.0, "x_max": 530.0, "y_max": 576.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 590.0, "x_max": 430.0, "y_max": 626.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 420.0, "y_min": 590.0, "x_max": 530.0, "y_max": 626.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 640.0, "x_max": 430.0, "y_max": 676.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 420.0, "y_min": 640.0, "x_max": 530.0, "y_max": 676.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        with patch.object(mapped_client, "_build_table_trace_overrides", return_value=(overlapping_trace, "trace_success")):
            overrides, strategy, reason, metrics = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(strategy, "map_fallback")
        self.assertIn("map_success", reason)
        self.assertEqual(len(overrides), 6)
        self.assertIn("guard_fail_overlap", str(metrics))

    def test_guard_fails_min_gap_and_uses_map_fallback(self) -> None:
        mapped_client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            signature_table_guard_min_gap=0.005,
            signature_table_maps={
                "settlement_form_3106": {
                    "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                    "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                    "signer2_signature": {"page": 0, "x": 0.40, "y": 0.74, "w": 0.30, "h": 0.05},
                    "signer2_date": {"page": 0, "x": 0.72, "y": 0.74, "w": 0.20, "h": 0.05},
                    "signer3_signature": {"page": 0, "x": 0.40, "y": 0.81, "w": 0.30, "h": 0.05},
                    "signer3_date": {"page": 0, "x": 0.72, "y": 0.81, "w": 0.20, "h": 0.05},
                }
            },
        )
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        tight_gap_trace = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 540.0, "x_max": 430.0, "y_max": 576.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 431.0, "y_min": 540.0, "x_max": 530.0, "y_max": 576.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 590.0, "x_max": 430.0, "y_max": 626.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 431.0, "y_min": 590.0, "x_max": 530.0, "y_max": 626.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 640.0, "x_max": 430.0, "y_max": 676.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 431.0, "y_min": 640.0, "x_max": 530.0, "y_max": 676.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        with patch.object(mapped_client, "_build_table_trace_overrides", return_value=(tight_gap_trace, "trace_success")):
            overrides, strategy, reason, metrics = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(strategy, "map_fallback")
        self.assertEqual(reason, "map_success")
        self.assertIn("guard_fail_min_gap", str(metrics))
        self.assertEqual(len(overrides), 6)

    def test_guard_fails_map_delta_and_uses_map_fallback(self) -> None:
        mapped_client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            signature_table_guard_tolerance=0.015,
            signature_table_maps={
                "statement_of_occurrence": {
                    "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                    "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                    "signer2_signature": {"page": 0, "x": 0.40, "y": 0.74, "w": 0.30, "h": 0.05},
                    "signer2_date": {"page": 0, "x": 0.72, "y": 0.74, "w": 0.20, "h": 0.05},
                    "signer3_signature": {"page": 0, "x": 0.40, "y": 0.81, "w": 0.30, "h": 0.05},
                    "signer3_date": {"page": 0, "x": 0.72, "y": 0.81, "w": 0.20, "h": 0.05},
                }
            },
        )
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        off_map_trace = {
            (1, "signature"): [{"x_min": 220.0, "y_min": 530.0, "x_max": 380.0, "y_max": 568.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 420.0, "y_min": 530.0, "x_max": 520.0, "y_max": 568.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 220.0, "y_min": 580.0, "x_max": 380.0, "y_max": 618.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 420.0, "y_min": 580.0, "x_max": 520.0, "y_max": 618.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 220.0, "y_min": 630.0, "x_max": 380.0, "y_max": 668.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 420.0, "y_min": 630.0, "x_max": 520.0, "y_max": 668.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        with patch.object(mapped_client, "_build_table_trace_overrides", return_value=(off_map_trace, "trace_success")):
            overrides, strategy, reason, metrics = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="statement_of_occurrence",
            )

        self.assertEqual(strategy, "map_fallback")
        self.assertEqual(reason, "map_success")
        self.assertIn("guard_fail_map_delta", str(metrics))
        self.assertEqual(len(overrides), 6)

    def test_guard_pass_keeps_trace_strategy(self) -> None:
        mapped_client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            signature_table_guard_tolerance=0.07,
            signature_table_maps={
                "settlement_form_3106": {
                    "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                    "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                    "signer2_signature": {"page": 0, "x": 0.40, "y": 0.74, "w": 0.30, "h": 0.05},
                    "signer2_date": {"page": 0, "x": 0.72, "y": 0.74, "w": 0.20, "h": 0.05},
                    "signer3_signature": {"page": 0, "x": 0.40, "y": 0.81, "w": 0.30, "h": 0.05},
                    "signer3_date": {"page": 0, "x": 0.72, "y": 0.81, "w": 0.20, "h": 0.05},
                }
            },
        )
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        good_trace = {
            (1, "signature"): [{"x_min": 247.0, "y_min": 532.0, "x_max": 428.0, "y_max": 571.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 443.0, "y_min": 532.0, "x_max": 529.0, "y_max": 571.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 247.0, "y_min": 584.0, "x_max": 428.0, "y_max": 623.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 443.0, "y_min": 584.0, "x_max": 529.0, "y_max": 623.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 247.0, "y_min": 636.0, "x_max": 428.0, "y_max": 675.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 443.0, "y_min": 636.0, "x_max": 529.0, "y_max": 675.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        with patch.object(mapped_client, "_build_table_trace_overrides", return_value=(good_trace, "trace_success")):
            overrides, strategy, reason, metrics = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(strategy, "trace")
        self.assertIn("guard_pass", reason)
        self.assertEqual(len(overrides), 6)
        self.assertIn("guard_metrics", metrics)

    def test_settlement_build_signer_rows_preserves_sparse_signer_indexes(self) -> None:
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        rows = self.client._build_signer_rows(
            placeholder_areas=areas,
            form_key="settlement_form_3106",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 2)

    def test_settlement_guard_ignores_vertical_map_drift(self) -> None:
        mapped_client = DocuSealClient(
            "http://docuseal:3000",
            "token",
            signature_table_guard_tolerance=0.015,
            signature_table_maps={
                "settlement_form_3106": {
                    "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                    "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                    "signer2_signature": {"page": 0, "x": 0.40, "y": 0.74, "w": 0.30, "h": 0.05},
                    "signer2_date": {"page": 0, "x": 0.72, "y": 0.74, "w": 0.20, "h": 0.05},
                    "signer3_signature": {"page": 0, "x": 0.40, "y": 0.81, "w": 0.30, "h": 0.05},
                    "signer3_date": {"page": 0, "x": 0.72, "y": 0.81, "w": 0.20, "h": 0.05},
                }
            },
        )
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 250.0, "y_min": 598.0, "x_max": 350.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 435.0, "y_min": 598.0, "x_max": 510.0, "y_max": 610.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 250.0, "y_min": 650.0, "x_max": 350.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 435.0, "y_min": 650.0, "x_max": 510.0, "y_max": 662.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        # Same column widths/positions as map but shifted down by row growth.
        y_drift_trace = {
            (1, "signature"): [{"x_min": 244.8, "y_min": 560.0, "x_max": 428.4, "y_max": 599.6, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 440.6, "y_min": 560.0, "x_max": 563.0, "y_max": 599.6, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "signature"): [{"x_min": 244.8, "y_min": 615.0, "x_max": 428.4, "y_max": 654.6, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (2, "date"): [{"x_min": 440.6, "y_min": 615.0, "x_max": 563.0, "y_max": 654.6, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "signature"): [{"x_min": 244.8, "y_min": 670.0, "x_max": 428.4, "y_max": 709.6, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (3, "date"): [{"x_min": 440.6, "y_min": 670.0, "x_max": 563.0, "y_max": 709.6, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }

        with patch.object(mapped_client, "_build_table_trace_overrides", return_value=(y_drift_trace, "trace_success")):
            overrides, strategy, reason, metrics = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(strategy, "trace")
        self.assertIn("guard_pass", reason)
        self.assertEqual(len(overrides), 6)
        self.assertIn("guard_metrics", metrics)

    def test_extract_signing_links_by_email_prefers_submitter_urls(self) -> None:
        links = self.client.extract_signing_links_by_email(
            {
                "submitters": [
                    {"email": "manager@example.org", "url": "https://docuseal.local/s/manager"},
                    {"email": "steward@example.org", "signing_url": "https://docuseal.local/s/steward"},
                    {"email": "grievant@example.org", "slug": "grievantSlug"},
                ]
            }
        )
        self.assertEqual(links["manager@example.org"], "https://docuseal.local/s/manager")
        self.assertEqual(links["steward@example.org"], "https://docuseal.local/s/steward")
        self.assertIn("grievant@example.org", links)
        self.assertTrue(links["grievant@example.org"].endswith("/s/grievantSlug"))

    def test_extract_signing_links_by_email_handles_signers_list(self) -> None:
        links = self.client.extract_signing_links_by_email(
            {
                "signers": [
                    {"email": "one@example.org", "link": "https://docuseal.local/s/one"},
                    {"email": "two@example.org", "slug": "twoSlug"},
                ]
            }
        )
        self.assertEqual(links["one@example.org"], "https://docuseal.local/s/one")
        self.assertIn("two@example.org", links)
        self.assertTrue(links["two@example.org"].endswith("/s/twoSlug"))

    def test_alignment_uses_exact_box_for_table_signature_overrides(self) -> None:
        placeholder_areas = {
            (1, "signature"): [
                {
                    "x_min": 120.0,
                    "y_min": 440.0,
                    "x_max": 260.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "date"): [
                {
                    "x_min": 390.0,
                    "y_min": 440.0,
                    "x_max": 500.0,
                    "y_max": 452.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
        }
        table_overrides = {
            (1, "signature"): [
                {
                    "x_min": 210.0,
                    "y_min": 560.0,
                    "x_max": 420.0,
                    "y_max": 580.0,
                    "page": 0,
                    "page_w": 612.0,
                    "page_h": 792.0,
                }
            ],
            (1, "date"): [
                {
                    "x_min": 440.0,
                    "y_min": 560.0,
                    "x_max": 530.0,
                    "y_max": 580.0,
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

        with patch.object(self.client, "_extract_placeholder_areas", return_value=placeholder_areas):
            with patch.object(
                self.client,
                "_resolve_signature_table_overrides",
                return_value=(table_overrides, "trace", "trace_settlement_rows_success;guard_pass", {}),
            ):
                with patch("grievance_api.services.docuseal_client.requests.get") as mock_get:
                    with patch("grievance_api.services.docuseal_client.requests.patch") as mock_patch:
                        mock_get.return_value = SimpleNamespace(status_code=200, json=lambda: template_obj)
                        self.client._apply_placeholder_field_alignment(
                            template_id="123",
                            pdf_bytes=b"fake",
                            form_key="settlement_form_3106",
                        )

        fields = mock_patch.call_args.kwargs["json"]["fields"]
        by_name = {str(field.get("name") or ""): field for field in fields}
        sig_area = by_name["signer1_signature"]["areas"][0]
        date_area = by_name["signer1_date"]["areas"][0]
        self.assertAlmostEqual(sig_area["x"], 210.0 / 612.0, places=6)
        self.assertAlmostEqual(sig_area["y"], 560.0 / 792.0, places=6)
        self.assertAlmostEqual(sig_area["w"], 210.0 / 612.0, places=6)
        self.assertAlmostEqual(sig_area["h"], 20.0 / 792.0, places=6)
        self.assertAlmostEqual(date_area["y"], 560.0 / 792.0, places=6)


if __name__ == "__main__":
    unittest.main()
