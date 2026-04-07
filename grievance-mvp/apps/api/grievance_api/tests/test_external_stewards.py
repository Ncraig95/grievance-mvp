from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from grievance_api.core.config import (
    ChiefStewardContractScopeConfig,
    ExternalStewardAuthConfig,
    OfficerAuthConfig,
    OfficerTrackingConfig,
)
from grievance_api.db.db import Db, utcnow
from grievance_api.db.migrate import migrate
from grievance_api.web.models import (
    ExternalStewardActionRequest,
    ExternalStewardCaseAssignmentCreateRequest,
    ExternalStewardUserCreateRequest,
    ExternalStewardUserUpdateRequest,
    OfficerCaseBulkUpdateRequest,
    OfficerCaseCreateRequest,
    OfficerCaseUpdateRequest,
)
from grievance_api.web.officer_auth import current_external_steward_user, external_steward_callback
from grievance_api.web.routes_officers import (
    bulk_update_officer_cases,
    create_officer_case,
    delete_officer_case,
    officer_cases,
    officers_page,
    update_officer_case,
)
from grievance_api.web.routes_ops import ops_page
from grievance_api.web.routes_steward import (
    _render_steward_page,
    assign_case_external_steward,
    case_external_stewards,
    create_external_steward_user,
    external_steward_users,
    remove_case_external_steward,
    steward_action_sent_third_level,
    steward_action_settlement_complete,
    steward_cases,
    steward_page,
    update_external_steward_user,
)


class _Request:
    def __init__(
        self,
        *,
        state,
        host: str = "127.0.0.1",
        scheme: str = "https",
        session: dict | None = None,
        query_params: dict | None = None,
    ) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host=host)
        self.url = SimpleNamespace(
            scheme=scheme,
            netloc=host,
            hostname=host.split(":", 1)[0],
        )
        self.headers = {"host": host}
        self.session = session if session is not None else {}
        self.query_params = query_params if query_params is not None else {}

class _FakeExternalMsalClient:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result

    def acquire_token_by_auth_code_flow(self, flow: dict[str, object], query_params: dict[str, object]) -> dict[str, object]:
        return dict(self._result)


class ExternalStewardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    @staticmethod
    def _officer_auth(enabled: bool) -> OfficerAuthConfig:
        return OfficerAuthConfig(
            enabled=enabled,
            tenant_id="tenant-id",
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://grievance.example.org/auth/callback",
            post_logout_redirect_uri="https://grievance.example.org/",
            session_secret="session-secret",
            officer_group_ids=("group-officers",),
            admin_group_ids=("group-admins",),
            chief_steward_group_ids=("group-chief-stewards",),
            chief_steward_contract_scopes={
                "mobility": ChiefStewardContractScopeConfig(
                    group_ids=("group-chief-mobility",),
                    contract_aliases=("mobility", "at&t mobility"),
                ),
                "utilities": ChiefStewardContractScopeConfig(
                    group_ids=("group-chief-utilities",),
                    contract_aliases=("utilities",),
                ),
            },
        )

    @staticmethod
    def _external_auth(enabled: bool) -> ExternalStewardAuthConfig:
        return ExternalStewardAuthConfig(
            enabled=enabled,
            tenant_id="tenant-id",
            reuse_officer_auth_app=False,
            client_id="external-client-id",
            client_secret="external-client-secret",
            redirect_uri="https://grievance.example.org/auth/steward/callback",
            post_logout_redirect_uri="https://grievance.example.org/",
        )

    @classmethod
    def _cfg(cls, *, officer_auth_enabled: bool = True, external_auth_enabled: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            grievance_id=SimpleNamespace(timezone="America/New_York"),
            officer_tracking=OfficerTrackingConfig(
                roster=("Officer A", "Officer B", "grievance@cwa3106.com"),
            ),
            officer_auth=cls._officer_auth(officer_auth_enabled),
            external_steward_auth=cls._external_auth(external_auth_enabled),
        )

    @staticmethod
    def _staff_session(role: str, *, email: str = "admin@example.org") -> dict[str, object]:
        return {
            "officer_user": {
                "email": email,
                "display_name": email,
                "role": role,
                "contract_scopes": [],
                "exp": int(time.time()) + 3600,
            }
        }

    @staticmethod
    def _external_session(
        *,
        external_user_id: int,
        email: str,
        display_name: str = "Outside Steward",
        issuer: str = "https://external.example.org",
        provider_subject: str = "ext-subject-1",
    ) -> dict[str, object]:
        return {
            "external_steward_user": {
                "external_user_id": external_user_id,
                "email": email,
                "display_name": display_name,
                "role": "external_steward",
                "auth_source": "microsoft_oidc",
                "issuer": issuer,
                "provider_subject": provider_subject,
                "verified_email": True,
                "exp": int(time.time()) + 3600,
            }
        }

    async def _insert_case(
        self,
        *,
        case_id: str,
        grievance_id: str,
        intake_request_id: str,
        contract: str | None = None,
        member_name: str = "Member Name",
        workflow_status: str = "awaiting_signatures",
        officer_status: str | None = None,
        officer_assignee: str | None = None,
        officer_notes: str | None = None,
    ) -> None:
        payload = {
            "request_id": intake_request_id,
            "contract": contract or "",
            "grievant_firstname": member_name.split()[0],
            "grievant_lastname": " ".join(member_name.split()[1:]),
            "narrative": f"Summary for {case_id}",
            "documents": [],
        }
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json,
                 tracking_contract, officer_status, officer_assignee, officer_notes
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                grievance_id,
                "2026-04-01T15:00:00+00:00",
                workflow_status,
                "pending",
                None,
                member_name,
                f"{case_id.lower()}@example.org",
                intake_request_id,
                json.dumps(payload),
                contract,
                officer_status,
                officer_assignee,
                officer_notes,
            ),
        )

    async def _insert_external_user(
        self,
        *,
        email: str,
        display_name: str = "Outside Steward",
        status: str = "active",
        issuer: str | None = None,
        subject: str | None = None,
    ) -> int:
        now = utcnow()
        await self.db.exec(
            """
            INSERT INTO external_steward_users(
              email, display_name, status, auth_source, auth_issuer, auth_subject,
              invited_by, created_at_utc, updated_at_utc, last_login_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                email.lower(),
                display_name,
                status,
                "microsoft_oidc" if issuer and subject else None,
                issuer,
                subject,
                "admin@example.org",
                now,
                now,
                None,
            ),
        )
        row = await self.db.fetchone("SELECT id FROM external_steward_users WHERE email=?", (email.lower(),))
        return int(row[0])

    async def _assign_external_user(self, *, external_user_id: int, case_id: str) -> None:
        now = utcnow()
        await self.db.exec(
            """
            INSERT INTO external_steward_case_assignments(
              external_steward_user_id, case_id, created_at_utc, updated_at_utc, assigned_by
            ) VALUES(?,?,?,?,?)
            """,
            (external_user_id, case_id, now, now, "admin@example.org"),
        )

    async def test_unauthenticated_steward_page_redirects_to_external_login(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            host="8.8.8.8",
        )

        response = await steward_page(request)

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/auth/steward/login?next=/steward", response.headers["location"])

    async def test_render_steward_page_includes_mobile_table_layout(self) -> None:
        html = _render_steward_page()

        self.assertIn('name="viewport"', html)
        self.assertIn('class="page-shell"', html)
        self.assertIn('id="stewardCasesTable"', html)
        self.assertIn("@media (max-width: 760px)", html)
        self.assertIn("#stewardCasesTable td::before", html)
        self.assertIn('data-label="Action Date"', html)

    async def test_external_callback_binds_allowlisted_user_and_stores_session(self) -> None:
        external_user_id = await self._insert_external_user(email="outside@example.org")
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            host="grievance.example.org",
            session={
                "external_steward_auth_flow": {"state": "abc"},
                "external_steward_auth_next": "/steward",
            },
            query_params={"code": "good-code", "state": "abc"},
        )
        token_result = {
            "access_token": "graph-access-token",
            "id_token_claims": {
                "exp": int(time.time()) + 3600,
                "iss": "https://login.microsoftonline.com/tenant-id/v2.0",
                "oid": "provider-subject-1",
                "sub": "ignored-subject",
                "email": "outside@example.org",
                "name": "Outside Steward",
            },
        }

        with (
            patch(
                "grievance_api.web.officer_auth._build_external_steward_msal_client",
                return_value=_FakeExternalMsalClient(token_result),
            ),
        ):
            response = await external_steward_callback(request)

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/steward")
        session_payload = request.session["external_steward_user"]
        self.assertEqual(session_payload["external_user_id"], external_user_id)
        self.assertEqual(session_payload["email"], "outside@example.org")
        self.assertEqual(session_payload["role"], "external_steward")

        current = await current_external_steward_user(request)
        self.assertIsNotNone(current)
        self.assertEqual(current.email, "outside@example.org")

        row = await self.db.fetchone(
            """
            SELECT auth_source, auth_issuer, auth_subject, last_login_at_utc
            FROM external_steward_users
            WHERE id=?
            """,
            (external_user_id,),
        )
        self.assertEqual(row[0], "microsoft_oidc")
        self.assertEqual(row[1], "https://login.microsoftonline.com/tenant-id/v2.0")
        self.assertEqual(row[2], "provider-subject-1")
        self.assertTrue(str(row[3] or "").strip())

    async def test_external_callback_denies_unallowlisted_user(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            session={
                "external_steward_auth_flow": {"state": "abc"},
                "external_steward_auth_next": "/steward",
            },
            query_params={"code": "good-code", "state": "abc"},
        )
        token_result = {
            "id_token_claims": {
                "exp": int(time.time()) + 3600,
                "iss": "https://login.microsoftonline.com/tenant-id/v2.0",
                "oid": "provider-subject-2",
                "email": "not-allowed@example.org",
                "name": "Not Allowed",
            }
        }

        with (
            patch(
                "grievance_api.web.officer_auth._build_external_steward_msal_client",
                return_value=_FakeExternalMsalClient(token_result),
            ),
        ):
            with self.assertRaises(HTTPException) as exc:
                await external_steward_callback(request)

        self.assertEqual(exc.exception.status_code, 403)
        self.assertIn("allowlisted", exc.exception.detail)

    async def test_external_callback_without_session_flow_restarts_login(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            session={},
            query_params={"code": "good-code", "state": "abc"},
        )

        response = await external_steward_callback(request)

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/steward/login?next=/steward")

    async def test_external_callback_denies_subject_mismatch_for_bound_identity(self) -> None:
        await self._insert_external_user(
            email="outside@example.org",
            issuer="https://external.example.org",
            subject="bound-subject",
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            session={
                "external_steward_auth_flow": {"state": "abc"},
                "external_steward_auth_next": "/steward",
            },
            query_params={"code": "good-code", "state": "abc"},
        )
        token_result = {
            "id_token_claims": {
                "exp": int(time.time()) + 3600,
                "iss": "https://external.example.org",
                "oid": "different-subject",
                "email": "outside@example.org",
            }
        }

        with (
            patch(
                "grievance_api.web.officer_auth._build_external_steward_msal_client",
                return_value=_FakeExternalMsalClient(token_result),
            ),
        ):
            with self.assertRaises(HTTPException) as exc:
                await external_steward_callback(request)

        self.assertEqual(exc.exception.status_code, 403)
        self.assertIn("different identity", exc.exception.detail)

    async def test_external_steward_only_sees_assigned_cases_and_fixed_actions_update_limited_fields(self) -> None:
        external_user_id = await self._insert_external_user(
            email="outside@example.org",
            issuer="https://external.example.org",
            subject="provider-subject-1",
        )
        await self._insert_case(
            case_id="C1",
            grievance_id="2027001",
            intake_request_id="forms-ext-1",
            contract="Mobility",
            officer_assignee="Officer A",
            officer_notes="keep me",
        )
        await self._insert_case(
            case_id="C2",
            grievance_id="2027002",
            intake_request_id="forms-ext-2",
            contract="Utilities",
        )
        await self._assign_external_user(external_user_id=external_user_id, case_id="C1")
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            session=self._external_session(
                external_user_id=external_user_id,
                email="outside@example.org",
                provider_subject="provider-subject-1",
            ),
            host="8.8.8.8",
        )

        cases = await steward_cases(request)

        self.assertEqual(cases.count, 1)
        self.assertEqual(cases.rows[0].case_id, "C1")
        self.assertIn("sent_third_level", cases.rows[0].available_actions)
        self.assertNotIn("C2", [row.case_id for row in cases.rows])

        third_level = await steward_action_sent_third_level(
            "C1",
            ExternalStewardActionRequest(action_date="2026-04-02"),
            request,
        )
        self.assertEqual(third_level.third_level_request_sent_date, "2026-04-02")

        settlement = await steward_action_settlement_complete(
            "C1",
            ExternalStewardActionRequest(action_date="2026-04-03"),
            request,
        )
        self.assertEqual(settlement.officer_status, "closed")
        self.assertTrue(str(settlement.officer_closed_at_utc or "").strip())
        self.assertEqual(settlement.officer_closed_by, "Outside Steward")

        case_row = await self.db.fetchone(
            """
            SELECT status, officer_status, officer_assignee, officer_notes,
                   tracking_third_level_request_sent_date, officer_closed_by
            FROM cases
            WHERE id=?
            """,
            ("C1",),
        )
        self.assertEqual(case_row[0], "awaiting_signatures")
        self.assertEqual(case_row[1], "closed")
        self.assertEqual(case_row[2], "Officer A")
        self.assertEqual(case_row[3], "keep me")
        self.assertEqual(case_row[4], "2026-04-02")
        self.assertEqual(case_row[5], "Outside Steward")

        event_row = await self.db.fetchone(
            "SELECT event_type, details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            ("C1",),
        )
        details = json.loads(event_row[1])
        self.assertEqual(event_row[0], "external_steward_action_settlement_complete")
        self.assertEqual(details["actor_role"], "external_steward")
        self.assertEqual(details["actor_email"], "outside@example.org")
        self.assertEqual(details["actor_provider_subject"], "provider-subject-1")
        self.assertEqual(details["action_date"], "2026-04-03")

    async def test_external_steward_cannot_access_officer_or_ops_routes_or_mutations(self) -> None:
        external_user_id = await self._insert_external_user(
            email="outside@example.org",
            issuer="https://external.example.org",
            subject="provider-subject-1",
        )
        await self._insert_case(
            case_id="C1",
            grievance_id="2027101",
            intake_request_id="forms-restricted-1",
            contract="Mobility",
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            session=self._external_session(
                external_user_id=external_user_id,
                email="outside@example.org",
                provider_subject="provider-subject-1",
            ),
            host="8.8.8.8",
        )

        with self.assertRaises(HTTPException) as officers_page_exc:
            await officers_page(request)
        with self.assertRaises(HTTPException) as officers_cases_exc:
            await officer_cases(request)
        with self.assertRaises(HTTPException) as ops_exc:
            await ops_page(request)
        with self.assertRaises(HTTPException) as create_exc:
            await create_officer_case(
                OfficerCaseCreateRequest(member_name="Pat Member", contract="Mobility"),
                request,
            )
        with self.assertRaises(HTTPException) as update_exc:
            await update_officer_case(
                "C1",
                OfficerCaseUpdateRequest(officer_status="closed"),
                request,
            )
        with self.assertRaises(HTTPException) as bulk_exc:
            await bulk_update_officer_cases(
                OfficerCaseBulkUpdateRequest(case_ids=["C1"], officer_status="closed"),
                request,
            )
        with self.assertRaises(HTTPException) as delete_exc:
            await delete_officer_case("C1", request)

        self.assertEqual(officers_page_exc.exception.status_code, 403)
        self.assertEqual(officers_cases_exc.exception.status_code, 403)
        self.assertEqual(ops_exc.exception.status_code, 403)
        self.assertEqual(create_exc.exception.status_code, 403)
        self.assertEqual(update_exc.exception.status_code, 403)
        self.assertEqual(bulk_exc.exception.status_code, 403)
        self.assertEqual(delete_exc.exception.status_code, 403)

    async def test_admin_can_allowlist_disable_assign_and_unassign_external_stewards_with_audit(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2027201",
            intake_request_id="forms-admin-ext-1",
            contract="Mobility",
        )
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(), db=self.db),
            session=self._staff_session("admin"),
            host="8.8.8.8",
        )

        created = await create_external_steward_user(
            ExternalStewardUserCreateRequest(
                email="outside@example.org",
                display_name="Outside Steward",
            ),
            admin_request,
        )
        self.assertEqual(created.status, "active")
        self.assertEqual(created.assignment_count, 0)

        listing = await external_steward_users(admin_request)
        self.assertEqual(len(listing.rows), 1)
        self.assertEqual(listing.rows[0].email, "outside@example.org")

        disabled = await update_external_steward_user(
            created.user_id,
            ExternalStewardUserUpdateRequest(status="disabled"),
            admin_request,
        )
        self.assertEqual(disabled.status, "disabled")

        with self.assertRaises(HTTPException) as assign_disabled_exc:
            await assign_case_external_steward(
                "C1",
                ExternalStewardCaseAssignmentCreateRequest(external_steward_user_id=created.user_id),
                admin_request,
            )
        self.assertEqual(assign_disabled_exc.exception.status_code, 400)

        active = await update_external_steward_user(
            created.user_id,
            ExternalStewardUserUpdateRequest(status="active"),
            admin_request,
        )
        self.assertEqual(active.status, "active")

        assignment = await assign_case_external_steward(
            "C1",
            ExternalStewardCaseAssignmentCreateRequest(external_steward_user_id=created.user_id),
            admin_request,
        )
        self.assertEqual(assignment.case_id, "C1")
        self.assertEqual(assignment.email, "outside@example.org")

        assignments = await case_external_stewards("C1", admin_request)
        self.assertEqual(len(assignments.rows), 1)
        self.assertEqual(assignments.rows[0].assignment_id, assignment.assignment_id)

        removed = await remove_case_external_steward("C1", assignment.assignment_id, admin_request)
        self.assertEqual(removed.assignment_id, assignment.assignment_id)

        user_events = await self.db.fetchall(
            "SELECT event_type FROM events WHERE case_id=? ORDER BY id",
            (f"external_steward:{created.user_id}",),
        )
        self.assertEqual(
            [str(row[0]) for row in user_events],
            [
                "external_steward_user_upserted",
                "external_steward_status_updated",
                "external_steward_status_updated",
            ],
        )

        case_events = await self.db.fetchall(
            "SELECT event_type FROM events WHERE case_id=? ORDER BY id",
            ("C1",),
        )
        self.assertEqual(
            [str(row[0]) for row in case_events],
            [
                "external_steward_case_assignment_added",
                "external_steward_case_assignment_removed",
            ],
        )


if __name__ == "__main__":
    unittest.main()
