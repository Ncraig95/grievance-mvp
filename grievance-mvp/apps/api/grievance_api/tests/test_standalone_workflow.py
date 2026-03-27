from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grievance_api.core.config import StandaloneFormConfig, StandaloneSharepointStorageConfig
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.web.models import StandaloneSubmissionRequest
from grievance_api.web.routes_standalone import create_standalone_submission
from grievance_api.web.routes_webhook import webhook_docuseal


class _FakeDocuSealCreateSubmission:
    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    def create_submission(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return SimpleNamespace(submission_id="ds-submission-1", signing_link="https://sign.local/1", raw={})

    def extract_signing_links_by_email(self, raw: dict) -> dict[str, str]:
        _ = raw
        return {"president@example.org": "https://sign.local/1"}

    def fetch_signing_links_by_email(self, submission_id: str) -> dict[str, str]:
        _ = submission_id
        return {"president@example.org": "https://sign.local/1"}


class _FakeDocuSealWebhook:
    def __init__(self) -> None:
        self.zip_bytes = self._build_zip()

    @staticmethod
    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("signed.pdf", b"%PDF-1.4\n% standalone signed pdf\n")
        return buf.getvalue()

    def download_completed_artifacts(self, *, submission_id: str) -> dict[str, object]:
        _ = submission_id
        return {"completed_zip_bytes": self.zip_bytes, "submission": {}}


class _FakeGraphUploader:
    def __init__(self, *, fail_audit_once: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_audit_once = fail_audit_once

    def upload_local_file_to_folder_path(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        filename = str(kwargs.get("filename", ""))
        if self.fail_audit_once and "Audit" in filename:
            self.fail_audit_once = False
            raise RuntimeError("audit upload failed")
        folder_path = str(kwargs.get("folder_path", ""))
        return SimpleNamespace(
            web_url=f"https://sharepoint.local/{folder_path}/{filename}",
            path=f"{folder_path}/{filename}",
        )


class _FakeNotifications:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_one(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            recipient_email=str(kwargs.get("recipient_email") or ""),
            status="sent",
            graph_message_id="g1",
            internet_message_id="i1",
            resend_count=0,
            deduped=False,
        )


class _Request:
    def __init__(self, *, state, body: bytes = b"", headers: dict[str, str] | None = None) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self._body = body
        self.headers = headers or {}
        self.client = None

    async def body(self) -> bytes:
        return self._body


def _fake_render_docx(template_path: str, context: dict[str, object], out_path: str, **kwargs) -> None:  # noqa: ANN001, ANN003
    _ = (template_path, context, kwargs)
    Path(out_path).write_bytes(b"docx")


def _fake_docx_to_pdf(docx_path: str, out_dir: str, timeout_seconds: int, **kwargs) -> str:  # noqa: ANN001, ANN003
    _ = (timeout_seconds, kwargs)
    out = Path(out_dir) / f"{Path(docx_path).stem}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"%PDF-1.4\n% fake pdf\n")
    return str(out)


class StandaloneWorkflowTests(unittest.IsolatedAsyncioTestCase):
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
            rendering=SimpleNamespace(normalize_split_placeholders=True),
            libreoffice_timeout_seconds=45,
            docx_pdf_engine="libreoffice",
            docx_pdf_graph_temp_folder="_docx_pdf_convert",
            standalone_forms={
                "att_mobility_bargaining_suggestion": StandaloneFormConfig(
                    template_path="/tmp/template.docx",
                    form_label="AT&T Mobility Bargaining Suggestion",
                    sharepoint_folder_label="AT&T Mobility Bargaining Suggestion",
                    signer_count=1,
                    default_signer_email="president@example.org",
                    sharepoint_storage=StandaloneSharepointStorageConfig(
                        root_folder="Mobility Demand Forms",
                        label_prefix="Mobility Demand",
                        sequence_scope="yearly",
                        year_subfolders=True,
                        upload_generated=False,
                        upload_signed=True,
                        upload_audit=True,
                    ),
                )
            },
            graph=SimpleNamespace(
                site_hostname="",
                site_path="",
                document_library="",
                generated_subfolder="Generated",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
                standalone_parent_folder="Standalone Forms",
            ),
            grievance_id=SimpleNamespace(timezone="America/New_York"),
            docuseal=SimpleNamespace(default_template_id=1, template_ids={}, strict_template_ids=False),
            intake_auth=SimpleNamespace(
                shared_header_name="X-Intake-Key",
                shared_header_value="",
                cloudflare_access_client_id="",
                cloudflare_access_client_secret="",
            ),
            email=SimpleNamespace(
                enabled=False,
                allow_signer_copy_link=False,
                max_attachment_bytes=2_000_000,
                artifact_delivery_mode="sharepoint_link",
                internal_recipients=(),
            ),
        )

    async def test_create_standalone_submission_is_idempotent_by_request_id(self) -> None:
        docuseal = _FakeDocuSealCreateSubmission()
        graph = _FakeGraphUploader()
        cfg = self._cfg()
        cfg.graph.site_hostname = "contoso.sharepoint.com"
        cfg.graph.site_path = "/sites/Grievances"
        cfg.graph.document_library = "Documents"
        state = SimpleNamespace(
            cfg=cfg,
            db=self.db,
            logger=logging.getLogger("test"),
            graph=graph,
            docuseal=docuseal,
            notifications=SimpleNamespace(),
        )
        request = _Request(state=state)
        body = StandaloneSubmissionRequest(
            request_id="forms-123",
            form_key="att_mobility_bargaining_suggestion",
            template_data={"local_number": "3106", "demand_text": "Demand", "reason_text": "Reason"},
        )

        with patch("grievance_api.web.routes_standalone.render_docx", _fake_render_docx), patch(
            "grievance_api.web.routes_standalone.docx_to_pdf",
            _fake_docx_to_pdf,
        ):
            first = await create_standalone_submission("att_mobility_bargaining_suggestion", body, request)
            second = await create_standalone_submission("att_mobility_bargaining_suggestion", body, request)

        self.assertEqual(first.submission_id, second.submission_id)
        self.assertEqual(first.status, "awaiting_signature")
        self.assertEqual(docuseal.calls, 1)
        self.assertEqual(docuseal.last_kwargs["signers"], ["president@example.org"])
        self.assertEqual(first.documents[0].document_link, None)
        self.assertEqual(graph.calls, [])
        row = await self.db.fetchone("SELECT COUNT(1) FROM standalone_submissions")
        self.assertEqual(int(row[0]), 1)
        stored = await self.db.fetchone("SELECT signer_email FROM standalone_submissions WHERE id=?", (first.submission_id,))
        self.assertEqual(stored[0], "president@example.org")

    async def test_create_standalone_submission_requires_signer_email_when_no_default_is_configured(self) -> None:
        docuseal = _FakeDocuSealCreateSubmission()
        cfg = self._cfg()
        cfg.standalone_forms["att_mobility_bargaining_suggestion"] = StandaloneFormConfig(
            template_path="/tmp/template.docx",
            form_label="AT&T Mobility Bargaining Suggestion",
            sharepoint_folder_label="AT&T Mobility Bargaining Suggestion",
            signer_count=1,
            default_signer_email="",
            sharepoint_storage=StandaloneSharepointStorageConfig(
                root_folder="Mobility Demand Forms",
                label_prefix="Mobility Demand",
                sequence_scope="yearly",
                year_subfolders=True,
                upload_generated=False,
                upload_signed=True,
                upload_audit=True,
            ),
        )
        state = SimpleNamespace(
            cfg=cfg,
            db=self.db,
            logger=logging.getLogger("test"),
            graph=_FakeGraphUploader(),
            docuseal=docuseal,
            notifications=SimpleNamespace(),
        )
        request = _Request(state=state)
        body = StandaloneSubmissionRequest(
            request_id="forms-124",
            form_key="att_mobility_bargaining_suggestion",
            template_data={"local_number": "3106", "demand_text": "Demand", "reason_text": "Reason"},
        )

        with self.assertRaisesRegex(Exception, "default_signer_email"):
            await create_standalone_submission("att_mobility_bargaining_suggestion", body, request)

    async def test_webhook_marks_standalone_submission_completed(self) -> None:
        submission_id = "S202603270001"
        document_id = "D202603270001"
        pdf_dir = Path(self.data_root) / "standalone" / submission_id / document_id
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / "generated.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% generated pdf\n")

        await self.db.exec(
            """INSERT INTO standalone_submissions(
                 id, request_id, form_key, form_title, signer_email, status, created_at_utc, template_data_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                submission_id,
                "forms-456",
                "att_mobility_bargaining_suggestion",
                "AT&T Mobility Bargaining Suggestion",
                "president@example.org",
                "awaiting_signature",
                "2026-03-27T12:00:00+00:00",
                "{}",
            ),
        )
        await self.db.exec(
            """INSERT INTO standalone_documents(
                 id, submission_id, created_at_utc, form_key, template_key, status, requires_signature,
                 signer_order_json, pdf_path, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                submission_id,
                "2026-03-27T12:00:00+00:00",
                "att_mobility_bargaining_suggestion",
                "att_mobility_bargaining_suggestion",
                "awaiting_signature",
                1,
                json.dumps(["president@example.org"]),
                str(pdf_path),
                "ds-submission-2",
                "https://sign.local/2",
            ),
        )

        cfg = self._cfg()
        cfg.docuseal = SimpleNamespace(webhook_secret="REPLACE", default_template_id=1, template_ids={}, strict_template_ids=False)
        cfg.graph.site_hostname = "contoso.sharepoint.com"
        cfg.graph.site_path = "/sites/Grievances"
        cfg.graph.document_library = "Documents"
        graph = _FakeGraphUploader()
        state = SimpleNamespace(
            cfg=cfg,
            db=self.db,
            logger=logging.getLogger("test"),
            graph=graph,
            docuseal=_FakeDocuSealWebhook(),
            notifications=SimpleNamespace(),
        )
        payload = {"event": "submission.completed", "submission_id": "ds-submission-2"}
        request = _Request(state=state, body=json.dumps(payload).encode("utf-8"), headers={})

        with patch("grievance_api.web.routes_webhook.current_year_in_timezone", return_value=2026):
            result = await webhook_docuseal(request)
            result_dup = await webhook_docuseal(request)

        self.assertTrue(result["handled"])
        self.assertTrue(result_dup["deduped"])
        self.assertEqual(len(graph.calls), 2)
        self.assertEqual(graph.calls[0]["folder_path"], "Mobility Demand Forms/2026/Mobility Demand 1")
        self.assertEqual(graph.calls[0]["filename"], "Mobility Demand 1.pdf")
        self.assertEqual(graph.calls[1]["folder_path"], "Mobility Demand Forms/2026/Mobility Demand 1")
        self.assertEqual(graph.calls[1]["filename"], "Mobility Demand 1 Audit.zip")
        doc_row = await self.db.fetchone(
            "SELECT status, signed_pdf_path, audit_zip_path, sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url FROM standalone_documents WHERE id=?",
            (document_id,),
        )
        self.assertEqual(doc_row[0], "completed")
        self.assertTrue(Path(doc_row[1]).exists())
        self.assertTrue(Path(doc_row[2]).exists())
        self.assertIsNone(doc_row[3])
        self.assertEqual(doc_row[4], "https://sharepoint.local/Mobility Demand Forms/2026/Mobility Demand 1/Mobility Demand 1.pdf")
        self.assertEqual(doc_row[5], "https://sharepoint.local/Mobility Demand Forms/2026/Mobility Demand 1/Mobility Demand 1 Audit.zip")
        submission_row = await self.db.fetchone(
            "SELECT status, filing_year, filing_sequence, filing_label, sharepoint_folder_path FROM standalone_submissions WHERE id=?",
            (submission_id,),
        )
        self.assertEqual(submission_row[0], "completed")
        self.assertEqual(submission_row[1], 2026)
        self.assertEqual(submission_row[2], 1)
        self.assertEqual(submission_row[3], "Mobility Demand 1")
        self.assertEqual(submission_row[4], "Mobility Demand Forms/2026/Mobility Demand 1")

    async def test_failed_audit_upload_retry_reuses_same_sequence(self) -> None:
        submission_id = "S202603270002"
        document_id = "D202603270002"
        pdf_dir = Path(self.data_root) / "standalone" / submission_id / document_id
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / "generated.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% generated pdf\n")

        await self.db.exec(
            """INSERT INTO standalone_submissions(
                 id, request_id, form_key, form_title, signer_email, status, created_at_utc, template_data_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                submission_id,
                "forms-789",
                "att_mobility_bargaining_suggestion",
                "AT&T Mobility Bargaining Suggestion",
                "president@example.org",
                "awaiting_signature",
                "2026-03-27T12:00:00+00:00",
                "{}",
            ),
        )
        await self.db.exec(
            """INSERT INTO standalone_documents(
                 id, submission_id, created_at_utc, form_key, template_key, status, requires_signature,
                 signer_order_json, pdf_path, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                submission_id,
                "2026-03-27T12:00:00+00:00",
                "att_mobility_bargaining_suggestion",
                "att_mobility_bargaining_suggestion",
                "awaiting_signature",
                1,
                json.dumps(["president@example.org"]),
                str(pdf_path),
                "ds-submission-3",
                "https://sign.local/3",
            ),
        )

        cfg = self._cfg()
        cfg.docuseal = SimpleNamespace(webhook_secret="REPLACE", default_template_id=1, template_ids={}, strict_template_ids=False)
        cfg.graph.site_hostname = "contoso.sharepoint.com"
        cfg.graph.site_path = "/sites/Grievances"
        cfg.graph.document_library = "Documents"
        graph = _FakeGraphUploader(fail_audit_once=True)
        state = SimpleNamespace(
            cfg=cfg,
            db=self.db,
            logger=logging.getLogger("test"),
            graph=graph,
            docuseal=_FakeDocuSealWebhook(),
            notifications=SimpleNamespace(),
        )
        payload = {"event": "submission.completed", "submission_id": "ds-submission-3"}
        request = _Request(state=state, body=json.dumps(payload).encode("utf-8"), headers={})

        with patch("grievance_api.web.routes_webhook.current_year_in_timezone", return_value=2026):
            with self.assertRaises(RuntimeError):
                await webhook_docuseal(request)
            retry_result = await webhook_docuseal(request)

        self.assertTrue(retry_result["handled"])
        submission_row = await self.db.fetchone(
            "SELECT status, filing_year, filing_sequence, filing_label, sharepoint_folder_path FROM standalone_submissions WHERE id=?",
            (submission_id,),
        )
        self.assertEqual(submission_row[0], "completed")
        self.assertEqual(submission_row[1], 2026)
        self.assertEqual(submission_row[2], 1)
        self.assertEqual(submission_row[3], "Mobility Demand 1")
        self.assertEqual(submission_row[4], "Mobility Demand Forms/2026/Mobility Demand 1")

    async def test_webhook_completion_emails_attach_signed_pdf_for_signer_and_internal(self) -> None:
        submission_id = "S202603270003"
        document_id = "D202603270003"
        pdf_dir = Path(self.data_root) / "standalone" / submission_id / document_id
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / "generated.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% generated pdf\n")

        await self.db.exec(
            """INSERT INTO standalone_submissions(
                 id, request_id, form_key, form_title, signer_email, status, created_at_utc, template_data_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                submission_id,
                "forms-999",
                "att_mobility_bargaining_suggestion",
                "AT&T Mobility Bargaining Suggestion",
                "president@example.org",
                "awaiting_signature",
                "2026-03-27T12:00:00+00:00",
                "{}",
            ),
        )
        await self.db.exec(
            """INSERT INTO standalone_documents(
                 id, submission_id, created_at_utc, form_key, template_key, status, requires_signature,
                 signer_order_json, pdf_path, docuseal_submission_id, docuseal_signing_link
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                submission_id,
                "2026-03-27T12:00:00+00:00",
                "att_mobility_bargaining_suggestion",
                "att_mobility_bargaining_suggestion",
                "awaiting_signature",
                1,
                json.dumps(["president@example.org"]),
                str(pdf_path),
                "ds-submission-4",
                "https://sign.local/4",
            ),
        )

        cfg = self._cfg()
        cfg.docuseal = SimpleNamespace(webhook_secret="REPLACE", default_template_id=1, template_ids={}, strict_template_ids=False)
        cfg.graph.site_hostname = "contoso.sharepoint.com"
        cfg.graph.site_path = "/sites/Grievances"
        cfg.graph.document_library = "Documents"
        cfg.email.enabled = True
        cfg.email.internal_recipients = ("staff@example.org",)
        notifications = _FakeNotifications()
        state = SimpleNamespace(
            cfg=cfg,
            db=self.db,
            logger=logging.getLogger("test"),
            graph=_FakeGraphUploader(),
            docuseal=_FakeDocuSealWebhook(),
            notifications=notifications,
        )
        payload = {"event": "submission.completed", "submission_id": "ds-submission-4"}
        request = _Request(state=state, body=json.dumps(payload).encode("utf-8"), headers={})

        with patch("grievance_api.web.routes_webhook.current_year_in_timezone", return_value=2026):
            result = await webhook_docuseal(request)

        self.assertTrue(result["handled"])
        self.assertEqual(len(notifications.calls), 2)
        signer_call = notifications.calls[0]
        internal_call = notifications.calls[1]
        self.assertEqual(signer_call["template_key"], "standalone_completion_signer")
        self.assertEqual(internal_call["template_key"], "standalone_completion_internal")
        self.assertIsNotNone(signer_call["attachments"])
        self.assertIsNotNone(internal_call["attachments"])
        self.assertEqual(signer_call["attachments"][0].filename, "att_mobility_bargaining_suggestion_signed.pdf")
        self.assertEqual(internal_call["attachments"][0].filename, "att_mobility_bargaining_suggestion_signed.pdf")


if __name__ == "__main__":
    unittest.main()
