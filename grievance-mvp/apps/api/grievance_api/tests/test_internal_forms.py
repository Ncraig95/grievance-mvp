from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from grievance_api.web.routes_internal_forms import (
    NonDisciplineInternalFormSubmission,
    _build_non_discipline_intake_payload,
    non_discipline_internal_form_page,
    statement_of_occurrence_internal_form_page,
    submit_non_discipline_internal_form,
    submit_statement_of_occurrence_internal_form,
)


class _Request:
    def __init__(self, *, state, host: str = "127.0.0.1") -> None:  # noqa: ANN001
        if not hasattr(state, "db"):
            state = SimpleNamespace(
                **state.__dict__,
                db=SimpleNamespace(hosted_form_settings_by_key=AsyncMock(return_value={})),
            )
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host=host)
        self.headers = {"host": host}
        self.session = {}


def _submission(**overrides: object) -> NonDisciplineInternalFormSubmission:
    values: dict[str, object] = {
        "request_id": "forms-internal-test-1",
        "grievant_firstname": "Taylor",
        "grievant_lastname": "Jones",
        "grievant_email": "taylor@example.org",
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
        "signer_email": "signer@example.org",
    }
    values.update(overrides)
    return NonDisciplineInternalFormSubmission.model_validate(values)


class InternalFormsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _cfg():
        return SimpleNamespace(
            officer_auth=SimpleNamespace(enabled=False),
            intake_auth=SimpleNamespace(
                shared_header_name="X-Intake-Key",
                shared_header_value="shared-secret",
                cloudflare_access_client_id="",
                cloudflare_access_client_secret="",
            ),
            hmac_shared_secret="REPLACE_WITH_LONG_RANDOM_SECRET",
        )

    def test_non_discipline_payload_matches_intake_mapping(self) -> None:
        payload = _build_non_discipline_intake_payload(_submission(signer_email=""))

        self.assertEqual(payload["request_id"], "forms-internal-test-1")
        self.assertEqual(payload["document_command"], "non_discipline_brief")
        self.assertEqual(payload["grievance_id"], "Local-26-001")
        self.assertEqual(payload["grievance_number"], "Local-26-001")
        self.assertEqual(payload["contract"], "CWA")
        self.assertEqual(payload["grievant_firstname"], "Taylor")
        self.assertEqual(payload["grievant_lastname"], "Jones")
        self.assertNotIn("grievant_email", payload)
        self.assertEqual(payload["narrative"], "Non-discipline grievance brief")

        template_data = payload["template_data"]
        self.assertIsInstance(template_data, dict)
        self.assertEqual(template_data["grievant_name"], "Taylor Jones")
        self.assertEqual(template_data["local_number"], "3106")
        self.assertEqual(template_data["local_grievance_number"], "Local-26-001")
        self.assertEqual(template_data["issue_or_condition_involved"], "Management denied agreed scheduling rights.")
        self.assertEqual(template_data["recommendation"], "Advance the grievance and seek full make-whole relief.")
        self.assertEqual(template_data["attachment_1"], "Exhibit A - Timeline")
        self.assertEqual(template_data["attachment_10"], "")
        self.assertNotIn("signer_email", template_data)

    def test_non_discipline_payload_accepts_grievance_number_alias(self) -> None:
        payload = _build_non_discipline_intake_payload(
            _submission(grievance_number="2026001", local_grievance_number="")
        )

        self.assertEqual(payload["grievance_id"], "2026001")
        self.assertEqual(payload["grievance_number"], "2026001")
        self.assertEqual(payload["template_data"]["local_grievance_number"], "2026001")

    async def test_page_renders_microsoft_forms_style_internal_submission_form(self) -> None:
        response = await non_discipline_internal_form_page(
            _Request(state=SimpleNamespace(cfg=self._cfg())),
        )
        html = response.body.decode("utf-8")

        self.assertIn("Non-Discipline Grievance Brief", html)
        self.assertIn("/internal/forms/non-discipline-brief/submissions", html)
        self.assertIn("document command", html.lower())
        self.assertIn("non_discipline_brief", html)
        self.assertIn("issue_or_condition_involved", html)
        self.assertIn("attachment_10", html)

    async def test_statement_page_renders_internal_attested_redirect_form(self) -> None:
        response = await statement_of_occurrence_internal_form_page(
            _Request(state=SimpleNamespace(cfg=self._cfg())),
        )
        html = response.body.decode("utf-8")

        self.assertIn("Statement of Occurrence", html)
        self.assertIn("/internal/forms/statement-of-occurrence/submissions", html)
        self.assertIn("signatureConsent", html)
        self.assertIn("Submit and open signature", html)
        self.assertIn("waitOverlay", html)
        self.assertIn("Preparing signature page", html)
        self.assertIn("Opening the signature page now", html)

    async def test_submit_posts_intake_payload_with_internal_auth_headers(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg()))
        response_payload = {
            "case_id": "C1",
            "grievance_id": "2026001",
            "status": "awaiting_signatures",
            "documents": [],
        }

        with patch("grievance_api.web.routes_internal_forms.submit_hosted_form") as mock_submit:
            mock_submit.return_value = {
                "request_id": "forms-internal-test-1",
                "form_key": "non_discipline_brief",
                "route_type": "intake",
                "backend_response": response_payload,
            }
            result = await submit_non_discipline_internal_form(_submission(), request)

        called_form_key = mock_submit.call_args.args[0]
        called_body = mock_submit.call_args.args[1]
        self.assertEqual(called_form_key, "non_discipline_brief")
        self.assertEqual(called_body["request_id"], "forms-internal-test-1")
        self.assertEqual(called_body["grievant_firstname"], "Taylor")
        self.assertEqual(called_body["recommendation"], "Advance the grievance and seek full make-whole relief.")
        self.assertEqual(result["request_id"], "forms-internal-test-1")
        self.assertEqual(result["document_command"], "non_discipline_brief")
        self.assertEqual(result["intake_response"], response_payload)

    async def test_statement_submit_returns_signing_redirect_metadata(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg()))
        body = {
            "request_id": "statement-1",
            "contract": "Wire Tech",
            "grievant_firstname": "Taylor",
            "grievant_lastname": "Jones",
            "grievant_email": "taylor@example.org",
            "_signature_attestation": {"accepted": True},
        }
        response_payload = {
            "case_id": "C1",
            "grievance_id": "2026001",
            "status": "awaiting_signatures",
            "documents": [
                {
                    "document_id": "D1",
                    "doc_type": "statement_of_occurrence",
                    "status": "sent_for_signature",
                    "signing_link": "https://sign.example/D1",
                }
            ],
        }

        with patch("grievance_api.web.routes_internal_forms.submit_hosted_form") as mock_submit:
            mock_submit.return_value = {
                "request_id": "statement-1",
                "form_key": "statement_of_occurrence",
                "route_type": "intake",
                "backend_response": response_payload,
                "signing_redirect_url": "https://sign.example/D1",
                "signing_redirect_reason": "ready",
                "document_status": "sent_for_signature",
            }
            result = await submit_statement_of_occurrence_internal_form(body, request)

        self.assertEqual(mock_submit.call_args.args[0], "statement_of_occurrence")
        self.assertEqual(result["document_command"], "statement_of_occurrence")
        self.assertEqual(result["signing_redirect_url"], "https://sign.example/D1")
        self.assertEqual(result["document_status"], "sent_for_signature")


if __name__ == "__main__":
    unittest.main()
