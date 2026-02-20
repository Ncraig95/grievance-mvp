from __future__ import annotations

import hashlib
import hmac
import unittest

from grievance_api.web.routes_webhook import verify_docuseal_webhook


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


if __name__ == "__main__":
    unittest.main()
