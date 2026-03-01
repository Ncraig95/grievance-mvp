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
        return _RenderedEmail(subject="Signature request", text_body="Body", html_body="<p>Body</p>")


class _FakeMailer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_mail(
        self,
        *,
        to_recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None,
        attachments,
        custom_headers: dict[str, str],
    ) -> _SentMail:
        self.calls.append(
            {
                "to_recipients": to_recipients,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "attachments": attachments,
                "custom_headers": custom_headers,
            }
        )
        return _SentMail(graph_message_id="g1", internet_message_id="i1")


class _FakeDb:
    def __init__(self) -> None:
        self.next_id = 1

    async def outbound_email_by_idempotency(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return None

    async def create_outbound_email(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        row_id = self.next_id
        self.next_id += 1
        return row_id

    async def mark_outbound_email_pending(self, **kwargs):  # noqa: ANN003
        _ = kwargs

    async def next_resend_count(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return 0

    async def fetchone(self, *args, **kwargs):  # noqa: ANN002, ANN003
        _ = (args, kwargs)
        return None

    async def mark_outbound_email_sent(self, **kwargs):  # noqa: ANN003
        _ = kwargs

    async def mark_outbound_email_failed(self, **kwargs):  # noqa: ANN003
        _ = kwargs

    async def add_event(self, *args, **kwargs):  # noqa: ANN002, ANN003
        _ = (args, kwargs)


class NotificationServiceFormTestModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_form_specific_test_mode_enables_banner_even_when_global_off(self) -> None:
        db = _FakeDb()
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
                test_mode_by_form={"settlement_form_3106": True},
            ),
        )

        await service.send_one(
            case_id="C1",
            document_id="D1",
            recipient_email="user@example.org",
            template_key="signature_request",
            context={},
            idempotency_key="idem-1",
            form_key="settlement_form_3106",
        )

        self.assertEqual(len(mailer.calls), 1)
        call = mailer.calls[0]
        self.assertTrue(str(call["subject"]).startswith("[TEST] "))
        self.assertIn("TEST MESSAGE", str(call["text_body"]))

    async def test_form_specific_test_mode_can_disable_banner_when_global_on(self) -> None:
        db = _FakeDb()
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
                test_mode=True,
                test_mode_by_form={"settlement_form_3106": False},
            ),
        )

        await service.send_one(
            case_id="C1",
            document_id="D1",
            recipient_email="user@example.org",
            template_key="signature_request",
            context={},
            idempotency_key="idem-2",
            form_key="settlement_form_3106",
        )

        self.assertEqual(len(mailer.calls), 1)
        call = mailer.calls[0]
        self.assertFalse(str(call["subject"]).startswith("[TEST] "))
        self.assertNotIn("TEST MESSAGE", str(call["text_body"]))


if __name__ == "__main__":
    unittest.main()
