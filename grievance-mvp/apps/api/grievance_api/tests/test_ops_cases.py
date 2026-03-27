from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.routes_ops import _load_grievance_doc_catalog, ops_resubmit_by_grievance


class _Request:
    def __init__(self, *, state) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host="127.0.0.1")


class OpsCaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    @staticmethod
    def _cfg():
        return SimpleNamespace(
            intake_auth=SimpleNamespace(
                shared_header_name="X-Intake-Key",
                shared_header_value="shared-secret",
                cloudflare_access_client_id="",
                cloudflare_access_client_secret="",
            ),
            hmac_shared_secret="",
        )

    async def test_load_grievance_doc_catalog_groups_cases_and_doc_types(self) -> None:
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
                json.dumps({"request_id": "forms-1", "documents": []}),
            ),
        )
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, grievance_number,
                 member_name, member_email, intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "C2",
                "OTHER-ID",
                "2026-03-27T16:00:00+00:00",
                "approved",
                "approved",
                "2026015",
                "Nick Craig",
                "nick@example.org",
                "forms-2",
                json.dumps({"request_id": "forms-2", "documents": []}),
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "D1",
                "C1",
                "2026-03-27T15:00:01+00:00",
                "statement_of_occurrence",
                "statement_of_occurrence",
                "sent_for_signature",
                1,
                json.dumps(["nick@example.org"]),
                "sub-1",
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "D2",
                "C2",
                "2026-03-27T16:00:01+00:00",
                "settlement_form_3106",
                "settlement_form_3106",
                "approved",
                1,
                json.dumps(["a@example.org", "b@example.org"]),
                "sub-2",
            ),
        )

        catalog = await _load_grievance_doc_catalog(db=self.db, grievance_ref="2026015")

        self.assertEqual(catalog["grievance_ref"], "2026015")
        self.assertEqual(catalog["case_count"], 2)
        self.assertEqual(catalog["doc_type_count"], 2)
        self.assertEqual(catalog["cases"][0]["case_id"], "C2")
        self.assertEqual(catalog["cases"][0]["match_fields"], ["grievance_number"])
        self.assertEqual(catalog["cases"][1]["match_fields"], ["grievance_id"])

        summary = {item["doc_type"]: item for item in catalog["doc_types"]}
        self.assertEqual(summary["settlement_form_3106"]["latest_case_id"], "C2")
        self.assertEqual(summary["settlement_form_3106"]["document_count"], 1)
        self.assertEqual(summary["statement_of_occurrence"]["latest_case_id"], "C1")

    async def test_ops_resubmit_by_grievance_filters_payload_to_selected_doc_type(self) -> None:
        stored_payload = {
            "request_id": "forms-1",
            "grievance_id": "2026015",
            "contract": "CWA",
            "grievant_firstname": "Nick",
            "grievant_lastname": "Craig",
            "grievant_email": "nick@example.org",
            "narrative": "test",
            "template_data": {"issue_text": "x"},
            "documents": [
                {
                    "doc_type": "statement_of_occurrence",
                    "template_key": "statement_of_occurrence",
                    "requires_signature": True,
                    "signers": ["nick@example.org"],
                },
                {
                    "doc_type": "settlement_form_3106",
                    "template_key": "settlement_form_3106",
                    "requires_signature": True,
                    "signers": ["a@example.org", "b@example.org"],
                },
            ],
        }
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
                json.dumps(stored_payload),
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "D1",
                "C1",
                "2026-03-27T15:00:01+00:00",
                "statement_of_occurrence",
                "statement_of_occurrence",
                "sent_for_signature",
                1,
                json.dumps(["nick@example.org"]),
                "sub-1",
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "D2",
                "C1",
                "2026-03-27T15:00:02+00:00",
                "settlement_form_3106",
                "settlement_form_3106",
                "sent_for_signature",
                1,
                json.dumps(["a@example.org", "b@example.org"]),
                "sub-2",
            ),
        )

        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))
        response_payload = {
            "case_id": "C2",
            "grievance_id": "2026015",
            "status": "awaiting_signatures",
            "documents": [],
        }

        with patch("grievance_api.web.routes_ops.requests.post") as mock_post:
            mock_post.return_value = SimpleNamespace(status_code=200, text=json.dumps(response_payload))
            result = await ops_resubmit_by_grievance("2026015", "settlement_form_3106", request)

        self.assertEqual(result["grievance_ref"], "2026015")
        self.assertEqual(result["source_case_id"], "C1")
        self.assertEqual(result["source_document_id"], "D2")
        self.assertEqual(result["resubmitted_document_count"], 1)
        self.assertEqual(result["intake_response"]["case_id"], "C2")
        self.assertIn("-resubmit-", result["new_request_id"])

        called_url = mock_post.call_args.args[0]
        called_body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
        called_headers = mock_post.call_args.kwargs["headers"]

        self.assertEqual(called_url, "http://127.0.0.1:8080/intake")
        self.assertEqual(len(called_body["documents"]), 1)
        self.assertEqual(called_body["documents"][0]["doc_type"], "settlement_form_3106")
        self.assertEqual(called_body["documents"][0]["signers"], ["a@example.org", "b@example.org"])
        self.assertNotIn("document_command", called_body)
        self.assertEqual(called_headers["X-Intake-Key"], "shared-secret")


if __name__ == "__main__":
    unittest.main()
