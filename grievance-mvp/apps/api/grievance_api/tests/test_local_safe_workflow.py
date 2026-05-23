from __future__ import annotations

import hashlib
import hmac
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grievance_api.core.config import (
    AppConfig,
    DocuSealConfig,
    DocumentPolicyConfig,
    EmailConfig,
    GraphConfig,
    GrievanceIdConfig,
    IntakeAuthConfig,
    RenderingConfig,
)
from grievance_api.core.hmac_auth import compute_signature
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services.email_templates import EmailTemplateStore
from grievance_api.services.local_providers import LocalDocuSealClient, LocalGraphMailer, LocalSharePointUploader
from grievance_api.services.notification_service import NotificationService
from grievance_api.web.routes_intake import intake
from grievance_api.web.routes_webhook import webhook_docuseal


class _Request:
    def __init__(self, *, state, body: bytes, headers: dict[str, str], path: str) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self._body = body
        self.headers = headers
        self.client = SimpleNamespace(host="127.0.0.1")
        self.url = SimpleNamespace(path=path)

    async def body(self) -> bytes:
        return self._body


class LocalSafeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.root = Path(self.tmpdir.name)
        self.db_path = str(self.root / "grievances.sqlite3")
        self.data_root = str(self.root / "grievances")
        migrate(self.db_path)
        self.db = Db(self.db_path)
        self.cfg = self._cfg()
        self.graph = LocalSharePointUploader(data_root=self.data_root)
        self.docuseal = LocalDocuSealClient(data_root=self.data_root, public_base_url="local://docuseal")
        self.mailer = LocalGraphMailer(data_root=self.data_root, sender_user_id=self.cfg.email.sender_user_id)
        self.notifications = NotificationService(
            db=self.db,
            logger=logging.getLogger("test.local_safe_workflow"),
            mailer=self.mailer,
            template_store=EmailTemplateStore(str(Path(__file__).resolve().parents[2] / "templates" / "email")),
            email_cfg=self.cfg.email,
        )
        self.state = SimpleNamespace(
            cfg=self.cfg,
            db=self.db,
            logger=logging.getLogger("test.local_safe_workflow"),
            graph=self.graph,
            docuseal=self.docuseal,
            notifications=self.notifications,
        )

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    def _cfg(self) -> AppConfig:
        templates_root = Path(__file__).resolve().parents[2] / "templates"
        return AppConfig(
            app_mode="local",
            hmac_shared_secret="local-hmac",
            db_path=self.db_path,
            data_root=self.data_root,
            docx_template_path=str(templates_root / "grievance_template.docx"),
            doc_templates={"grievance_form": str(templates_root / "grievance_template.docx")},
            libreoffice_timeout_seconds=45,
            graph=GraphConfig(
                tenant_id="local-tenant",
                client_id="local-client",
                cert_pem_path="/tmp/local-cert.pem",
                cert_thumbprint="local-thumb",
                site_hostname="local.sharepoint.invalid",
                site_path="/sites/LocalSafe",
                document_library="Documents",
                case_parent_folder="Grievances",
                generated_subfolder="Generated",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
                audit_backup_subfolders=(),
                audit_local_backup_roots=(),
                client_supplied_subfolder="Client supplied data",
                failed_processes_folder="config files/failed",
            ),
            docuseal=DocuSealConfig(
                base_url="local://docuseal",
                api_token="local-token",
                webhook_secret="local-webhook",
                public_base_url="local://docuseal",
                web_base_url="local://docuseal",
                web_email="local-admin@example.invalid",
                web_password="local-password",
                default_template_id=1,
                template_ids={},
            ),
            email=EmailConfig(
                enabled=True,
                sender_user_id="local-mailer@example.invalid",
                templates_dir=str(templates_root / "email"),
                internal_recipients=("team@example.invalid",),
                derek_email=None,
                approval_request_url_base="http://127.0.0.1:8080/approval",
                allow_signer_copy_link=True,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=0,
                dry_run=False,
            ),
            grievance_id=GrievanceIdConfig(
                mode="auto",
                timezone="America/New_York",
                min_width=3,
                separator="",
            ),
            intake_auth=IntakeAuthConfig(
                shared_header_name="X-Intake-Key",
                shared_header_value="",
                cloudflare_access_client_id="",
                cloudflare_access_client_secret="",
            ),
            rendering=RenderingConfig(normalize_split_placeholders=True, layout_policies={}),
            document_policies={
                "grievance_form": DocumentPolicyConfig(
                    folder_resolution="default",
                    default_signer_field="grievant_email",
                    default_requires_signature=True,
                    signature_dispatch_timing="immediate",
                )
            },
            docx_pdf_engine="libreoffice",
            docx_pdf_graph_temp_folder="Grievances/_local_docx_pdf_convert",
            wait_for_grievance_number_before_signature=False,
            require_approver_decision=False,
        )

    @staticmethod
    def _fake_render_docx(template_path: str, context: dict, output_path: str, **kwargs) -> None:  # noqa: ANN003
        _ = (template_path, context, kwargs)
        Path(output_path).write_bytes(b"local-test-docx")

    @staticmethod
    def _fake_docx_to_pdf(docx_path: str, output_dir: str, timeout_seconds: int, **kwargs) -> str:  # noqa: ANN003
        _ = (timeout_seconds, kwargs)
        pdf_path = Path(output_dir) / f"{Path(docx_path).stem}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\nlocal workflow pdf\n")
        return str(pdf_path)

    def _signed_intake_request(self, payload: dict[str, object]) -> _Request:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ts = str(int(time.time()))
        return _Request(
            state=self.state,
            body=body,
            headers={"X-Timestamp": ts, "X-Signature": compute_signature(self.cfg.hmac_shared_secret, ts, body)},
            path="/intake",
        )

    def _signed_webhook_request(self, payload: dict[str, object]) -> _Request:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        sig = hmac.new(self.cfg.docuseal.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return _Request(
            state=self.state,
            body=body,
            headers={"X-DocuSeal-Signature": sig},
            path="/webhook/docuseal",
        )

    async def test_intake_signature_webhook_email_audit_and_local_sharepoint(self) -> None:
        payload = {
            "request_id": "local-workflow-test-1",
            "contract": "AT&T",
            "grievant_firstname": "Local",
            "grievant_lastname": "Signer",
            "grievant_email": "local.signer@example.invalid",
            "incident_date": "2026-05-23",
            "narrative": "Local-safe workflow test.",
            "template_data": {"personal_email": "local.signer@example.invalid"},
            "documents": [
                {
                    "doc_type": "grievance_form",
                    "template_key": "grievance_form",
                    "requires_signature": True,
                    "signers": ["local.signer@example.invalid"],
                }
            ],
        }

        with patch("grievance_api.web.routes_intake.render_docx", self._fake_render_docx), patch(
            "grievance_api.web.routes_intake.docx_to_pdf", self._fake_docx_to_pdf
        ):
            intake_response = await intake(self._signed_intake_request(payload))

        self.assertEqual(intake_response.status, "awaiting_signatures")
        document = intake_response.documents[0]
        self.assertEqual(document.status, "sent_for_signature")
        self.assertTrue((document.signing_link or "").startswith("local://docuseal/"))

        doc_row = await self.db.fetchone(
            "SELECT docuseal_submission_id, pdf_path FROM documents WHERE id=?",
            (document.document_id,),
        )
        self.assertIsNotNone(doc_row)
        submission_id = str(doc_row[0])
        self.assertTrue(submission_id.startswith("local-sub-"))
        self.assertTrue(Path(str(doc_row[1])).exists())

        webhook_response = await webhook_docuseal(
            self._signed_webhook_request(
                {
                    "event": "submission.completed",
                    "submission_id": submission_id,
                    "submission": {
                        "id": submission_id,
                        "status": "completed",
                        "submitters": [{"email": "local.signer@example.invalid", "status": "completed"}],
                    },
                }
            )
        )
        self.assertEqual(webhook_response, {"ok": True, "handled": True})

        completed = await self.db.fetchone(
            """
            SELECT status, signed_pdf_path, audit_zip_path,
                   sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url
            FROM documents WHERE id=?
            """,
            (document.document_id,),
        )
        self.assertIsNotNone(completed)
        self.assertIn(completed[0], {"signed", "approved"})
        self.assertTrue(Path(str(completed[1])).exists())
        self.assertTrue(Path(str(completed[2])).exists())
        self.assertTrue(str(completed[3]).startswith("local://sharepoint/"))
        self.assertTrue(str(completed[4]).startswith("local://sharepoint/"))
        self.assertTrue(str(completed[5]).startswith("local://sharepoint/"))

        sent_count = await self.db.fetchone("SELECT COUNT(1) FROM outbound_emails WHERE status='sent'", ())
        self.assertGreaterEqual(int(sent_count[0]), 1)
        mail_messages = list((Path(self.data_root) / "local_mock" / "mail").glob("*/message.json"))
        self.assertGreaterEqual(len(mail_messages), 1)

        sharepoint_root = Path(self.data_root) / "local_mock" / "sharepoint" / "Documents" / "Grievances"
        expected_names = {
            f"grievance_form_{document.document_id}.pdf",
            f"grievance_form_{document.document_id}_signed.pdf",
            f"grievance_form_{document.document_id}_audit.zip",
        }
        found_names = {item.name for item in sharepoint_root.rglob("*") if item.is_file()}
        self.assertTrue(expected_names.issubset(found_names))
