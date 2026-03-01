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

        self.assertEqual(reason, "trace_success")
        self.assertEqual(len(overrides), 6)
        sig1 = overrides[(1, "signature")][0]
        sig2 = overrides[(2, "signature")][0]
        sig3 = overrides[(3, "signature")][0]
        date1 = overrides[(1, "date")][0]
        self.assertLess(sig1["x_min"], sig1["x_max"])
        self.assertLess(date1["x_min"], date1["x_max"])
        self.assertLess(sig1["x_max"], date1["x_min"])
        self.assertLess(sig1["y_min"], sig2["y_min"])
        self.assertLess(sig2["y_min"], sig3["y_min"])

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
            overrides, strategy, reason = mapped_client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(strategy, "map_fallback")
        self.assertEqual(reason, "map_success")
        self.assertEqual(len(overrides), 6)
        sig1 = overrides[(1, "signature")][0]
        self.assertAlmostEqual(sig1["x_min"], 0.40 * 612.0, places=3)

    def test_resolve_overrides_uses_generic_when_trace_and_map_unavailable(self) -> None:
        areas = {
            (1, "signature"): [{"x_min": 250.0, "y_min": 546.0, "x_max": 350.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
            (1, "date"): [{"x_min": 435.0, "y_min": 546.0, "x_max": 510.0, "y_max": 558.0, "page": 0, "page_w": 612.0, "page_h": 792.0}],
        }
        with patch.object(self.client, "_build_table_trace_overrides", return_value=({}, "trace_no_page_segments")):
            overrides, strategy, reason = self.client._resolve_signature_table_overrides(
                placeholder_areas=areas,
                pdf_bytes=b"fake",
                form_key="settlement_form_3106",
            )

        self.assertEqual(overrides, {})
        self.assertEqual(strategy, "generic_fallback")
        self.assertIn("trace_reason=trace_no_page_segments", reason)


if __name__ == "__main__":
    unittest.main()
