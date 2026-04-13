from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from grievance_api.core.config import (
    ChiefStewardContractScopeConfig,
    OfficerAuthConfig,
    OfficerTrackingConfig,
)
from grievance_api.core.officer_auth import validate_officer_auth_config
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services.sharepoint_graph import CaseFolderRef, DirectoryUserRef
from grievance_api.web.officer_auth import officer_callback, officer_login
from grievance_api.web.models import (
    ChiefStewardAssignmentCreateRequest,
    OfficerCaseBulkDeleteRequest,
    OfficerCaseBulkUpdateRequest,
    OfficerCaseCreateRequest,
    OfficerCaseUpdateRequest,
    OfficerAutoDataRequestDocument,
)
from grievance_api.web.routes_officers import (
    _build_auto_data_request_payload,
    bulk_delete_officer_cases,
    bulk_update_officer_cases,
    chief_steward_assignments,
    create_officer_case,
    create_chief_steward_assignment,
    delete_officer_case,
    delete_chief_steward_assignment,
    officer_auto_data_request,
    officer_directory_users,
    officer_case_events,
    officer_cases,
    officer_next_grievance_number,
    officers_page,
    update_officer_case,
)
from grievance_api.web.routes_ops import ops_page


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


class _MsalClientStub:
    def __init__(self, claims: dict[str, object]) -> None:
        self.claims = claims
        self.last_scopes: list[str] | None = None
        self.last_redirect_uri: str | None = None

    def acquire_token_by_auth_code_flow(self, flow: dict[str, object], query_params: dict[str, object]) -> dict[str, object]:
        _ = flow
        _ = query_params
        return {"id_token_claims": self.claims}

    def initiate_auth_code_flow(self, scopes: list[str], redirect_uri: str) -> dict[str, object]:
        self.last_scopes = list(scopes)
        self.last_redirect_uri = redirect_uri
        return {"state": "abc123", "auth_uri": "https://login.microsoftonline.com/example/oauth2/v2.0/authorize"}


class _DirectoryGraphStub:
    def __init__(self, rows: list[DirectoryUserRef]) -> None:
        self.rows = rows
        self.last_search: str | None = None
        self.last_limit: int | None = None

    def search_directory_users(self, search_text: str, *, limit: int = 10) -> list[DirectoryUserRef]:
        self.last_search = search_text
        self.last_limit = limit
        return list(self.rows)


class _FailingDirectoryGraphStub:
    def __init__(self, message: str) -> None:
        self.message = message
        self.last_search: str | None = None
        self.last_limit: int | None = None

    def search_directory_users(self, search_text: str, *, limit: int = 10) -> list[DirectoryUserRef]:
        self.last_search = search_text
        self.last_limit = limit
        raise RuntimeError(self.message)


