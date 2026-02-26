from __future__ import annotations

import unittest

from grievance_api.web.models import IntakeRequest


class IntakeRequestModelTests(unittest.TestCase):
    def test_contract_is_optional(self) -> None:
        payload = IntakeRequest(
            request_id="forms-123",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            narrative="test",
        )
        self.assertIsNone(payload.contract)

    def test_contract_still_accepted_when_present(self) -> None:
        payload = IntakeRequest(
            request_id="forms-456",
            contract="CWA",
            grievant_firstname="Jane",
            grievant_lastname="Doe",
            narrative="test",
        )
        self.assertEqual(payload.contract, "CWA")


if __name__ == "__main__":
    unittest.main()
