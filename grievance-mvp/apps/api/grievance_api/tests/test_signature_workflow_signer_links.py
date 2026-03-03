from __future__ import annotations

import logging
from types import SimpleNamespace
import sys
import types
import unittest

from grievance_api.services.docuseal_client import DocuSealSubmission

if "aiosqlite" not in sys.modules:
    sys.modules["aiosqlite"] = types.SimpleNamespace(connect=None)
if "msal" not in sys.modules:
    sys.modules["msal"] = types.SimpleNamespace(ConfidentialClientApplication=object)

from grievance_api.services.signature_workflow import send_document_for_signature


class _FakeDb:
    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, tuple]] = []
        self.events: list[tuple[str, str | None, str, dict]] = []

    async def exec(self, sql: str, params: tuple) -> None:
        self.exec_calls.append((sql, params))

    async def add_event(self, case_id: str, document_id: str | None, event_type: str, details: dict) -> None:
        self.events.append((case_id, document_id, event_type, details))


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_one(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SimpleNamespace(status="sent")


class _FakeDocuSeal:
    def __init__(
        self,
        *,
        per_signer_links: dict[str, str],
        fallback_link: str | None = None,
        fetched_links: dict[str, str] | None = None,
    ) -> None:
        self.per_signer_links = per_signer_links
        self.fallback_link = fallback_link
        self.fetched_links = fetched_links or {}
        self.create_calls: list[dict] = []

    def create_submission(self, **kwargs):  # noqa: ANN003
        self.create_calls.append(kwargs)
        raw_submitters = [
            {"email": email, "url": url}
            for email, url in self.per_signer_links.items()
        ]
        return DocuSealSubmission(
            submission_id="sub-1",
            signing_link=self.fallback_link,
            template_id="tpl-1",
            raw={"id": "sub-1", "submitters": raw_submitters},
        )

    def extract_signing_links_by_email(self, submission: dict) -> dict[str, str]:
        _ = submission
        return dict(self.per_signer_links)

    def fetch_signing_links_by_email(self, *, submission_id: str) -> dict[str, str]:
        _ = submission_id
        return dict(self.fetched_links)


class SignatureWorkflowSignerLinksTests(unittest.IsolatedAsyncioTestCase):
    async def test_signature_request_emails_use_per_signer_links(self) -> None:
        db = _FakeDb()
        notifications = _FakeNotifications()
        docuseal = _FakeDocuSeal(
            per_signer_links={
                "manager@example.org": "https://docuseal.local/s/manager",
                "steward@example.org": "https://docuseal.local/s/steward",
                "grievant@example.org": "https://docuseal.local/s/grievant",
            },
            fallback_link="https://docuseal.local/s/fallback",
        )
        cfg = SimpleNamespace(
            email=SimpleNamespace(enabled=True),
            docuseal=SimpleNamespace(template_ids={}, strict_template_ids=False, default_template_id=None),
        )

        out = await send_document_for_signature(
            cfg=cfg,
            db=db,
            logger=logging.getLogger("test.signature_workflow"),
            docuseal=docuseal,
            notifications=notifications,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="settlement_form_3106",
            template_key="settlement_form_3106",
            pdf_bytes=b"%PDF",
            alignment_pdf_bytes=None,
            signer_order=["manager@example.org", "steward@example.org", "grievant@example.org"],
            correlation_id="C1",
            idempotency_prefix="intake:C1:D1",
        )

        self.assertEqual(out.status, "sent_for_signature")
        self.assertEqual(len(notifications.calls), 3)
        by_recipient = {str(c["recipient_email"]).lower(): c for c in notifications.calls}
        self.assertEqual(
            by_recipient["manager@example.org"]["context"]["docuseal_signing_url"],
            "https://docuseal.local/s/manager",
        )
        self.assertEqual(
            by_recipient["steward@example.org"]["context"]["docuseal_signing_url"],
            "https://docuseal.local/s/steward",
        )
        self.assertEqual(
            by_recipient["grievant@example.org"]["context"]["docuseal_signing_url"],
            "https://docuseal.local/s/grievant",
        )

    async def test_signature_request_emails_fallback_to_shared_link(self) -> None:
        db = _FakeDb()
        notifications = _FakeNotifications()
        docuseal = _FakeDocuSeal(
            per_signer_links={},
            fallback_link="https://docuseal.local/s/fallback",
        )
        cfg = SimpleNamespace(
            email=SimpleNamespace(enabled=True),
            docuseal=SimpleNamespace(template_ids={}, strict_template_ids=False, default_template_id=None),
        )

        out = await send_document_for_signature(
            cfg=cfg,
            db=db,
            logger=logging.getLogger("test.signature_workflow"),
            docuseal=docuseal,
            notifications=notifications,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="settlement_form_3106",
            template_key="settlement_form_3106",
            pdf_bytes=b"%PDF",
            alignment_pdf_bytes=None,
            signer_order=["manager@example.org", "steward@example.org", "grievant@example.org"],
            correlation_id="C1",
            idempotency_prefix="intake:C1:D1",
        )

        self.assertEqual(out.status, "sent_for_signature")
        self.assertEqual(len(notifications.calls), 3)
        for call in notifications.calls:
            self.assertEqual(call["context"]["docuseal_signing_url"], "https://docuseal.local/s/fallback")

    async def test_signature_request_emails_use_fetched_links_when_create_response_has_none(self) -> None:
        db = _FakeDb()
        notifications = _FakeNotifications()
        docuseal = _FakeDocuSeal(
            per_signer_links={},
            fetched_links={
                "manager@example.org": "https://docuseal.local/s/f-manager",
                "steward@example.org": "https://docuseal.local/s/f-steward",
                "grievant@example.org": "https://docuseal.local/s/f-grievant",
            },
            fallback_link="https://docuseal.local/s/fallback",
        )
        cfg = SimpleNamespace(
            email=SimpleNamespace(enabled=True),
            docuseal=SimpleNamespace(template_ids={}, strict_template_ids=False, default_template_id=None),
        )

        out = await send_document_for_signature(
            cfg=cfg,
            db=db,
            logger=logging.getLogger("test.signature_workflow"),
            docuseal=docuseal,
            notifications=notifications,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="settlement_form_3106",
            template_key="settlement_form_3106",
            pdf_bytes=b"%PDF",
            alignment_pdf_bytes=None,
            signer_order=["manager@example.org", "steward@example.org", "grievant@example.org"],
            correlation_id="C1",
            idempotency_prefix="intake:C1:D1",
        )

        self.assertEqual(out.status, "sent_for_signature")
        self.assertEqual(len(notifications.calls), 3)
        by_recipient = {str(c["recipient_email"]).lower(): c for c in notifications.calls}
        self.assertEqual(
            by_recipient["manager@example.org"]["context"]["docuseal_signing_url"],
            "https://docuseal.local/s/f-manager",
        )
        self.assertEqual(
            by_recipient["steward@example.org"]["context"]["docuseal_signing_url"],
            "https://docuseal.local/s/f-steward",
        )
        self.assertEqual(
            by_recipient["grievant@example.org"]["context"]["docuseal_signing_url"],
            "https://docuseal.local/s/f-grievant",
        )


if __name__ == "__main__":
    unittest.main()