class _CaseFolderGraphStub:
    def __init__(self, case_folder_names: list[str] | None = None) -> None:
        self.ensure_calls: list[dict[str, object]] = []
        self.case_folder_names = list(case_folder_names or [])

    def ensure_case_folder(self, **kwargs):  # noqa: ANN003
        self.ensure_calls.append(dict(kwargs))
        grievance_id = str(kwargs["grievance_id"])
        member_name = str(kwargs["member_name"])
        folder_name = f"{grievance_id} {member_name}".strip()
        return CaseFolderRef(
            drive_id="drive-1",
            folder_id="folder-1",
            folder_name=folder_name,
            web_url=f"https://sharepoint.local/{folder_name.replace(' ', '%20')}",
        )

    def list_case_folder_names(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return list(self.case_folder_names)


class OfficerTrackerTests(unittest.IsolatedAsyncioTestCase):
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

    @classmethod
    def _cfg(cls, *, auth_enabled: bool) -> SimpleNamespace:
        return SimpleNamespace(
            officer_tracking=OfficerTrackingConfig(
                roster=("Officer A", "Officer B", "grievance@cwa3106.com"),
            ),
            grievance_id=SimpleNamespace(
                mode="auto",
                timezone="America/New_York",
                min_width=3,
                separator="",
            ),
            officer_auth=cls._officer_auth(auth_enabled),
            graph=SimpleNamespace(
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/grievance",
                document_library="Documents",
                case_parent_folder="Grievances",
            ),
        )

    @staticmethod
    def _session_user(
        role: str,
        *,
        email: str = "user@example.org",
        scopes: tuple[str, ...] = (),
        group_ids: tuple[str, ...] = (),
        user_id: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "email": email,
            "display_name": email,
            "role": role,
            "contract_scopes": list(scopes),
            "exp": int(time.time()) + 3600,
        }
        if group_ids:
            payload["group_ids"] = list(group_ids)
        if user_id:
            payload["user_id"] = user_id
        return {"officer_user": payload}

    async def _insert_case(
        self,
        *,
        case_id: str,
        grievance_id: str,
        intake_request_id: str,
        contract: str | None = None,
        member_name: str = "Member Name",
        tracking_department: str | None = None,
    ) -> None:
        payload = {
            "request_id": intake_request_id,
            "contract": contract or "",
            "grievant_firstname": member_name.split()[0],
            "grievant_lastname": " ".join(member_name.split()[1:]),
            "documents": [],
        }
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json,
                 tracking_contract, tracking_department
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                grievance_id,
                "2026-03-27T15:00:00+00:00",
                "awaiting_signatures",
                "pending",
                None,
                member_name,
                f"{case_id.lower()}@example.org",
                intake_request_id,
                json.dumps(payload),
                contract,
                tracking_department,
            ),
        )

    async def test_migrate_adds_officer_columns_to_legacy_cases_table(self) -> None:
        legacy_path = str(Path(self.tmpdir.name) / "legacy.sqlite3")
        con = sqlite3.connect(legacy_path)
        try:
            con.execute(
                """
                CREATE TABLE cases (
                  id TEXT PRIMARY KEY,
                  grievance_id TEXT NOT NULL,
                  created_at_utc TEXT NOT NULL,
                  status TEXT NOT NULL,
                  member_name TEXT NOT NULL,
                  intake_request_id TEXT NOT NULL,
                  intake_payload_json TEXT NOT NULL
                )
                """
            )
            con.commit()
        finally:
            con.close()

        migrate(legacy_path)

        con = sqlite3.connect(legacy_path)
        try:
            cols = {str(row[1]) for row in con.execute("PRAGMA table_info(cases)").fetchall()}
            chief_cols = {str(row[1]) for row in con.execute("PRAGMA table_info(chief_steward_assignments)").fetchall()}
        finally:
            con.close()

        self.assertIn("officer_status", cols)
        self.assertIn("tracking_contract", cols)
        self.assertIn("tracking_department", cols)
        self.assertIn("tracking_second_level_request_sent_date", cols)
        self.assertIn("tracking_third_level_request_sent_date", cols)
        self.assertIn("tracking_fourth_level_request_sent_date", cols)

        self.assertIn("principal_email", chief_cols)
        self.assertIn("contract_scope", chief_cols)

    async def test_officer_cases_uses_existing_payload_fallbacks_and_contract_scope(self) -> None:
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "C1",
                "2026015",
                "2026-03-27T15:00:00+00:00",
                "awaiting_signatures",
                "pending",
                None,
                "Nick Craig",
                "nick@example.org",
                "forms-1",
                json.dumps(
                    {
                        "request_id": "forms-1",
                        "contract": "AT&T Mobility",
                        "grievant_email": "nick@example.org",
                        "incident_date": "2026-03-20",
                        "narrative": "Basic summary",
                        "template_data": {
                            "q2_department": "Utilities",
                            "q5_union_rep_name_attuid": "Steward Smith",
                            "issue_text": "Contract issue details",
                        },
                    }
                ),
            ),
        )
        request = _Request(state=SimpleNamespace(cfg=self._cfg(auth_enabled=False), db=self.db))

        result = await officer_cases(request)

        self.assertEqual(result.count, 1)
        row = result.rows[0]
        self.assertEqual(row.department, "Utilities")
        self.assertEqual(row.steward, "Steward Smith")
        self.assertEqual(row.occurrence_date, "2026-03-20")
        self.assertEqual(row.issue_summary, "Contract issue details")
        self.assertEqual(row.contract, "AT&T Mobility")
        self.assertEqual(row.contract_scope, "mobility")
        self.assertEqual(result.viewer.role, "read_only")

    async def test_officers_page_hides_mutation_controls_when_auth_disabled(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg(auth_enabled=False), db=self.db))

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn("Local Read-Only Mode", html)
        self.assertNotIn("Create Paper Grievance", html)
        self.assertNotIn("Delete Checked Rows", html)
        self.assertNotIn("Save Edits", html)

    async def test_officers_page_renders_status_options_in_create_and_edit_forms(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")
        create_select = html[
            html.index('<select id="createOfficerStatus">') : html.index("</select>", html.index('<select id="createOfficerStatus">'))
        ]
        edit_select = html[
            html.index('<select id="editOfficerStatus">') : html.index("</select>", html.index('<select id="editOfficerStatus">'))
        ]

        self.assertIn('<select id="createOfficerStatus">', html)
        self.assertIn('<select id="editOfficerStatus">', html)
        self.assertIn('<option value="open">Open</option>', create_select)
        self.assertIn('<option value="open_at_state">Open at State</option>', create_select)
        self.assertIn('<option value="open_at_national">Open at National</option>', create_select)
        self.assertIn('<option value="open">Open</option>', edit_select)
        self.assertIn('<option value="open_at_state">Open at State</option>', edit_select)
        self.assertIn('<option value="open_at_national">Open at National</option>', edit_select)

    async def test_officers_page_renders_auto_data_request_action_for_editors(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn("Auto Data Request", html)
        self.assertIn("async function autoDataRequest(caseId)", html)
        self.assertIn("data-action=\"auto-data-request\"", html)

    async def test_officers_page_manual_entry_uses_single_grievance_field_and_fixed_scope_dropdown(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn("Grievance ID / Number", html)
        self.assertIn('<select id="createContract">', html)
        self.assertNotIn('id="createGrievanceId"', html)
        self.assertIn('<select id="editContract"', html)
        self.assertIn('const FIXED_SCOPE_OPTIONS =', html)
        for label in (
            "City of Jacksonville (COJ)",
            "Wire Tech (WT)",
            "Core Southeastern",
            "Construction",
            "Yellow Pages / Thrive",
            "Mobility / IHX",
        ):
            self.assertIn(label, html)

    async def test_officers_page_manual_entry_prefills_next_grievance_number_softly(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn("async function loadNextGrievanceNumber()", html)
        self.assertIn("await call('/officers/grievance-number/next')", html)
        self.assertIn("if (!VIEWER.can_create || !input || input.value.trim()) return;", html)
        self.assertIn("void loadNextGrievanceNumber();", html)

    async def test_admin_can_preview_next_grievance_number(self) -> None:
        graph = _CaseFolderGraphStub(case_folder_names=["2026009 Existing Person"])
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db, graph=graph, logger=SimpleNamespace()),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )
        await self.db.exec(
            """
            INSERT INTO grievance_id_sequences(year, last_seq, updated_at_utc)
            VALUES(?,?,?)
            """,
            (2026, 11, "2026-04-13T00:00:00+00:00"),
        )

        with patch("grievance_api.services.grievance_id_allocator.current_year_in_timezone", return_value=2026):
            response = await officer_next_grievance_number(request)

        self.assertEqual(response.grievance_id, "2026012")
        self.assertEqual(response.grievance_number, "2026012")
        self.assertEqual(response.sequence, 12)
        self.assertEqual(response.sharepoint_max_seq, 9)
        self.assertEqual(response.db_last_seq, 11)

    async def test_officers_page_renders_sticky_tracker_header_css(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn("#trackerTable thead th {", html)
        self.assertIn("position: sticky;", html)
        self.assertIn("top: 0;", html)
        self.assertIn(".tracker-table-wrap {", html)
        self.assertIn("max-height: 70vh;", html)
        self.assertIn("overflow-y: auto;", html)
        self.assertIn('<div class="table-wrap tracker-table-wrap">', html)

    async def test_officers_page_renders_hero_and_tracker_metrics(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn('class="panel hero-panel"', html)
        self.assertIn("Officer Workspace", html)
        self.assertIn('id="trackerStats"', html)
        self.assertIn('id="metricTotalValue">0</div>', html)
        self.assertIn('id="metricEscalatedValue">0</div>', html)
        self.assertIn('href="#trackerPanel"', html)

    async def test_officers_page_deduplicates_scope_pills_for_admin(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")
        marker = '<div class="scope-pills">'
        start = html.index(marker)
        end = html.index("</div>", start)
        scope_pills = html[start:end]

        self.assertEqual(scope_pills.count("Mobility / IHX"), 1)
        self.assertEqual(scope_pills.count("Core Southeastern"), 1)
        self.assertEqual(scope_pills.count("City of Jacksonville (COJ)"), 1)
        self.assertEqual(scope_pills.count("Wire Tech (WT)"), 1)
        self.assertEqual(scope_pills.count("Construction"), 1)
        self.assertEqual(scope_pills.count("Yellow Pages / Thrive"), 1)

    async def test_officers_page_renders_workspace_menu_with_ops_link_for_admin(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officers_page(request)
        html = response.body.decode("utf-8")

        self.assertIn('id="workspaceMenuPanel"', html)
        self.assertIn("Quick Nav", html)
        self.assertIn('class="workspace-menu-bar"', html)
        self.assertIn('href="#filtersPanel"', html)
        self.assertIn('href="#mutationSplit"', html)
        self.assertIn('href="/ops"', html)
        self.assertIn("Ops Console", html)

    async def test_officer_mutation_routes_block_when_auth_disabled(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg(auth_enabled=False), db=self.db))
        await self._insert_case(
            case_id="C1",
            grievance_id="2026101",
            intake_request_id="forms-C1",
            contract="Mobility",
        )

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
        with self.assertRaises(HTTPException) as bulk_delete_exc:
            await bulk_delete_officer_cases(
                OfficerCaseBulkDeleteRequest(case_ids=["C1"]),
                request,
            )

        self.assertEqual(create_exc.exception.status_code, 423)
        self.assertEqual(update_exc.exception.status_code, 423)
        self.assertEqual(bulk_exc.exception.status_code, 423)
        self.assertEqual(delete_exc.exception.status_code, 423)
        self.assertEqual(bulk_delete_exc.exception.status_code, 423)

    async def test_unauthenticated_pages_redirect_to_login_when_auth_enabled(self) -> None:
        state = SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db)

        officers_response = await officers_page(_Request(state=state, host="8.8.8.8"))
        ops_response = await ops_page(_Request(state=state, host="8.8.8.8"))

        self.assertIsInstance(officers_response, RedirectResponse)
        self.assertIsInstance(ops_response, RedirectResponse)
        self.assertIn("/auth/login?next=/officers", officers_response.headers["location"])
        self.assertIn("/auth/login?next=/ops", ops_response.headers["location"])

    async def test_officer_login_callback_stores_session(self) -> None:
        cfg = self._cfg(auth_enabled=True)
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session={"officer_auth_flow": {"state": "abc"}, "officer_auth_next": "/officers"},
            query_params={"code": "test-code", "state": "abc"},
        )
        claims = {
            "exp": int(time.time()) + 3600,
            "name": "Officer User",
            "preferred_username": "officer@example.org",
            "groups": ["group-officers"],
        }

        with patch("grievance_api.web.officer_auth._build_msal_client", return_value=_MsalClientStub(claims)):
            response = await officer_callback(request)

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/officers")
        self.assertEqual(request.session["officer_user"]["role"], "officer")
        self.assertEqual(request.session["officer_user"]["email"], "officer@example.org")

    async def test_officer_login_uses_non_reserved_scopes(self) -> None:
        cfg = self._cfg(auth_enabled=True)
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            host="grievance.example.org",
        )
        client = _MsalClientStub({})

        with patch("grievance_api.web.officer_auth._build_msal_client", return_value=client):
            response = await officer_login(request, next="/officers")

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "https://login.microsoftonline.com/example/oauth2/v2.0/authorize")
        self.assertEqual(client.last_scopes, [])
        self.assertEqual(client.last_redirect_uri, "https://grievance.example.org/auth/callback")
        self.assertIn("officer_auth_flow", request.session)

    async def test_officer_login_on_canonical_host_does_not_redirect_loop_when_internal_scheme_differs(self) -> None:
        cfg = self._cfg(auth_enabled=True)
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            host="grievance.example.org",
            scheme="http",
        )
        client = _MsalClientStub({})

        with patch("grievance_api.web.officer_auth._build_msal_client", return_value=client):
            response = await officer_login(request, next="/officers")

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "https://login.microsoftonline.com/example/oauth2/v2.0/authorize")

    async def test_officer_login_redirects_to_configured_auth_host(self) -> None:
        cfg = self._cfg(auth_enabled=True)
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            host="api.example.org",
        )

        response = await officer_login(request, next="/officers")

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "https://grievance.example.org/auth/login?next=/officers")

    async def test_officer_callback_without_session_flow_redirects_back_to_login(self) -> None:
        cfg = self._cfg(auth_enabled=True)
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            query_params={"code": "test-code", "state": "missing"},
        )

        response = await officer_callback(request)

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/auth/login?next=/officers")

    async def test_missing_group_claims_are_denied_cleanly(self) -> None:
        cfg = self._cfg(auth_enabled=True)
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session={"officer_auth_flow": {"state": "abc"}, "officer_auth_next": "/officers"},
            query_params={"code": "test-code", "state": "abc"},
        )
        claims = {
            "exp": int(time.time()) + 3600,
            "name": "Officer User",
            "preferred_username": "officer@example.org",
        }

        with patch("grievance_api.web.officer_auth._build_msal_client", return_value=_MsalClientStub(claims)):
            with self.assertRaises(HTTPException) as exc:
                await officer_callback(request)

        self.assertEqual(exc.exception.status_code, 403)
        self.assertIn("group claims", exc.exception.detail)

    async def test_auth_validation_allows_ui_only_chief_scope_mappings(self) -> None:
        cfg = OfficerAuthConfig(
            enabled=True,
            tenant_id="tenant-id",
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://grievance.example.org/auth/callback",
            post_logout_redirect_uri="https://grievance.example.org/",
            session_secret="session-secret",
            officer_group_ids=("officers-group",),
            admin_group_ids=("admins-group",),
            chief_steward_group_ids=("chiefs-group",),
            chief_steward_contract_scopes={
                "wire_tech": ChiefStewardContractScopeConfig(
                    group_ids=(),
                    contract_aliases=("Wire Tech", "WT"),
                )
            },
        )

        validate_officer_auth_config(cfg)

    async def test_officer_can_view_all_cases_but_cannot_edit(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026101",
            intake_request_id="forms-C1",
            contract="Mobility",
        )
        await self._insert_case(
            case_id="C2",
            grievance_id="2026102",
            intake_request_id="forms-C2",
            contract="Utilities",
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("officer", email="officer@example.org"),
            host="8.8.8.8",
        )

        result = await officer_cases(request)

        self.assertEqual(result.count, 2)
        self.assertEqual(result.viewer.role, "officer")
        with self.assertRaises(HTTPException) as exc:
            await update_officer_case(
                "C1",
                OfficerCaseUpdateRequest(officer_status="closed"),
                request,
            )
        self.assertEqual(exc.exception.status_code, 403)

    async def test_chief_steward_only_sees_in_scope_cases_and_can_edit_in_scope(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026201",
            intake_request_id="forms-chief-1",
            contract="Mobility",
        )
        await self._insert_case(
            case_id="C2",
            grievance_id="2026202",
            intake_request_id="forms-chief-2",
            contract="Utilities",
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("chief_steward", email="chief@example.org", scopes=("mobility",)),
            host="8.8.8.8",
        )

        result = await officer_cases(request)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.rows[0].case_id, "C1")

        updated = await update_officer_case(
            "C1",
            OfficerCaseUpdateRequest(officer_status="closed", officer_notes="Handled"),
            request,
        )
        self.assertEqual(updated.officer_status, "closed")
        event_row = await self.db.fetchone(
            "SELECT details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            ("C1",),
        )
        details = json.loads(event_row[0])
        self.assertEqual(details["actor_role"], "chief_steward")
        self.assertEqual(details["actor_email"], "chief@example.org")
        self.assertEqual(details["changes"]["officer_status"], "closed")

        with self.assertRaises(HTTPException) as exc:
            await update_officer_case(
                "C2",
                OfficerCaseUpdateRequest(officer_status="closed"),
                request,
            )
        self.assertEqual(exc.exception.status_code, 403)

    async def test_chief_steward_mobility_scope_can_access_mobility_ihx_cases(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026211",
            intake_request_id="forms-chief-mihx-1",
            contract="Mobility / IHX",
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("chief_steward", email="chief@example.org", scopes=("mobility",)),
            host="8.8.8.8",
        )

        result = await officer_cases(request)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.rows[0].contract_scope, "mobility_ihx")

        updated = await update_officer_case(
            "C1",
            OfficerCaseUpdateRequest(officer_status="closed"),
            request,
        )
        self.assertEqual(updated.officer_status, "closed")

    async def test_admin_can_assign_chief_steward_scope_in_ui_and_session_picks_it_up(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026251",
            intake_request_id="forms-chief-ui-1",
            contract="Mobility",
        )
        await self._insert_case(
            case_id="C2",
            grievance_id="2026252",
            intake_request_id="forms-chief-ui-2",
            contract="Utilities",
        )
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        saved = await create_chief_steward_assignment(
            ChiefStewardAssignmentCreateRequest(
                principal_email="chief.ui@example.org",
                principal_display_name="Chief UI",
                contract_scope="mobility",
            ),
            admin_request,
        )
        self.assertEqual(saved.principal_email, "chief.ui@example.org")
        self.assertEqual(saved.contract_scope, "mobility")

        listing = await chief_steward_assignments(admin_request)
        self.assertEqual(len(listing.rows), 1)
        self.assertEqual(listing.rows[0].principal_display_name, "Chief UI")

        chief_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user(
                "officer",
                email="chief.ui@example.org",
                group_ids=("group-chief-stewards",),
                user_id="chief-ui-oid",
            ),
            host="8.8.8.8",
        )
        cases = await officer_cases(chief_request)
        self.assertEqual(cases.viewer.role, "chief_steward")
        self.assertEqual(cases.viewer.contract_scopes, ["mobility"])
        self.assertEqual([row.case_id for row in cases.rows], ["C1"])

        deleted = await delete_chief_steward_assignment(saved.assignment_id, admin_request)
        self.assertEqual(deleted.assignment_id, saved.assignment_id)

    async def test_chief_assignment_ui_includes_default_manual_scopes(self) -> None:
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        listing = await chief_steward_assignments(admin_request)

        self.assertIn("coj", listing.available_contract_scopes)
        self.assertIn("wire_tech", listing.available_contract_scopes)
        self.assertIn("core_southeastern", listing.available_contract_scopes)
        self.assertIn("construction", listing.available_contract_scopes)
        self.assertIn("yellow_pages_thrive", listing.available_contract_scopes)
        self.assertIn("mobility_ihx", listing.available_contract_scopes)

    async def test_admin_can_search_directory_users_for_assignment_ui(self) -> None:
        graph = _DirectoryGraphStub(
            [
                DirectoryUserRef(
                    id="oid-chief-1",
                    display_name="Jamie McKinney",
                    email="jmckinney@cwa3106.com",
                    user_principal_name="jmckinney@cwa3106.com",
                )
            ]
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db, graph=graph),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        response = await officer_directory_users(request, search="jmck", limit=8)

        self.assertEqual(response.search, "jmck")
        self.assertEqual(response.count, 1)
        self.assertEqual(response.rows[0].principal_id, "oid-chief-1")
        self.assertEqual(response.rows[0].email, "jmckinney@cwa3106.com")
        self.assertEqual(response.rows[0].match_source, "directory")
        self.assertIsNone(response.warning)
        self.assertEqual(graph.last_search, "jmck")
        self.assertEqual(graph.last_limit, 8)

    async def test_directory_search_deduplicates_graph_and_local_match_by_email(self) -> None:
        graph = _DirectoryGraphStub(
            [
                DirectoryUserRef(
                    id="oid-chief-1",
                    display_name="Jamie McKinney",
                    email="jmckinney@cwa3106.com",
                    user_principal_name="jmckinney@cwa3106.com",
                )
            ]
        )
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db, graph=graph),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        await create_chief_steward_assignment(
            ChiefStewardAssignmentCreateRequest(
                principal_email="jmckinney@cwa3106.com",
                principal_display_name="Local Jamie",
                contract_scope="mobility",
            ),
            admin_request,
        )

        response = await officer_directory_users(admin_request, search="jmck", limit=8)

        self.assertEqual(response.count, 1)
        self.assertEqual(response.rows[0].principal_id, "oid-chief-1")
        self.assertEqual(response.rows[0].email, "jmckinney@cwa3106.com")
        self.assertEqual(response.rows[0].match_source, "directory")

    async def test_directory_search_falls_back_to_local_known_people_when_graph_is_denied(self) -> None:
        graph = _FailingDirectoryGraphStub(
            'Graph request failed (GET /users): 403 {"error":{"code":"Authorization_RequestDenied","message":"Insufficient privileges to complete the operation."}}'
        )
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db, graph=graph),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        await create_chief_steward_assignment(
            ChiefStewardAssignmentCreateRequest(
                principal_email="chief.ui@example.org",
                principal_display_name="Chief UI",
                contract_scope="mobility",
            ),
            admin_request,
        )

        response = await officer_directory_users(admin_request, search="chief", limit=8)

        self.assertEqual(response.search, "chief")
        self.assertEqual(response.count, 1)
        self.assertEqual(response.rows[0].email, "chief.ui@example.org")
        self.assertEqual(response.rows[0].display_name, "Chief UI")
        self.assertIsNone(response.rows[0].principal_id)
        self.assertEqual(response.rows[0].match_source, "local")
        self.assertIn("User.Read.All", str(response.warning or ""))
        self.assertEqual(graph.last_search, "chief")
        self.assertEqual(graph.last_limit, 8)

    async def test_chief_steward_assignment_can_match_directory_principal_id(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026261",
            intake_request_id="forms-chief-id-1",
            contract="Mobility",
        )
        await self._insert_case(
            case_id="C2",
            grievance_id="2026262",
            intake_request_id="forms-chief-id-2",
            contract="Utilities",
        )
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        saved = await create_chief_steward_assignment(
            ChiefStewardAssignmentCreateRequest(
                principal_id="chief-user-oid",
                principal_email="old-chief-email@example.org",
                principal_display_name="Chief By Id",
                contract_scope="mobility",
            ),
            admin_request,
        )
        self.assertEqual(saved.principal_id, "chief-user-oid")

        chief_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user(
                "officer",
                email="new-chief-email@example.org",
                group_ids=("group-chief-stewards",),
                user_id="chief-user-oid",
            ),
            host="8.8.8.8",
        )

        cases = await officer_cases(chief_request)

        self.assertEqual(cases.viewer.role, "chief_steward")
        self.assertEqual(cases.viewer.contract_scopes, ["mobility"])
        self.assertEqual([row.case_id for row in cases.rows], ["C1"])

    async def test_chief_steward_can_bulk_edit_only_in_scope(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026301",
            intake_request_id="forms-bulk-1",
            contract="Mobility",
        )
        await self._insert_case(
            case_id="C2",
            grievance_id="2026302",
            intake_request_id="forms-bulk-2",
            contract="Utilities",
        )
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("chief_steward", email="chief@example.org", scopes=("mobility",)),
            host="8.8.8.8",
        )

        with self.assertRaises(HTTPException) as exc:
            await bulk_update_officer_cases(
                OfficerCaseBulkUpdateRequest(
                    case_ids=["C1", "C2"],
                    officer_status="closed",
                    officer_notes="Bulk close",
                ),
                request,
            )
        self.assertEqual(exc.exception.status_code, 403)

        result = await bulk_update_officer_cases(
            OfficerCaseBulkUpdateRequest(
                case_ids=["C1"],
                officer_status="open_at_national",
                officer_notes="Bulk close",
                third_level_request_sent_date="2026-03-29",
                fourth_level_request_sent_date="2026-03-31",
            ),
            request,
        )
        self.assertEqual(result.updated_case_count, 1)
        row = await self.db.fetchone(
            """
            SELECT officer_status, tracking_third_level_request_sent_date, tracking_fourth_level_request_sent_date
            FROM cases
            WHERE id=?
            """,
            ("C1",),
        )
        self.assertEqual(row[0], "open_at_national")
        self.assertEqual(row[1], "2026-03-29")
        self.assertEqual(row[2], "2026-03-31")
        event_row = await self.db.fetchone(
            "SELECT details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            ("C1",),
        )
        details = json.loads(event_row[0])
        self.assertTrue(details["bulk_update"])
        self.assertEqual(details["actor_role"], "chief_steward")

    async def test_officer_auto_data_request_uses_statement_fields_and_updates_case_status(self) -> None:
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json,
                 tracking_contract, tracking_steward, tracking_occurrence_date
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "C1",
                "2026301",
                "2026-03-27T15:00:00+00:00",
                "approved",
                "approved",
                None,
                "Taylor Jones",
                "taylor@example.org",
                "forms-auto-1",
                json.dumps(
                    {
                        "request_id": "forms-auto-1",
                        "contract": "AT&T Mobility",
                        "grievant_firstname": "Taylor",
                        "grievant_lastname": "Jones",
                        "grievant_email": "taylor@example.org",
                        "incident_date": "2026-03-20",
                        "supervisor": "Pat Supervisor",
                        "supervisor_email": "pat.supervisor@example.org",
                        "documents": [],
                        "template_data": {
                            "article": "Article 12",
                            "union_phone": "904-555-0100",
                        },
                    }
                ),
                "AT&T Mobility",
                "Steward Smith",
                "2026-03-20",
            ),
        )
        request = _Request(
            state=SimpleNamespace(
                cfg=self._cfg(auth_enabled=True),
                db=self.db,
                logger=SimpleNamespace(),
                docuseal=SimpleNamespace(),
                notifications=SimpleNamespace(),
            ),
            session=self._session_user("admin", email="officer@example.org"),
            host="8.8.8.8",
        )

        with patch(
            "grievance_api.web.routes_officers._ensure_officer_case_sharepoint_folder",
            new=AsyncMock(return_value=(None, None)),
        ), patch(
            "grievance_api.web.routes_officers._append_officer_case_document",
            new=AsyncMock(
                side_effect=[
                    OfficerAutoDataRequestDocument(
                        document_id="D1",
                        doc_type="data_request_letterhead",
                        status="uploaded",
                        generated_link="https://sharepoint.local/generated/D1.pdf",
                    ),
                    OfficerAutoDataRequestDocument(
                        document_id="D2",
                        doc_type="grievance_data_request_form",
                        status="sent_for_signature",
                        signing_link="https://docuseal.local/sign/D2",
                        generated_link="https://sharepoint.local/generated/D2.pdf",
                    ),
                ]
            ),
        ) as mock_append:
            response = await officer_auto_data_request("C1", request)

        payload = mock_append.await_args_list[0].kwargs["payload"]
        self.assertEqual(payload.grievance_id, "2026301")
        self.assertEqual(payload.grievance_number, "2026301")
        self.assertEqual(payload.template_data["articles"], "Article 12")
        self.assertEqual(payload.template_data["company_rep_name"], "Pat Supervisor")
        self.assertEqual(payload.template_data["company_rep_email"], "pat.supervisor@example.org")
        self.assertEqual(payload.template_data["union_phone"], "904-555-0100")
        self.assertEqual(payload.template_data["union_rep_name"], "officer@example.org")
        self.assertEqual(payload.template_data["signer_email"], "officer@example.org")
        self.assertEqual(response.documents[1].signing_link, "https://docuseal.local/sign/D2")

        case_status = await self.db.fetchone("SELECT status FROM cases WHERE id=?", ("C1",))
        self.assertEqual(case_status[0], "awaiting_signatures")

    async def test_admin_can_create_delete_bulk_delete_and_access_ops(self) -> None:
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        created = await create_officer_case(
            OfficerCaseCreateRequest(
                grievance_number="2026-100",
                contract="Mobility",
                member_name="Pat Member",
                member_email="pat@example.org",
                officer_assignee="Officer A",
                officer_status="open_at_state",
                third_level_request_sent_date="2026-03-28",
                fourth_level_request_sent_date="2026-03-30",
            ),
            request,
        )
        self.assertEqual(created.workflow_status, "manual_tracking")
        self.assertEqual(created.grievance_id, "2026-100")
        self.assertEqual(created.contract_scope, "mobility")
        self.assertEqual(created.officer_status, "open_at_state")
        self.assertEqual(created.third_level_request_sent_date, "2026-03-28")
        self.assertEqual(created.fourth_level_request_sent_date, "2026-03-30")

        page = await ops_page(request)
        self.assertIn("Grievance Ops", page)

        await self._insert_case(
            case_id="C2",
            grievance_id="2026402",
            intake_request_id="forms-admin-2",
            contract="Utilities",
        )
        delete_result = await bulk_delete_officer_cases(
            OfficerCaseBulkDeleteRequest(case_ids=[created.case_id, "C2"]),
            request,
        )
        self.assertEqual(delete_result.deleted_case_count, 2)
        remaining = await self.db.fetchall("SELECT id FROM cases ORDER BY id")
        self.assertEqual(remaining, [])

    async def test_manual_officer_case_creation_creates_sharepoint_folder_metadata(self) -> None:
        graph = _CaseFolderGraphStub()
        request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db, graph=graph),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        created = await create_officer_case(
            OfficerCaseCreateRequest(
                grievance_number="2026-101",
                contract="AT&T Mobility",
                member_name="Pat Member",
            ),
            request,
        )

        self.assertEqual(len(graph.ensure_calls), 1)
        self.assertEqual(graph.ensure_calls[0]["grievance_id"], "2026-101")
        self.assertEqual(graph.ensure_calls[0]["member_name"], "Pat Member - AT&T Mobility")

        case_row = await self.db.fetchone(
            "SELECT sharepoint_case_folder, sharepoint_case_web_url FROM cases WHERE id=?",
            (created.case_id,),
        )
        self.assertEqual(case_row[0], "2026-101 Pat Member - AT&T Mobility")
        self.assertEqual(case_row[1], "https://sharepoint.local/2026-101%20Pat%20Member%20-%20AT&T%20Mobility")

        event_rows = await self.db.fetchall(
            "SELECT event_type, details_json FROM events WHERE case_id=? ORDER BY id",
            (created.case_id,),
        )
        self.assertEqual(event_rows[0][0], "sharepoint_upload_target_resolved")
        self.assertEqual(event_rows[1][0], "officer_case_created")
        self.assertEqual(
            json.loads(event_rows[1][1])["sharepoint_case_folder"],
            "2026-101 Pat Member - AT&T Mobility",
        )

    async def test_admin_audit_endpoint_returns_case_events(self) -> None:
        await self._insert_case(
            case_id="C1",
            grievance_id="2026501",
            intake_request_id="forms-audit-1",
            contract="Mobility",
        )
        chief_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("chief_steward", email="chief@example.org", scopes=("mobility",)),
            host="8.8.8.8",
        )
        admin_request = _Request(
            state=SimpleNamespace(cfg=self._cfg(auth_enabled=True), db=self.db),
            session=self._session_user("admin", email="admin@example.org"),
            host="8.8.8.8",
        )

        await update_officer_case(
            "C1",
            OfficerCaseUpdateRequest(officer_notes="Audit me"),
            chief_request,
        )

        response = await officer_case_events("C1", admin_request)

        self.assertEqual(response.case_id, "C1")
        self.assertEqual(response.event_count, 1)
        self.assertEqual(response.events[0].event_type, "officer_case_updated")
        self.assertEqual(response.events[0].details["actor_email"], "chief@example.org")


if __name__ == "__main__":
    unittest.main()
