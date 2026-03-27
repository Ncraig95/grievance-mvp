from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.routes_ops import _load_standalone_trace, ops_resubmit_standalone


class _Request:
    def __init__(self, *, state) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host="127.0.0.1")


class OpsStandaloneTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_load_standalone_trace_returns_submission_documents_events_and_emails(self) -> None:
        await self.db.exec(
            """INSERT INTO standalone_submissions(
                 id, request_id, form_key, form_title, signer_email, status, created_at_utc,
                 template_data_json, filing_year, filing_sequence, filing_label,
                 sharepoint_folder_path, sharepoint_folder_web_url
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "S1",
                "forms-1",
                "att_mobility_bargaining_suggestion",
                "AT&T Mobility Bargaining Suggestion",
                "president@example.org",
                "completed",
                "2026-03-27T14:00:00+00:00",
                json.dumps({"local_number": "3106"}),
                2026,
                1,
                "Mobility Demand 1",
                "Mobility Demand Forms/2026/Mobility Demand 1",
                "https://sharepoint.local/folder",
            ),
        )
        await self.db.exec(
            """INSERT INTO standalone_documents(
                 id, submission_id, created_at_utc, form_key, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id, docuseal_signing_link,
                 sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url, completed_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "D1",
                "S1",
                "2026-03-27T14:00:01+00:00",
                "att_mobility_bargaining_suggestion",
                "att_mobility_bargaining_suggestion",
                "completed",
                1,
                json.dumps(["president@example.org"]),
                "ds-1",
                "https://docuseal.local/sign/1",
                "",
                "https://sharepoint.local/signed.pdf",
                "https://sharepoint.local/audit.zip",
                "2026-03-27T14:03:00+00:00",
            ),
        )
        await self.db.exec(
            """INSERT INTO standalone_events(
                 submission_id, document_id, ts_utc, event_type, details_json
               ) VALUES(?,?,?,?,?)""",
            (
                "S1",
                "D1",
                "2026-03-27T14:03:00+00:00",
                "submission_completed",
                json.dumps({"ok": True}),
            ),
        )
        await self.db.exec(
            """INSERT INTO standalone_outbound_emails(
                 submission_id, document_scope_id, template_key, recipient_email, idempotency_key,
                 status, graph_message_id, internet_message_id, resend_count,
                 created_at_utc, last_sent_at_utc, updated_at_utc, metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "S1",
                "D1",
                "standalone_signature_request",
                "president@example.org",
                "idem-1",
                "sent",
                "graph-1",
                "internet-1",
                0,
                "2026-03-27T14:00:02+00:00",
                "2026-03-27T14:00:02+00:00",
                "2026-03-27T14:00:02+00:00",
                "{}",
            ),
        )

        trace = await _load_standalone_trace(db=self.db, submission_id="S1")

        self.assertEqual(trace["submission"]["filing_label"], "Mobility Demand 1")
        self.assertEqual(trace["submission"]["sharepoint_folder_path"], "Mobility Demand Forms/2026/Mobility Demand 1")
        self.assertEqual(len(trace["documents"]), 1)
        self.assertEqual(trace["documents"][0]["docuseal_signing_link"], "https://docuseal.local/sign/1")
        self.assertEqual(trace["documents"][0]["sharepoint_signed_url"], "https://sharepoint.local/signed.pdf")
        self.assertEqual(len(trace["events"]), 1)
        self.assertEqual(trace["events"][0]["event_type"], "submission_completed")
        self.assertEqual(len(trace["outbound_emails"]), 1)
        self.assertEqual(trace["outbound_emails"][0]["template_key"], "standalone_signature_request")

    async def test_ops_resubmit_standalone_posts_to_standalone_endpoint(self) -> None:
        await self.db.exec(
            """INSERT INTO standalone_submissions(
                 id, request_id, form_key, form_title, signer_email, status, created_at_utc, template_data_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "S1",
                "forms-AT&T Mobility Bargaining Suggestion-1",
                "att_mobility_bargaining_suggestion",
                "AT&T Mobility Bargaining Suggestion",
                "president@example.org",
                "awaiting_signature",
                "2026-03-27T14:00:00+00:00",
                json.dumps({"local_number": "3106", "demand_text": "Demand"}),
            ),
        )

        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))
        response_payload = {
            "submission_id": "S2",
            "form_key": "att_mobility_bargaining_suggestion",
            "status": "awaiting_signature",
            "documents": [],
        }

        with patch("grievance_api.web.routes_ops.requests.post") as mock_post:
            mock_post.return_value = SimpleNamespace(status_code=200, text=json.dumps(response_payload))
            result = await ops_resubmit_standalone("S1", request)

        self.assertEqual(result["submission_id"], "S1")
        self.assertIn("-resubmit-", result["new_request_id"])
        self.assertEqual(result["standalone_response"]["submission_id"], "S2")

        called_url = mock_post.call_args.args[0]
        called_body = json.loads(mock_post.call_args.kwargs["data"].decode("utf-8"))
        called_headers = mock_post.call_args.kwargs["headers"]

        self.assertEqual(
            called_url,
            "http://127.0.0.1:8080/standalone/forms/att_mobility_bargaining_suggestion/submissions",
        )
        self.assertEqual(called_body["form_key"], "att_mobility_bargaining_suggestion")
        self.assertEqual(called_body["template_data"]["local_number"], "3106")
        self.assertEqual(called_body["local_president_signer_email"], "president@example.org")
        self.assertEqual(called_headers["X-Intake-Key"], "shared-secret")


if __name__ == "__main__":
    unittest.main()
