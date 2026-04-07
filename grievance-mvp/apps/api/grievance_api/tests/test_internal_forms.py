from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from grievance_api.web.routes_internal_forms import (
    NonDisciplineInternalFormSubmission,
    _build_non_discipline_intake_payload,
    non_discipline_internal_form_page,
    submit_non_discipline_internal_form,
)


class _Request:
    def __init__(self, *, state, host: str = "127.0.0.1") -> None:  # noqa: ANN001
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
        self.assertEqual(payload["contract"], "CWA")
        self.assertEqual(payload["grievant_firstname"], "Taylor")
        self.assertEqual(payload["grievant_lastname"], "Jones")
        self.assertEqual(payload["grievant_email"], "taylor@example.org")
        self.assertEqual(payload["narrative"], "Non-discipline grievance brief")

        template_data = payload["template_data"]
        self.assertIsInstance(template_data, dict)
        self.assertEqual(template_data["grievant_name"], "Taylor Jones")
        self.assertEqual(template_data["local_number"], "3106")
        self.assertEqual(template_data["issue_or_condition_involved"], "Management denied agreed scheduling rights.")
        self.assertEqual(template_data["recommendation"], "Advance the grievance and seek full make-whole relief.")
        self.assertEqual(template_data["attachment_1"], "Exhibit A - Timeline")
        self.assertEqual(template_data["attachment_10"], "")
        self.assertEqual(template_data["signer_email"], "")

    async def test_page_renders_microsoft_forms_style_internal_submission_form(self) -> None:
        response = await non_discipline_internal_form_page(
            _Request(state=SimpleNamespace(cfg=self._cfg())),
        )
        html = response.body.decode("utf-8")

        self.assertIn("Non-Discipline Grievance Brief", html)
        self.assertIn("FORM_ENDPOINT = '/internal/forms/non-discipline-brief/submissions'", html)
        self.assertIn("document command", html.lower())
        self.assertIn("non_discipline_brief", html)
        self.assertIn("issue_or_condition_involved", html)
        self.assertIn("attachment_10", html)

    async def test_submit_posts_intake_payload_with_internal_auth_headers(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg()))
        response_payload = {
            "case_id": "C1",
            "grievance_id": "2026001",
            "status": "awaiting_signatures",
            "documents": [],
        }

        with patch("grievance_api.web.routes_internal_forms.requests.post") as mock_post:
            mock_post.return_value = SimpleNamespace(status_code=200, text=json.dumps(response_payload))
            result = await submit_non_discipline_internal_form(_submission(), request)

        called_url = mock_post.call_args.args[0]
        called_body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
        called_headers = mock_post.call_args.kwargs["headers"]

        self.assertEqual(called_url, "http://127.0.0.1:8080/intake")
        self.assertEqual(called_headers["X-Intake-Key"], "shared-secret")
        self.assertEqual(called_body["request_id"], "forms-internal-test-1")
        self.assertEqual(called_body["document_command"], "non_discipline_brief")
        self.assertEqual(called_body["template_data"]["grievant_name"], "Taylor Jones")
        self.assertEqual(result["request_id"], "forms-internal-test-1")
        self.assertEqual(result["document_command"], "non_discipline_brief")
        self.assertEqual(result["intake_response"], response_payload)


if __name__ == "__main__":
    unittest.main()
