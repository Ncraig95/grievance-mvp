from __future__ import annotations

import unittest
from types import SimpleNamespace

from grievance_api.core.config import DocumentPolicyConfig
from grievance_api.services.sharepoint_graph import (
    CaseFolderAmbiguousError,
    CaseFolderNotFoundError,
    GraphUploader,
)
from grievance_api.web.models import DocumentRequest, IntakeRequest
from grievance_api.web.routes_intake import (
    _build_template_context,
    _doc_requires_existing_exact_folder,
    _preferred_signer_email_for_doc,
    _resolve_document_command,
)


class BellSouthCommandTests(unittest.TestCase):
    def test_bellsouth_command_resolves_with_alias(self) -> None:
        cfg = SimpleNamespace(
            doc_templates={
                "bellsouth_formal_grievance_meeting_request": "/tmp/bellsouth.docx",
            },
            document_policies={
                "bellsouth_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                )
            },
        )

        doc = _resolve_document_command(cfg, "bellsouth_meeting_request")
        self.assertEqual(doc.doc_type, "bellsouth_meeting_request")
        self.assertEqual(doc.template_key, "bellsouth_formal_grievance_meeting_request")
        self.assertTrue(doc.requires_signature)

    def test_existing_exact_folder_policy_is_applied(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bellsouth_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                )
            }
        )
        doc_req = DocumentRequest(
            doc_type="bellsouth_meeting_request",
            template_key="bellsouth_formal_grievance_meeting_request",
            requires_signature=True,
        )
        self.assertTrue(_doc_requires_existing_exact_folder(cfg=cfg, doc_req=doc_req))


class BellSouthSignerTests(unittest.TestCase):
    def test_union_rep_email_is_default_signer(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bellsouth_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                )
            }
        )
        payload = IntakeRequest(
            request_id="req-1",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="grievant@example.com",
            narrative="test",
            template_data={"union_rep_email": "rep@example.com"},
        )

        signer, source = _preferred_signer_email_for_doc(
            payload=payload,
            doc_type="bellsouth_meeting_request",
            template_key="bellsouth_formal_grievance_meeting_request",
            cfg=cfg,
        )
        self.assertEqual(signer, "rep@example.com")
        self.assertEqual(source, "template_data.union_rep_email")

    def test_signer_falls_back_to_grievant_email(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bellsouth_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                )
            }
        )
        payload = IntakeRequest(
            request_id="req-2",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="grievant@example.com",
            narrative="test",
            template_data={},
        )

        signer, source = _preferred_signer_email_for_doc(
            payload=payload,
            doc_type="bellsouth_meeting_request",
            template_key="bellsouth_formal_grievance_meeting_request",
            cfg=cfg,
        )
        self.assertEqual(signer, "grievant@example.com")
        self.assertEqual(source, "default.grievant_email")


class BellSouthContextTests(unittest.TestCase):
    def test_template_context_populates_bellsouth_fields(self) -> None:
        payload = IntakeRequest(
            request_id="req-3",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            incident_date="2026-02-20",
            narrative="Meeting request context",
            template_data={
                "to": "Labor Relations",
                "union_rep_email": "rep@example.com",
                "meeting_requested_date": "02/21/2026",
            },
        )
        cfg = SimpleNamespace(
            rendering=SimpleNamespace(layout_policies={}),
        )

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="bellsouth_meeting_request",
            grievance_number=None,
        )
        self.assertEqual(context["to"], "Labor Relations")
        self.assertEqual(context["grievant_names"], "John Doe")
        self.assertEqual(context["date_grievance_occurred"], "2026-02-20")
        self.assertEqual(context["meeting_requested_date"], "2026-02-21")


class FolderMatcherTests(unittest.TestCase):
    def test_exact_grievance_prefix_matcher(self) -> None:
        self.assertTrue(
            GraphUploader._matches_grievance_id_prefix(grievance_id="2026001", folder_name="2026001 John Doe")
        )
        self.assertTrue(GraphUploader._matches_grievance_id_prefix(grievance_id="2026001", folder_name="2026001"))
        self.assertFalse(
            GraphUploader._matches_grievance_id_prefix(grievance_id="2026001", folder_name="X2026001 John Doe")
        )

    def test_find_case_folder_exact_returns_single_match(self) -> None:
        uploader = self._build_uploader_with_children(
            [
                {"id": "1", "name": "2026001 John Doe", "folder": {}, "webUrl": "https://sp/folder1"},
                {"id": "2", "name": "2026002 Jane Doe", "folder": {}, "webUrl": "https://sp/folder2"},
            ]
        )
        ref = uploader.find_case_folder_by_grievance_id_exact(
            site_hostname="contoso.sharepoint.com",
            site_path="/sites/Grievances",
            library="Documents",
            case_parent_folder="Grievances",
            grievance_id="2026001",
        )
        self.assertEqual(ref.folder_name, "2026001 John Doe")

    def test_find_case_folder_exact_raises_not_found(self) -> None:
        uploader = self._build_uploader_with_children(
            [{"id": "2", "name": "2026002 Jane Doe", "folder": {}, "webUrl": "https://sp/folder2"}]
        )
        with self.assertRaises(CaseFolderNotFoundError):
            uploader.find_case_folder_by_grievance_id_exact(
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                library="Documents",
                case_parent_folder="Grievances",
                grievance_id="2026001",
            )

    def test_find_case_folder_exact_raises_ambiguous(self) -> None:
        uploader = self._build_uploader_with_children(
            [
                {"id": "1", "name": "2026001 John Doe", "folder": {}, "webUrl": "https://sp/folder1"},
                {"id": "2", "name": "2026001 John Doe - Legacy", "folder": {}, "webUrl": "https://sp/folder2"},
            ]
        )
        with self.assertRaises(CaseFolderAmbiguousError):
            uploader.find_case_folder_by_grievance_id_exact(
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                library="Documents",
                case_parent_folder="Grievances",
                grievance_id="2026001",
            )

    @staticmethod
    def _build_uploader_with_children(children: list[dict[str, object]]) -> GraphUploader:
        uploader = GraphUploader(
            tenant_id="tenant",
            client_id="client",
            cert_thumbprint="thumb",
            cert_pem_path="/tmp/cert.pem",
            dry_run=True,
        )
        uploader._drive_id = lambda *args, **kwargs: "drive"  # type: ignore[method-assign]
        uploader._ensure_folder_chain = lambda *args, **kwargs: ("parent", "Grievances")  # type: ignore[method-assign]
        uploader._list_children = lambda *args, **kwargs: children  # type: ignore[method-assign]
        return uploader


if __name__ == "__main__":
    unittest.main()
