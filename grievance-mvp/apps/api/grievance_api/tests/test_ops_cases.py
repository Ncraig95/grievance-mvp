from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.routes_ops import (
    _load_active_signature_queue,
    _load_grievance_doc_catalog,
    ops_clear_document,
    ops_page,
    ops_update_document_email,
    ops_resubmit_by_grievance,
)


class _Request:
    def __init__(self, *, state) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.client = SimpleNamespace(host="127.0.0.1")


class _DocuSealStub:
    def __init__(self) -> None:
        self.deleted_submission_ids: list[str] = []
        self.updated_submitters: list[tuple[object, str | None, bool]] = []
        self.submitters_by_submission: dict[str, list[dict[str, object]]] = {}
        self.links_by_submission: dict[str, dict[str, str]] = {}

    def delete_submission(self, *, submission_id: str) -> dict[str, object]:
        self.deleted_submission_ids.append(submission_id)
        return {"ok": True, "already_missing": False, "status_code": 200, "response": {"deleted": True}}

    def list_submitters(self, *, submission_id: str) -> list[dict[str, object]]:
        return [dict(item) for item in self.submitters_by_submission.get(submission_id, [])]

    def update_submitter(self, *, submitter_id: str | int, email: str | None = None, send_email: bool = False) -> dict[str, object]:
        self.updated_submitters.append((submitter_id, email, send_email))
        for items in self.submitters_by_submission.values():
            for item in items:
                if str(item.get("id")) == str(submitter_id) and email:
                    item["email"] = email
        return {"id": submitter_id, "email": email, "send_email": send_email}

    def fetch_signing_links_by_email(self, *, submission_id: str) -> dict[str, str]:
        return dict(self.links_by_submission.get(submission_id, {}))


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

    async def test_load_active_signature_queue_returns_only_live_signature_documents(self) -> None:
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
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "D1",
                "C1",
                "2026-03-27T15:00:01+00:00",
                "settlement_form_3106",
                "settlement_form_3106",
                "sent_for_signature",
                1,
                json.dumps(["a@example.org", "b@example.org"]),
                "sub-1",
                "https://docuseal.local/s/1",
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
                "statement_of_occurrence",
                "statement_of_occurrence",
                "ops_cleared",
                1,
                json.dumps(["nick@example.org"]),
                "sub-2",
            ),
        )

        queue = await _load_active_signature_queue(db=self.db)

        self.assertEqual(queue["total_count"], 1)
        self.assertEqual(queue["case_document_count"], 1)
        self.assertEqual(queue["standalone_document_count"], 0)
        self.assertEqual(queue["case_documents"][0]["document_id"], "D1")
        self.assertEqual(queue["case_documents"][0]["doc_type"], "settlement_form_3106")

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

    async def test_ops_clear_document_deletes_docuseal_submission_and_retires_case_document(self) -> None:
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
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "D1",
                "C1",
                "2026-03-27T15:00:01+00:00",
                "settlement_form_3106",
                "settlement_form_3106",
                "sent_for_signature",
                1,
                json.dumps(["a@example.org", "b@example.org"]),
                "sub-1",
                "https://docuseal.local/s/1",
            ),
        )
        docuseal = _DocuSealStub()
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db, docuseal=docuseal))

        result = await ops_clear_document("D1", request, reason="testing cleanup")

        self.assertEqual(result["document_status"], "ops_cleared")
        self.assertEqual(result["case_status"], "ops_cleared")
        self.assertEqual(docuseal.deleted_submission_ids, ["sub-1"])

        document_row = await self.db.fetchone("SELECT status, docuseal_signing_link FROM documents WHERE id=?", ("D1",))
        case_row = await self.db.fetchone("SELECT status FROM cases WHERE id=?", ("C1",))
        event_row = await self.db.fetchone(
            "SELECT event_type, details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            ("C1",),
        )

        self.assertEqual(document_row[0], "ops_cleared")
        self.assertIsNone(document_row[1])
        self.assertEqual(case_row[0], "ops_cleared")
        self.assertEqual(event_row[0], "ops_document_cleared")
        self.assertEqual(json.loads(event_row[1])["reason"], "testing cleanup")

    async def test_ops_page_uses_data_action_buttons_for_active_queue(self) -> None:
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db))

        html = await ops_page(request)

        self.assertIn('data-action="clear-case-document"', html)
        self.assertIn('data-action="clear-standalone-document"', html)
        self.assertIn("activeQueueBody.addEventListener('click'", html)
        self.assertNotIn('onclick="clearCaseDocument(', html)
        self.assertNotIn('onclick="clearStandaloneDocument(', html)

    async def test_ops_update_document_email_updates_docuseal_and_local_signers(self) -> None:
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
                json.dumps(
                    {
                        "request_id": "forms-1",
                        "documents": [
                            {
                                "doc_type": "settlement_form_3106",
                                "template_key": "settlement_form_3106",
                                "signers": ["wrong@example.org", "union@example.org"],
                            }
                        ],
                    }
                ),
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, signer_order_json, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "D1",
                "C1",
                "2026-03-27T15:00:01+00:00",
                "settlement_form_3106",
                "settlement_form_3106",
                "sent_for_signature",
                1,
                json.dumps(["wrong@example.org", "union@example.org"]),
                "sub-1",
                "https://docuseal.local/s/old",
            ),
        )
        docuseal = _DocuSealStub()
        docuseal.submitters_by_submission["sub-1"] = [
            {"id": 11, "email": "wrong@example.org", "status": "opened"},
            {"id": 12, "email": "union@example.org", "status": "sent"},
        ]
        docuseal.links_by_submission["sub-1"] = {
            "correct@example.org": "https://docuseal.local/s/correct",
            "union@example.org": "https://docuseal.local/s/union",
        }
        request = _Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db, docuseal=docuseal))

        result = await ops_update_document_email(
            "D1",
            request,
            current_email="wrong@example.org",
            new_email="correct@example.org",
            resend_email=True,
        )

        self.assertEqual(result["previous_email"], "wrong@example.org")
        self.assertEqual(result["new_email"], "correct@example.org")
        self.assertEqual(docuseal.updated_submitters, [(11, "correct@example.org", True)])

        document_row = await self.db.fetchone(
            "SELECT signer_order_json, docuseal_signing_link FROM documents WHERE id=?",
            ("D1",),
        )
        case_row = await self.db.fetchone("SELECT intake_payload_json FROM cases WHERE id=?", ("C1",))
        event_row = await self.db.fetchone(
            "SELECT event_type, details_json FROM events WHERE case_id=? ORDER BY id DESC LIMIT 1",
            ("C1",),
        )

        self.assertEqual(json.loads(document_row[0]), ["correct@example.org", "union@example.org"])
        self.assertEqual(document_row[1], "https://docuseal.local/s/correct")
        self.assertEqual(
            json.loads(case_row[0])["documents"][0]["signers"],
            ["correct@example.org", "union@example.org"],
        )
        self.assertEqual(event_row[0], "ops_signer_email_updated")
        self.assertEqual(json.loads(event_row[1])["new_email"], "correct@example.org")


if __name__ == "__main__":
    unittest.main()
