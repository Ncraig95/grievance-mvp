from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from grievance_api.core.config import DocumentPolicyConfig
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.routes_webhook import webhook_docuseal


class _Request:
    def __init__(self, *, state, body: bytes = b"", headers: dict[str, str] | None = None) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self._body = body
        self.headers = headers or {}
        self.client = None

    async def body(self) -> bytes:
        return self._body


class _FakeStageDocuSeal:
    def __init__(self, *, stage1_status: str = "completed", include_signed_pdf: bool = True) -> None:
        self.stage1_status = stage1_status
        self.include_signed_pdf = include_signed_pdf
        self.create_calls: list[dict[str, object]] = []

    @staticmethod
    def _signed_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("signed.pdf", b"%PDF-1.4\n% stage 1 signed pdf\n")
        return buf.getvalue()

    def download_completed_artifacts(self, *, submission_id: str) -> dict[str, object]:
        _ = submission_id
        return {
            "completed_zip_bytes": self._signed_zip() if self.include_signed_pdf else None,
            "submission": {"submitters": [{"email": "union@example.org", "status": self.stage1_status}]},
        }

    def list_submitters(self, *, submission_id: str) -> list[dict[str, object]]:
        _ = submission_id
        return [{"email": "union@example.org", "status": self.stage1_status}]

    def create_submission(self, **kwargs):  # noqa: ANN003
        self.create_calls.append(dict(kwargs))
        return SimpleNamespace(submission_id="stage2-submission", signing_link="https://sign.local/stage2", raw={})

    def extract_signing_links_by_email(self, raw: dict) -> dict[str, str]:
        _ = raw
        return {}

    def fetch_signing_links_by_email(self, submission_id: str) -> dict[str, str]:
        _ = submission_id
        return {}


