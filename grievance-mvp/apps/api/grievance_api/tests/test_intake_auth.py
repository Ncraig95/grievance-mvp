from __future__ import annotations

import unittest

from fastapi import HTTPException

from grievance_api.core.config import IntakeAuthConfig
from grievance_api.core.intake_auth import validate_intake_auth_config, verify_intake_headers


class IntakeAuthTests(unittest.TestCase):
    def test_no_auth_configured_allows_request(self) -> None:
        cfg = IntakeAuthConfig(
            shared_header_name="X-Intake-Key",
            shared_header_value="",
            cloudflare_access_client_id="",
            cloudflare_access_client_secret="",
        )
        verify_intake_headers({}, cfg)

    def test_shared_header_required_when_configured(self) -> None:
        cfg = IntakeAuthConfig(
            shared_header_name="X-Intake-Key",
            shared_header_value="secret-value",
            cloudflare_access_client_id="",
            cloudflare_access_client_secret="",
        )
        with self.assertRaises(HTTPException):
            verify_intake_headers({}, cfg)
        verify_intake_headers({"X-Intake-Key": "secret-value"}, cfg)

    def test_cloudflare_service_token_required_when_configured(self) -> None:
        cfg = IntakeAuthConfig(
            shared_header_name="X-Intake-Key",
            shared_header_value="",
            cloudflare_access_client_id="cf-id",
            cloudflare_access_client_secret="cf-secret",
        )
        with self.assertRaises(HTTPException):
            verify_intake_headers({}, cfg)
        verify_intake_headers(
            {
                "CF-Access-Client-Id": "cf-id",
                "CF-Access-Client-Secret": "cf-secret",
            },
            cfg,
        )

    def test_both_checks_apply_when_both_configured(self) -> None:
        cfg = IntakeAuthConfig(
            shared_header_name="X-Intake-Key",
            shared_header_value="shared",
            cloudflare_access_client_id="cf-id",
            cloudflare_access_client_secret="cf-secret",
        )
        with self.assertRaises(HTTPException):
            verify_intake_headers(
                {"X-Intake-Key": "shared"},
                cfg,
            )
        verify_intake_headers(
            {
                "X-Intake-Key": "shared",
                "CF-Access-Client-Id": "cf-id",
                "CF-Access-Client-Secret": "cf-secret",
            },
            cfg,
        )

    def test_validate_rejects_partial_cloudflare_config(self) -> None:
        cfg = IntakeAuthConfig(
            shared_header_name="X-Intake-Key",
            shared_header_value="",
            cloudflare_access_client_id="cf-id",
            cloudflare_access_client_secret="",
        )
        with self.assertRaises(RuntimeError):
            validate_intake_auth_config(cfg)


if __name__ == "__main__":
    unittest.main()
