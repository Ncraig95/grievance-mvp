from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from grievance_api.web.models import ResendNotificationRequest
from grievance_api.web.routes_notifications import resend_notification
from grievance_api.web.routes_standalone import resend_standalone_notification


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_one(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            recipient_email=str(kwargs.get("recipient_email") or ""),
            status="sent",
            graph_message_id="g1",
            internet_message_id="i1",
            resend_count=0,
            deduped=False,
        )


class _FakeCaseDb:
    def __init__(self, *, signed_pdf_path: str) -> None:
        self.signed_pdf_path = signed_pdf_path

    async def fetchone(self, sql: str, params: tuple = ()):  # noqa: ANN001
        if "FROM cases WHERE id=?" in sql:
            return ("2026015", None, "Nick Craig", "nick@example.org", "approved", "{}")
        if "FROM documents WHERE id=? AND case_id=?" in sql:
            return (
                "settlement_form_3106",
                "settlement_form_3106",
                "submission-1",
                "https://docuseal.local/s/1",
                "https://sharepoint.local/signed.pdf",
                '["nick@example.org"]',
                self.signed_pdf_path,
            )
        raise AssertionError(f"Unexpected SQL: {sql!r} params={params!r}")


class _FakeStandaloneDb:
    def __init__(self, *, signed_pdf_path: str) -> None:
        self.signed_pdf_path = signed_pdf_path

    async def fetchone(self, sql: str, params: tuple = ()):  # noqa: ANN001
        if "FROM standalone_submissions" in sql:
            return (
                "att_mobility_bargaining_suggestion",
                "AT&T Mobility Bargaining Suggestion",
                "president@example.org",
                "completed",
            )
        if "FROM standalone_documents" in sql:
            return (
                "https://docuseal.local/s/2",
                "https://sharepoint.local/standalone-signed.pdf",
                '["president@example.org"]',
                self.signed_pdf_path,
            )
        raise AssertionError(f"Unexpected SQL: {sql!r} params={params!r}")


class _Request:
    def __init__(self, *, state) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)


class CompletionAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    def _cfg(self):
        return SimpleNamespace(
            email=SimpleNamespace(
                internal_recipients=("staff@example.org",),
                derek_email="approver@example.org",
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                approval_request_url_base="https://approve.local/cases",
            ),
        )

    async def test_case_completion_internal_resend_attaches_signed_pdf_even_when_delivery_mode_is_link(self) -> None:
        signed_path = Path(self.tmpdir.name) / "case-signed.pdf"
        signed_path.write_bytes(b"%PDF-1.4\n% signed case pdf\n")
        notifications = _FakeNotifications()
        state = SimpleNamespace(
            db=_FakeCaseDb(signed_pdf_path=str(signed_path)),
            cfg=self._cfg(),
            logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
            notifications=notifications,
        )
        request = _Request(state=state)
        body = ResendNotificationRequest(
            template_key="completion_internal",
            idempotency_key="idem-1",
            document_id="D1",
        )

        result = await resend_notification("C1", body, request)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(notifications.calls), 1)
        attachments = notifications.calls[0]["attachments"]
        self.assertIsNotNone(attachments)
        self.assertEqual(attachments[0].filename, "settlement_form_3106_signed.pdf")

    async def test_standalone_completion_internal_resend_attaches_signed_pdf_even_when_delivery_mode_is_link(self) -> None:
        signed_path = Path(self.tmpdir.name) / "standalone-signed.pdf"
        signed_path.write_bytes(b"%PDF-1.4\n% signed standalone pdf\n")
        notifications = _FakeNotifications()
        state = SimpleNamespace(
            db=_FakeStandaloneDb(signed_pdf_path=str(signed_path)),
            cfg=self._cfg(),
            logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
            notifications=notifications,
        )
        request = _Request(state=state)
        body = ResendNotificationRequest(
            template_key="completion_internal",
            idempotency_key="idem-2",
            document_id="D2",
        )

        result = await resend_standalone_notification("S1", body, request)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(notifications.calls), 1)
        attachments = notifications.calls[0]["attachments"]
        self.assertIsNotNone(attachments)
        self.assertEqual(attachments[0].filename, "att_mobility_bargaining_suggestion_signed.pdf")


if __name__ == "__main__":
    unittest.main()
