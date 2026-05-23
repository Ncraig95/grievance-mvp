from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from grievance_api.services.graph_mail import MailAttachment
from grievance_api.services.local_providers import LocalDocuSealClient, LocalGraphMailer, LocalSharePointUploader


class LocalProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.tmpdir.name) / "data"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_local_mailer_writes_message_json_and_attachments(self) -> None:
        mailer = LocalGraphMailer(data_root=str(self.data_root), sender_user_id="sender@example.invalid")
        sent = mailer.send_mail(
            to_recipients=["worker@example.invalid"],
            subject="Local smoke",
            text_body="body",
            attachments=[MailAttachment(filename="signed.pdf", content_type="application/pdf", content_bytes=b"pdf")],
            custom_headers={"X-Test": "1"},
        )

        message_path = self.data_root / "local_mock" / "mail" / sent.graph_message_id / "message.json"
        self.assertTrue(message_path.exists())
        payload = json.loads(message_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["sender_user_id"], "sender@example.invalid")
        self.assertEqual(payload["to_recipients"], ["worker@example.invalid"])
        self.assertEqual(payload["custom_headers"], {"X-Test": "1"})
        attachment_path = Path(payload["attachments"][0]["path"])
        self.assertEqual(attachment_path.read_bytes(), b"pdf")
        self.assertTrue(sent.graph_message_id.startswith("local-mail-"))

    def test_local_sharepoint_folder_lookup_upload_and_download(self) -> None:
        graph = LocalSharePointUploader(data_root=str(self.data_root))
        folder = graph.ensure_case_folder(
            site_hostname="local.sharepoint.invalid",
            site_path="/sites/LocalSafe",
            library="Documents",
            case_parent_folder="Grievances",
            grievance_id="2026001",
            member_name="Local Signer",
        )
        self.assertEqual(folder.folder_name, "2026001 Local Signer")
        self.assertTrue(folder.web_url.startswith("local://sharepoint/"))

        resolved = graph.find_case_folder_by_grievance_id_exact(
            site_hostname="local.sharepoint.invalid",
            site_path="/sites/LocalSafe",
            library="Documents",
            case_parent_folder="Grievances",
            grievance_id="2026001",
        )
        self.assertEqual(resolved.folder_name, folder.folder_name)

        uploaded = graph.upload_to_case_subfolder(
            site_hostname="local.sharepoint.invalid",
            site_path="/sites/LocalSafe",
            library="Documents",
            case_folder_name=folder.folder_name,
            case_parent_folder="Grievances",
            subfolder="Generated",
            filename="grievance_form_D1.pdf",
            file_bytes=b"generated-pdf",
        )
        self.assertEqual(graph.download_item_bytes(drive_id=uploaded.drive_id, item_id=uploaded.item_id), b"generated-pdf")
        files = graph.list_files_in_folder_path(
            site_hostname="local.sharepoint.invalid",
            site_path="/sites/LocalSafe",
            library="Documents",
            folder_path="Grievances/2026001 Local Signer/Generated",
        )
        self.assertEqual([item.name for item in files], ["grievance_form_D1.pdf"])
        self.assertTrue(uploaded.web_url.startswith("local://sharepoint/"))

    def test_local_docuseal_submission_links_and_completed_artifacts(self) -> None:
        docuseal = LocalDocuSealClient(data_root=str(self.data_root), public_base_url="local://docuseal")
        submission = docuseal.create_submission(
            pdf_bytes=b"%PDF-1.4\nunsigned\n",
            alignment_pdf_bytes=b"%PDF-1.4\nalignment\n",
            signers=["worker@example.invalid"],
            title="Local grievance",
            metadata={"case_id": "C1", "document_id": "D1"},
            template_id=1,
            form_key="grievance_form",
        )

        self.assertTrue(submission.submission_id.startswith("local-sub-"))
        self.assertTrue(submission.signing_link.startswith("local://docuseal/submissions/"))
        self.assertEqual(
            docuseal.extract_signing_links_by_email(submission.raw),
            {"worker@example.invalid": submission.signing_link},
        )
        self.assertEqual(
            docuseal.fetch_signing_links_by_email(submission_id=submission.submission_id),
            {"worker@example.invalid": submission.signing_link},
        )

        artifacts = docuseal.download_completed_artifacts(submission_id=submission.submission_id)
        self.assertGreater(len(artifacts["completed_zip_bytes"]), 0)
        self.assertEqual(artifacts["signed_pdf_bytes"], b"%PDF-1.4\nunsigned\n")
        submission_dir = self.data_root / "local_mock" / "docuseal" / "submissions" / submission.submission_id
        self.assertTrue((submission_dir / "submitted.pdf").exists())
        self.assertTrue((submission_dir / "signed.pdf").exists())
        self.assertTrue((submission_dir / "completed.zip").exists())
