from __future__ import annotations

from dataclasses import dataclass
import logging
import unittest

from grievance_api.core.config import EmailConfig
from grievance_api.services.notification_service import NotificationService


@dataclass
class _RenderedEmail:
    subject: str
    text_body: str
    html_body: str | None


@dataclass
class _SentMail:
    graph_message_id: str
    internet_message_id: str


class _FakeTemplateStore:
    def render(self, template_key: str, context: dict[str, object]) -> _RenderedEmail:
        _ = (template_key, context)
        return _RenderedEmail(subject="Standalone", text_body="Body", html_body=None)


class _FakeMailer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_mail(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return _SentMail(graph_message_id="g1", internet_message_id="i1")


class _FakeStandaloneDb:
    def __init__(self) -> None:
        self.next_id = 1
        self.events: list[tuple[str, str | None, str]] = []

    async def standalone_outbound_email_by_idempotency(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return None

    async def create_standalone_outbound_email(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        row_id = self.next_id
        self.next_id += 1
        return row_id

    async def mark_standalone_outbound_email_pending(self, **kwargs):  # noqa: ANN003
        _ = kwargs

    async def next_standalone_resend_count(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return 0

    async def fetchone(self, *args, **kwargs):  # noqa: ANN002, ANN003
        _ = (args, kwargs)
        return None

    async def mark_standalone_outbound_email_sent(self, **kwargs):  # noqa: ANN003
        _ = kwargs

    async def mark_standalone_outbound_email_failed(self, **kwargs):  # noqa: ANN003
        _ = kwargs

    async def add_standalone_event(self, submission_id: str, document_id: str | None, event_type: str, details: dict) -> None:
        _ = details
        self.events.append((submission_id, document_id, event_type))


class NotificationServiceStandaloneTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_scope_uses_submission_headers_and_events(self) -> None:
        db = _FakeStandaloneDb()
        mailer = _FakeMailer()
        service = NotificationService(
            db=db,
            logger=logging.getLogger("test"),
            mailer=mailer,
            template_store=_FakeTemplateStore(),
            email_cfg=EmailConfig(
                enabled=True,
                sender_user_id="sender@example.org",
                templates_dir="/tmp",
                internal_recipients=(),
                derek_email=None,
                approval_request_url_base=None,
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=300,
                dry_run=False,
                test_mode=False,
                test_mode_by_form={},
            ),
        )

        await service.send_one(
            case_id="S1",
            document_id="D1",
            recipient_email="user@example.org",
            template_key="standalone_signature_request",
            context={},
            idempotency_key="idem-standalone",
            form_key="att_mobility_bargaining_suggestion",
            scope_kind="standalone",
        )

        self.assertEqual(len(mailer.calls), 1)
        headers = mailer.calls[0]["custom_headers"]
        self.assertEqual(headers["X-Submission-ID"], "S1")
        self.assertEqual(headers["X-Workflow-Kind"], "standalone")
        self.assertNotIn("X-Case-ID", headers)
        self.assertIn(("S1", "D1", "email_sent"), db.events)


if __name__ == "__main__":
    unittest.main()
