from __future__ import annotations

import hashlib
import hmac
import unittest

from grievance_api.web.routes_webhook import _build_receipt_key, verify_docuseal_webhook


class DocuSealWebhookAuthTests(unittest.TestCase):
    def test_missing_secret_disables_verification(self) -> None:
        verify_docuseal_webhook(b"{}", {}, "")

    def test_hmac_signature_header_is_accepted(self) -> None:
        secret = "test-secret"
        body = b'{"event_type":"submission.completed","data":{"id":123}}'
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        verify_docuseal_webhook(body, {"X-DocuSeal-Signature": sig}, secret)

    def test_static_token_header_is_accepted(self) -> None:
        secret = "webhook-token"
        body = b'{"event_type":"submission.completed"}'

        verify_docuseal_webhook(body, {"X-Webhook-Token": secret}, secret)

    def test_bearer_token_is_accepted(self) -> None:
        secret = "bearer-token"
        body = b'{"event_type":"submission.completed"}'

        verify_docuseal_webhook(body, {"Authorization": f"Bearer {secret}"}, secret)

    def test_invalid_token_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            verify_docuseal_webhook(b"{}", {"X-Webhook-Token": "wrong"}, "expected")

    def test_receipt_key_uses_event_id_when_present(self) -> None:
        payload = {"event_id": "abc123", "event_type": "submission.completed", "data": {"id": 42}}
        key = _build_receipt_key(payload, b'{"event_id":"abc123"}', "42")
        self.assertEqual(key, "event:abc123")

    def test_receipt_key_does_not_collapse_distinct_events_on_same_submission(self) -> None:
        viewed_body = b'{"event_type":"form.viewed","data":{"id":33}}'
        completed_body = b'{"event_type":"submission.completed","data":{"id":33}}'
        viewed_key = _build_receipt_key({"event_type": "form.viewed", "data": {"id": 33}}, viewed_body, "33")
        completed_key = _build_receipt_key(
            {"event_type": "submission.completed", "data": {"id": 33}},
            completed_body,
            "33",
        )
        self.assertNotEqual(viewed_key, completed_key)


if __name__ == "__main__":
    unittest.main()
