from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.hosted_forms_registry import get_hosted_form_definition, hosted_form_keys
from grievance_api.web.models import HostedFormSettingsUpdateRequest
from grievance_api.web.routes_hosted_forms import (
    _PUBLIC_RATE_LIMIT_BUCKETS,
    hosted_forms_admin_page,
    hosted_form_page,
    hosted_forms_admin_settings,
    submit_hosted_form,
    update_hosted_form_setting,
)


class _Request:
    def __init__(
        self,
        *,
        state,
        host: str = "127.0.0.1",
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        merged_headers = {"host": host}
        if headers:
            merged_headers.update(headers)
        self.headers = merged_headers
        self.client = SimpleNamespace(host=host)
        self.session = {}
        self._json_body = json_body or {}

    async def json(self) -> dict[str, object]:
        return dict(self._json_body)


def _sample_value(field) -> str:  # noqa: ANN001
    if field.type == "email":
        return f"{field.name}@example.org"
    if field.type == "date":
        return "2026-04-09"
    if field.type == "textarea":
        return f"{field.label} details"
    if field.type == "select":
        return field.options[0] if field.options else ""
    return field.placeholder or field.label or field.name


def _values_for_definition(form_key: str, **overrides: str) -> dict[str, str]:
    definition = get_hosted_form_definition(form_key)
    if not definition:
        raise AssertionError(f"missing definition for {form_key}")
    values: dict[str, str] = {}
    for field in definition.fields:
        values[field.name] = "" if not field.required else _sample_value(field)
    values.update(overrides)
    return values


class HostedFormsTests(unittest.IsolatedAsyncioTestCase):
    _EXPECTED_FORM_KEYS = {
        "statement_of_occurrence",
        "bellsouth_meeting_request",
        "mobility_meeting_request",
        "grievance_data_request",
        "data_request_letterhead",
        "true_intent_brief",
        "non_discipline_brief",
        "disciplinary_brief",
        "settlement_form",
        "mobility_record_of_grievance",
        "bst_grievance_form_3g3a",
        "att_mobility_bargaining_suggestion",
    }

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)
        _PUBLIC_RATE_LIMIT_BUCKETS.clear()

    async def _cleanup_tmpdir(self) -> None:
        _PUBLIC_RATE_LIMIT_BUCKETS.clear()
        self.tmpdir.cleanup()

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

    def _state(self):
        return SimpleNamespace(
            cfg=self._cfg(),
            db=self.db,
            logger=logging.getLogger("test"),
        )

    def test_registry_covers_repo_form_catalog(self) -> None:
        self.assertEqual(set(hosted_form_keys()), self._EXPECTED_FORM_KEYS)

    def test_builds_simple_intake_payload(self) -> None:
        definition = get_hosted_form_definition("statement_of_occurrence")
        assert definition is not None
        payload = definition.build_payload(
            _values_for_definition(
                "statement_of_occurrence",
                contract="Wire Tech",
                grievant_firstname="Taylor",
                grievant_lastname="Jones",
                article="Article 1",
            )
        )

        self.assertEqual(payload["document_command"], "statement_of_occurrence")
        self.assertEqual(payload["contract"], "Wire Tech")
        self.assertEqual(payload["grievant_firstname"], "Taylor")
        self.assertEqual(payload["template_data"]["article"], "Article 1")
        self.assertEqual(payload["template_data"]["witness_1_name"], "")

    def test_statement_contract_field_uses_supported_contract_dropdown(self) -> None:
        definition = get_hosted_form_definition("statement_of_occurrence")
        assert definition is not None
        contract_field = next(field for field in definition.fields if field.source_key == "contract")

        self.assertEqual(contract_field.type, "select")
        self.assertIn("City of Jacksonville", contract_field.options)
        self.assertIn("Wire Tech", contract_field.options)
        self.assertIn("BellSouth", contract_field.options)
        self.assertIn("AT&T Mobility", contract_field.options)
        self.assertIn("IHX", contract_field.options)
        self.assertIn("BST", contract_field.options)
        self.assertIn("Utilities", contract_field.options)

    def test_true_intent_field_order_matches_form_sections(self) -> None:
        definition = get_hosted_form_definition("true_intent_brief")
        assert definition is not None
        names = [field.name for field in definition.fields]

        self.assertEqual(
            names[:10],
            [
                "grievant_firstname",
                "grievant_lastname",
                "grievant_email",
                "grievant_phone",
                "grievant_street",
                "grievant_city",
                "grievant_state",
                "grievant_zip",
                "title",
                "department",
            ],
        )
        self.assertEqual(
            names[-11:],
            [
                "attachment_1",
                "attachment_2",
                "attachment_3",
                "attachment_4",
                "attachment_5",
                "attachment_6",
                "attachment_7",
                "attachment_8",
                "attachment_9",
                "attachment_10",
                "signer_email",
            ],
        )

    def test_non_discipline_attachment_order_is_natural(self) -> None:
        definition = get_hosted_form_definition("non_discipline_brief")
        assert definition is not None
        names = [field.name for field in definition.fields]

        self.assertLess(names.index("attachment_2"), names.index("attachment_10"))
        self.assertEqual(
            names[-11:],
            [
                "attachment_1",
                "attachment_2",
                "attachment_3",
                "attachment_4",
                "attachment_5",
                "attachment_6",
                "attachment_7",
                "attachment_8",
                "attachment_9",
                "attachment_10",
                "signer_email",
            ],
        )

    def test_settlement_signer_questions_render_before_case_details(self) -> None:
        definition = get_hosted_form_definition("settlement_form")
        assert definition is not None
        names = [field.name for field in definition.fields]

        self.assertEqual(
            names[:6],
            [
                "grievance_id",
                "manager_signer_email",
                "steward_signer_email",
                "grievant_firstname",
                "grievant_lastname",
                "grievant_email",
            ],
        )

    def test_bargaining_suggestion_override_is_last(self) -> None:
        definition = get_hosted_form_definition("att_mobility_bargaining_suggestion")
        assert definition is not None
        names = [field.name for field in definition.fields]

        self.assertEqual(
            names,
            [
                "local_number",
                "demand_from_local",
                "submitting_member_title",
                "submitting_member_name",
                "demand_text",
                "reason_text",
                "specific_examples_text",
                "work_phone",
                "home_phone",
                "non_work_email",
                "local_president_signer_email",
            ],
        )

    def test_builds_intake_payload_with_explicit_signers(self) -> None:
        definition = get_hosted_form_definition("settlement_form")
        assert definition is not None
        payload = definition.build_payload(
            _values_for_definition(
                "settlement_form",
                grievance_id="2026001",
                grievant_firstname="Taylor",
                grievant_lastname="Jones",
                grievant_email="taylor@example.org",
                issue_text="Issue details",
                settlement_text="Settlement terms",
                manager_signer_email="manager@example.org",
                steward_signer_email="steward@example.org",
            )
        )

        self.assertEqual(payload["document_command"], "settlement_form")
        self.assertEqual(payload["grievance_id"], "2026001")
        self.assertEqual(payload["narrative"], "Issue details")
        self.assertEqual(payload["template_data"]["grievance_number"], "2026001")
        self.assertEqual(payload["documents"][0]["signers"], ["manager@example.org", "steward@example.org"])

    def test_builds_existing_grievance_intake_payload(self) -> None:
        definition = get_hosted_form_definition("bellsouth_meeting_request")
        assert definition is not None
        payload = definition.build_payload(
            _values_for_definition(
                "bellsouth_meeting_request",
                grievance_id="2026111",
                grievant_firstname="Avery",
                grievant_lastname="Smith",
                grievant_names="Avery Smith",
            )
        )

        self.assertEqual(payload["grievance_id"], "2026111")
        self.assertEqual(payload["contract"], "BellSouth")
        self.assertEqual(payload["document_command"], "bellsouth_meeting_request")
        self.assertEqual(payload["template_data"]["request_date"], date.today().isoformat())

    def test_grievance_data_request_payload_uses_existing_grievance_id(self) -> None:
        definition = get_hosted_form_definition("grievance_data_request")
        assert definition is not None
        payload = definition.build_payload(
            _values_for_definition(
                "grievance_data_request",
                grievance_id="2026111",
                contract="AT&T Mobility",
                grievant_firstname="Avery",
                grievant_lastname="Smith",
                grievant_email="avery@example.org",
                signer_email="chief@example.org",
            )
        )

        self.assertEqual(payload["grievance_id"], "2026111")
        self.assertEqual(payload["contract"], "AT&T Mobility")
        self.assertEqual(payload["document_command"], "grievance_data_request")
        self.assertEqual(payload["template_data"]["grievant_name"], "Avery Smith")
        self.assertEqual(payload["template_data"]["today_date"], date.today().isoformat())
        self.assertEqual(payload["template_data"]["signer_email"], "chief@example.org")

    def test_data_request_letterhead_derives_grievance_number_and_today_date(self) -> None:
        definition = get_hosted_form_definition("data_request_letterhead")
        assert definition is not None
        payload = definition.build_payload(
            _values_for_definition(
                "data_request_letterhead",
                grievance_id="2026444",
                contract="AT&T Mobility",
                grievant_firstname="Jordan",
                grievant_lastname="Lee",
                grievant_email="jordan@example.org",
                company_rep_name="Pat Supervisor",
                data_requested="Personnel file and discipline notes",
                preferred_format="PDF",
                steward_name="Chief Steward",
                steward_email="chief@example.org",
            )
        )

        self.assertEqual(payload["grievance_id"], "2026444")
        self.assertEqual(payload["document_command"], "data_request_letterhead")
        self.assertEqual(payload["template_data"]["grievance_number"], "2026444")
        self.assertEqual(payload["template_data"]["grievant_name"], "Jordan Lee")
        self.assertEqual(payload["template_data"]["today_date"], date.today().isoformat())

    def test_builds_standalone_payload(self) -> None:
        definition = get_hosted_form_definition("att_mobility_bargaining_suggestion")
        assert definition is not None
        payload = definition.build_payload(
            _values_for_definition(
                "att_mobility_bargaining_suggestion",
                local_number="3106",
                demand_from_local="Demand from Local 3106",
                submitting_member_name="Taylor Jones",
                demand_text="More staffing coverage",
                reason_text="Coverage gaps are recurring",
                local_president_signer_email="president@example.org",
            )
        )

        self.assertEqual(payload["form_key"], "att_mobility_bargaining_suggestion")
        self.assertEqual(payload["local_president_signer_email"], "president@example.org")
        self.assertEqual(payload["template_data"]["demand_text"], "More staffing coverage")
        self.assertEqual(payload["template_data"]["specific_examples_text"], "")

    async def test_public_page_renders_shared_form(self) -> None:
        response = await hosted_form_page("statement_of_occurrence", _Request(state=self._state()))
        html = response.body.decode("utf-8")

        self.assertIn("Statement of Occurrence", html)
        self.assertIn("/forms/statement_of_occurrence/submissions", html)
        self.assertIn("Hosted form key", html)

    async def test_private_page_redirects_to_officer_login_gate(self) -> None:
        await self.db.upsert_hosted_form_setting(
            form_key="statement_of_occurrence",
            visibility="private",
            enabled=True,
            updated_by="tester",
        )
        redirect = RedirectResponse("/auth/login", status_code=303)
        with patch("grievance_api.web.routes_hosted_forms.require_officer_page_access", new=AsyncMock(return_value=redirect)):
            response = await hosted_form_page("statement_of_occurrence", _Request(state=self._state(), host="198.51.100.9"))

        self.assertIs(response, redirect)

    async def test_private_submission_requires_officer_access(self) -> None:
        await self.db.upsert_hosted_form_setting(
            form_key="statement_of_occurrence",
            visibility="private",
            enabled=True,
            updated_by="tester",
        )
        with patch(
            "grievance_api.web.routes_hosted_forms.require_authenticated_officer",
            new=AsyncMock(return_value=SimpleNamespace(role="officer")),
        ) as mock_auth, patch(
            "grievance_api.web.routes_hosted_forms._post_internal_json",
            new=AsyncMock(return_value={"case_id": "C1", "grievance_id": "2026001"}),
        ):
            result = await submit_hosted_form(
                "statement_of_occurrence",
                _values_for_definition("statement_of_occurrence"),
                _Request(state=self._state(), host="198.51.100.10"),
            )

        self.assertEqual(result["backend_response"]["case_id"], "C1")
        self.assertEqual(mock_auth.await_count, 1)

    async def test_disabled_form_returns_404(self) -> None:
        await self.db.upsert_hosted_form_setting(
            form_key="statement_of_occurrence",
            visibility="public",
            enabled=False,
            updated_by="tester",
        )

        with self.assertRaises(HTTPException) as ctx:
            await hosted_form_page("statement_of_occurrence", _Request(state=self._state()))

        self.assertEqual(ctx.exception.status_code, 404)

    async def test_admin_settings_persist_and_list(self) -> None:
        request = _Request(state=self._state(), host="127.0.0.1")
        row = await update_hosted_form_setting(
            "statement_of_occurrence",
            HostedFormSettingsUpdateRequest(visibility="private", enabled=False),
            request,
        )
        listing = await hosted_forms_admin_settings(request)
        stored = next(item for item in listing.rows if item.form_key == "statement_of_occurrence")

        self.assertEqual(row.visibility, "private")
        self.assertFalse(row.enabled)
        self.assertEqual(stored.visibility, "private")
        self.assertFalse(stored.enabled)
        self.assertEqual(stored.public_path, "/forms/statement_of_occurrence")

    async def test_admin_page_redirects_to_login_when_not_authenticated(self) -> None:
        redirect = RedirectResponse("/auth/login?next=/officers/forms", status_code=303)
        with patch(
            "grievance_api.web.routes_hosted_forms.require_ops_page_access",
            new=AsyncMock(return_value=redirect),
        ):
            response = await hosted_forms_admin_page(_Request(state=self._state(), host="198.51.100.9"))

        self.assertIs(response, redirect)

    async def test_public_submission_rate_limit_returns_429(self) -> None:
        request = _Request(state=self._state(), host="203.0.113.10")
        values = _values_for_definition("statement_of_occurrence")
        with patch(
            "grievance_api.web.routes_hosted_forms._post_internal_json",
            new=AsyncMock(return_value={"case_id": "C1", "grievance_id": "2026001"}),
        ):
            for _ in range(10):
                await submit_hosted_form("statement_of_occurrence", values, request)
            with self.assertRaises(HTTPException) as ctx:
                await submit_hosted_form("statement_of_occurrence", values, request)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("Retry-After", ctx.exception.headers)