class _FakeNotifications:
    async def send_one(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        return SimpleNamespace(status="sent")


class StagedWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        self.data_root = str(Path(self.tmpdir.name) / "data")
        migrate(self.db_path)
        self.db = Db(self.db_path)

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    def _cfg(self):
        return SimpleNamespace(
            data_root=self.data_root,
            document_policies={
                "bst_grievance_form_3g3a": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="",
                    default_requires_signature=True,
                    staged_flow_enabled=True,
                    auto_advance=True,
                    store_all_stage_artifacts=True,
                    input_source="docuseal_fill_fields",
                )
            },
            docuseal=SimpleNamespace(
                webhook_secret="REPLACE",
                default_template_id=1,
                template_ids={},
                strict_template_ids=False,
            ),
            graph=SimpleNamespace(
                site_hostname="",
                site_path="",
                document_library="",
                case_parent_folder="Grievances",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
            ),
            email=SimpleNamespace(
                enabled=False,
                internal_recipients=(),
                allow_signer_copy_link=False,
                max_attachment_bytes=5_000_000,
                approval_request_url_base="https://approve.local/cases",
                derek_email="approver@example.org",
            ),
            require_approver_decision=False,
        )

    async def _insert_stage1_document(self) -> tuple[str, str, str]:
        case_id = "C-stage"
        document_id = "D-stage"
        submission_id = "stage1-submission"
        doc_dir = Path(self.data_root) / case_id / document_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = doc_dir / "generated.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% unsigned generated pdf\n")
        alignment_dir = doc_dir / "stage_alignments"
        alignment_dir.mkdir(parents=True, exist_ok=True)
        (alignment_dir / "stage2_alignment.pdf").write_bytes(b"%PDF-1.4\n% stage 2 alignment\n")

        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, member_name, member_email,
                 intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                "2026001",
                "2026-04-20T12:00:00+00:00",
                "awaiting_signatures",
                "approved",
                "Jane Worker",
                "jane@example.org",
                "forms-3g3a-test",
                json.dumps({"contract": "BellSouth", "incident_date": "2026-04-01"}),
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status, requires_signature,
                 signer_order_json, pdf_path, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                case_id,
                "2026-04-20T12:00:01+00:00",
                "bst_grievance_form_3g3a",
                "bst_grievance_form_3g3a",
                "sent_for_signature_stage1",
                1,
                json.dumps(["union@example.org", "manager@example.org", "final@example.org"]),
                str(pdf_path),
                submission_id,
                "https://sign.local/stage1",
            ),
        )
        stage_id = await self.db.create_document_stage(
            case_id=case_id,
            document_id=document_id,
            stage_no=1,
            stage_key="stage1_union",
            status="preparing",
            signer_email="union@example.org",
            source_payload={},
        )
        await self.db.update_document_stage_submission(
            stage_id=stage_id,
            status="sent_for_signature_stage1",
            submission_id=submission_id,
            signing_link="https://sign.local/stage1",
        )
        return case_id, document_id, submission_id

    async def _run_stage1_webhook(self, *, docuseal: _FakeStageDocuSeal) -> dict[str, object]:
        _, _, submission_id = await self._insert_stage1_document()
        state = SimpleNamespace(
            cfg=self._cfg(),
            db=self.db,
            logger=logging.getLogger("test"),
            graph=SimpleNamespace(),
            docuseal=docuseal,
            notifications=_FakeNotifications(),
        )
        payload = {"event": "submission.completed", "submission_id": submission_id}
        request = _Request(state=state, body=json.dumps(payload).encode("utf-8"), headers={})
        return await webhook_docuseal(request)

    async def _insert_regular_document(
        self,
        *,
        case_id: str = "C-regular",
        document_id: str = "D-regular",
        submission_id: str = "regular-submission",
        doc_type: str = "settlement_form_3106",
        template_key: str = "settlement_form_3106",
        officer_status: str | None = None,
        officer_closed_at_utc: str | None = None,
        officer_closed_by: str | None = None,
    ) -> tuple[str, str, str]:
        doc_dir = Path(self.data_root) / case_id / document_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = doc_dir / "generated.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% unsigned generated pdf\n")
        await self.db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status, member_name, member_email,
                 intake_request_id, intake_payload_json, officer_status, officer_closed_at_utc, officer_closed_by
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                "2026002",
                "2026-04-20T12:00:00+00:00",
                "awaiting_signatures",
                "approved",
                "Pat Member",
                "pat@example.org",
                f"forms-{case_id}",
                json.dumps({"contract": "AT&T Mobility", "incident_date": "2026-04-01"}),
                officer_status,
                officer_closed_at_utc,
                officer_closed_by,
            ),
        )
        await self.db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status, requires_signature,
                 signer_order_json, pdf_path, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                case_id,
                "2026-04-20T12:00:01+00:00",
                doc_type,
                template_key,
                "sent_for_signature",
                1,
                json.dumps(["pat@example.org"]),
                str(pdf_path),
                submission_id,
                "https://sign.local/regular",
            ),
        )
        return case_id, document_id, submission_id

    async def _run_regular_completion_webhook(
        self,
        *,
        case_id: str = "C-regular",
        document_id: str = "D-regular",
        submission_id: str = "regular-submission",
        doc_type: str = "settlement_form_3106",
        template_key: str = "settlement_form_3106",
        officer_status: str | None = None,
        officer_closed_at_utc: str | None = None,
        officer_closed_by: str | None = None,
    ) -> dict[str, object]:
        await self._insert_regular_document(
            case_id=case_id,
            document_id=document_id,
            submission_id=submission_id,
            doc_type=doc_type,
            template_key=template_key,
            officer_status=officer_status,
            officer_closed_at_utc=officer_closed_at_utc,
            officer_closed_by=officer_closed_by,
        )
        state = SimpleNamespace(
            cfg=self._cfg(),
            db=self.db,
            logger=logging.getLogger("test"),
            graph=SimpleNamespace(),
            docuseal=_FakeStageDocuSeal(stage1_status="completed", include_signed_pdf=True),
            notifications=_FakeNotifications(),
        )
        payload = {"event": "submission.completed", "submission_id": submission_id}
        request = _Request(state=state, body=json.dumps(payload).encode("utf-8"), headers={})
        return await webhook_docuseal(request)

    async def test_settlement_completion_auto_closes_blank_tracker_status(self) -> None:
        result = await self._run_regular_completion_webhook()

        self.assertTrue(result["handled"])
        row = await self.db.fetchone(
            "SELECT status, officer_status, officer_closed_at_utc, officer_closed_by FROM cases WHERE id=?",
            ("C-regular",),
        )
        self.assertEqual(row[0], "approved")
        self.assertEqual(row[1], "closed")
        self.assertTrue(str(row[2] or "").strip())
        self.assertEqual(row[3], "DocuSeal automation")
        event = await self.db.fetchone(
            "SELECT details_json FROM events WHERE event_type='settlement_tracker_auto_closed'"
        )
        details = json.loads(event[0])
        self.assertIsNone(details["previous_officer_status"])
        self.assertEqual(details["new_officer_status"], "closed")
        self.assertEqual(details["actor"], "docuseal")

    async def test_settlement_completion_auto_closes_escalated_tracker_status(self) -> None:
        await self._run_regular_completion_webhook(
            case_id="C-national",
            document_id="D-national",
            submission_id="regular-submission-national",
            officer_status="open_at_national",
        )

        row = await self.db.fetchone(
            "SELECT officer_status, officer_closed_by FROM cases WHERE id=?",
            ("C-national",),
        )
        self.assertEqual(row[0], "closed")
        self.assertEqual(row[1], "DocuSeal automation")
        event = await self.db.fetchone(
            "SELECT details_json FROM events WHERE event_type='settlement_tracker_auto_closed'"
        )
        details = json.loads(event[0])
        self.assertEqual(details["previous_officer_status"], "open_at_national")

    async def test_non_settlement_completion_does_not_close_tracker_status(self) -> None:
        await self._run_regular_completion_webhook(
            case_id="C-statement",
            document_id="D-statement",
            submission_id="regular-submission-statement",
            doc_type="statement_of_occurrence",
            template_key="statement_of_occurrence",
            officer_status="open",
        )

        row = await self.db.fetchone(
            "SELECT officer_status, officer_closed_at_utc, officer_closed_by FROM cases WHERE id=?",
            ("C-statement",),
        )
        self.assertEqual(row[0], "open")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        event = await self.db.fetchone(
            "SELECT id FROM events WHERE event_type='settlement_tracker_auto_closed'"
        )
        self.assertIsNone(event)

    async def test_settlement_completion_preserves_already_closed_tracker_metadata(self) -> None:
        await self._run_regular_completion_webhook(
            case_id="C-closed",
            document_id="D-closed",
            submission_id="regular-submission-closed",
            officer_status="closed",
            officer_closed_at_utc="2026-04-01T00:00:00+00:00",
            officer_closed_by="Manual Officer",
        )

        row = await self.db.fetchone(
            "SELECT officer_status, officer_closed_at_utc, officer_closed_by FROM cases WHERE id=?",
            ("C-closed",),
        )
        self.assertEqual(row[0], "closed")
        self.assertEqual(row[1], "2026-04-01T00:00:00+00:00")
        self.assertEqual(row[2], "Manual Officer")
        event = await self.db.fetchone(
            "SELECT id FROM events WHERE event_type='settlement_tracker_auto_closed'"
        )
        self.assertIsNone(event)

    async def test_stage_completion_does_not_advance_when_signer_not_completed(self) -> None:
        docuseal = _FakeStageDocuSeal(stage1_status="pending", include_signed_pdf=True)

        result = await self._run_stage1_webhook(docuseal=docuseal)

        self.assertFalse(result["handled"])
        self.assertEqual(docuseal.create_calls, [])
        stage1 = await self.db.get_document_stage(document_id="D-stage", stage_no=1)
        stage2 = await self.db.get_document_stage(document_id="D-stage", stage_no=2)
        self.assertEqual(stage1[5], "sent_for_signature_stage1")
        self.assertIsNone(stage2)
        event = await self.db.fetchone(
            "SELECT details_json FROM events WHERE event_type='document_stage_completion_deferred'"
        )
        self.assertIn("signer_not_completed", event[0])

    async def test_stage_completion_does_not_advance_without_signed_pdf(self) -> None:
        docuseal = _FakeStageDocuSeal(stage1_status="completed", include_signed_pdf=False)

        result = await self._run_stage1_webhook(docuseal=docuseal)

        self.assertFalse(result["handled"])
        self.assertEqual(docuseal.create_calls, [])
        stage1 = await self.db.get_document_stage(document_id="D-stage", stage_no=1)
        stage2 = await self.db.get_document_stage(document_id="D-stage", stage_no=2)
        self.assertEqual(stage1[5], "sent_for_signature_stage1")
        self.assertIsNone(stage2)
        event = await self.db.fetchone(
            "SELECT details_json FROM events WHERE event_type='document_stage_completion_deferred'"
        )
        self.assertIn("missing_signed_pdf", event[0])

    async def test_stage_completion_advances_with_completed_signer_and_signed_pdf(self) -> None:
        docuseal = _FakeStageDocuSeal(stage1_status="completed", include_signed_pdf=True)

        result = await self._run_stage1_webhook(docuseal=docuseal)

        self.assertTrue(result["handled"])
        self.assertEqual(len(docuseal.create_calls), 1)
        self.assertEqual(docuseal.create_calls[0]["signers"], ["manager@example.org"])
        self.assertIn(b"stage 1 signed pdf", docuseal.create_calls[0]["pdf_bytes"])
        stage1 = await self.db.get_document_stage(document_id="D-stage", stage_no=1)
        stage2 = await self.db.get_document_stage(document_id="D-stage", stage_no=2)
        self.assertEqual(stage1[5], "completed")
        self.assertEqual(stage2[5], "sent_for_signature_stage2")


if __name__ == "__main__":
    unittest.main()
