from __future__ import annotations

import base64
import logging
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook
from fastapi.responses import HTMLResponse, RedirectResponse

from grievance_api.core.config import EmailConfig, OfficerAuthConfig, OutreachConfig
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services.graph_mail import SentGraphMail
from grievance_api.services.outreach_service import OutreachService
from grievance_api.web.outreach_models import OutreachImportInspectRequest, OutreachTestSendRequest
from grievance_api.web.routes_outreach import (
    outreach_bootstrap,
    outreach_inspect_contact_import,
    outreach_page,
    outreach_test_send,
    outreach_ui_page,
)


class _Request:
    def __init__(self, *, state, host: str = "127.0.0.1") -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host=host)
        self.url = SimpleNamespace(scheme="https", netloc=host, hostname=host.split(":", 1)[0])
        self.headers = {"host": host}
        self.session: dict[str, object] = {}
        self.query_params: dict[str, object] = {}


class _FakeMailer:
    def send_mime_mail(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return SentGraphMail(graph_message_id="graph-1", internet_message_id="internet-1")


class _FailingMailer:
    def __init__(self, message: str = "graph send denied") -> None:
        self.message = message

    def send_mime_mail(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        raise RuntimeError(self.message)


class OutreachRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "routes-outreach.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)
        self.email_cfg = EmailConfig(
            enabled=False,
            sender_user_id="",
            templates_dir=str(Path(self.tmpdir.name) / "templates"),
            internal_recipients=(),
            derek_email="grievances@example.org",
            approval_request_url_base="https://grievance.example.org",
            allow_signer_copy_link=False,
            artifact_delivery_mode="sharepoint_link",
            max_attachment_bytes=5_000_000,
            resend_cooldown_seconds=60,
            dry_run=False,
        )
        self.officer_auth_cfg = OfficerAuthConfig(
            enabled=False,
            redirect_uri="https://grievance.example.org/officers/login",
        )

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    def _service(self, *, mailer, enabled: bool = True) -> OutreachService:  # noqa: ANN001
        return OutreachService(
            db=self.db,
            logger=logging.getLogger("test.routes.outreach"),
            outreach_cfg=OutreachConfig(
                enabled=enabled,
                sender_user_id="organizing@members.example.org" if enabled else "",
                sender_display_name="CWA Local 3106",
                public_base_url="https://grievance.example.org",
                reply_to_address="replies@example.org",
                reply_to_name="CWA Local 3106",
                timezone="America/New_York",
                min_seconds_between_sends=0.0,
                max_parallel_sends=1,
                max_sends_per_run=50,
            ),
            email_cfg=self.email_cfg,
            officer_auth_cfg=self.officer_auth_cfg,
            mailer=mailer,
        )

    def _request(self, service: OutreachService) -> _Request:
        cfg = SimpleNamespace(officer_auth=self.officer_auth_cfg)
        state = SimpleNamespace(cfg=cfg, outreach=service)
        return _Request(state=state)

    @staticmethod
    def _csv_payload(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    @staticmethod
    def _xlsx_payload(rows: list[list[object]]) -> str:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Members"
        for row in rows:
            sheet.append(list(row))
        buf = BytesIO()
        workbook.save(buf)
        workbook.close()
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def test_bootstrap_includes_send_readiness(self):
        service = self._service(mailer=_FakeMailer(), enabled=True)
        await service.ensure_seed_data()
        request = self._request(service)

        response = await outreach_bootstrap(request)

        self.assertTrue(response.send_readiness.ready)
        self.assertEqual(response.send_readiness.sender_user_id, "organizing@members.example.org")
        self.assertTrue(len(response.placeholder_catalog) > 0)

    async def test_outreach_root_redirects_to_overview_page(self):
        service = self._service(mailer=None, enabled=False)
        request = self._request(service)

        response = await outreach_page(request)

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/officers/outreach/ui/overview")

    async def test_outreach_ui_page_renders_requested_section_heading(self):
        service = self._service(mailer=None, enabled=False)
        request = self._request(service)

        response = await outreach_ui_page("contacts", request)

        self.assertIsInstance(response, HTMLResponse)
        self.assertIn("Outreach Contacts", response.body.decode("utf-8"))
        self.assertIn('/officers/outreach/ui/compose', response.body.decode("utf-8"))

    async def test_inspect_contact_import_route_returns_sheet_preview(self):
        service = self._service(mailer=None, enabled=False)
        request = self._request(service)
        body = OutreachImportInspectRequest(
            filename="members.xlsx",
            content_base64=self._xlsx_payload(
                [
                    ["Email", "Status"],
                    ["member@example.org", "Member - Active - Active"],
                ]
            ),
            sheet_name="Members",
        )

        response = await outreach_inspect_contact_import(body, request)

        self.assertEqual(response.selected_sheet_name, "Members")
        self.assertEqual(response.headers, ["Email", "Status"])
        self.assertEqual(response.preview.imported_count, 1)
        self.assertEqual(response.preview.bucket_counts["Member - Active - Active"], 1)

    async def test_test_send_route_surfaces_error_text(self):
        service = self._service(mailer=_FailingMailer("mailbox lacks send-as permission"), enabled=True)
        await service.ensure_seed_data()
        contact = await service.save_contact(
            contact_id=None,
            payload={"email": "member@example.org", "first_name": "Jordan", "work_location": "Route Hall", "active": True},
        )
        template = await service.save_template(
            template_id=None,
            payload={
                "template_key": "route_notice",
                "name": "Route Notice",
                "template_type": "notice",
                "subject_template": "Route Notice",
                "body_template": "Hello {{ first_name }}.",
                "active": True,
            },
        )
        stop = await service.save_stop(
            stop_id=None,
            payload={
                "location_name": "Route Hall",
                "visit_date_local": "2026-06-09",
                "start_time_local": "11:00",
                "end_time_local": "12:00",
                "timezone": "America/New_York",
                "audience_location": "Route Hall",
                "notice_subject": "Route Notice",
                "reminder_subject": "Route Reminder",
                "status": "draft",
            },
        )
        request = self._request(service)
        body = OutreachTestSendRequest(
            template_id=int(template["id"]),
            stop_id=int(stop["id"]),
            contact_id=int(contact["id"]),
            recipient_email=contact["email"],
        )

        response = await outreach_test_send(body, request)

        self.assertEqual(response.status, "failed")
        self.assertEqual(response.error_text, "mailbox lacks send-as permission")


if __name__ == "__main__":
    unittest.main()
