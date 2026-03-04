from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from grievance_api.core.config import DocumentPolicyConfig
from grievance_api.services.sharepoint_graph import (
    CaseFolderAmbiguousError,
    CaseFolderNotFoundError,
    GraphUploader,
)
from grievance_api.web.models import DocumentRequest, IntakeRequest
from grievance_api.web.routes_intake import (
    _build_document_basename,
    _build_template_context,
    _doc_uses_auto_grievance_id,
    _doc_requires_existing_exact_folder,
    _validate_existing_folder_mode,
    _preferred_signer_email_for_doc,
    _resolve_document_command,
)
from grievance_api.services.staged_signature_workflow import is_3g3a_staged


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

    def test_non_bellsouth_alias_commands_resolve(self) -> None:
        cfg = SimpleNamespace(
            doc_templates={
                "mobility_formal_grievance_meeting_request": "/tmp/mobility.docx",
                "grievance_data_request_form": "/tmp/data-request.docx",
                "true_intent_grievance_brief": "/tmp/true-intent.docx",
                "disciplinary_grievance_brief": "/tmp/disciplinary.docx",
            },
            document_policies={},
        )

        mobility = _resolve_document_command(cfg, "mobility_meeting_request")
        self.assertEqual(mobility.doc_type, "mobility_formal_grievance_meeting_request")
        self.assertEqual(mobility.template_key, "mobility_formal_grievance_meeting_request")

        data_request = _resolve_document_command(cfg, "grievance_data_request")
        self.assertEqual(data_request.doc_type, "grievance_data_request_form")
        self.assertEqual(data_request.template_key, "grievance_data_request_form")

        true_intent = _resolve_document_command(cfg, "true_intent_brief")
        self.assertEqual(true_intent.doc_type, "true_intent_grievance_brief")
        self.assertEqual(true_intent.template_key, "true_intent_grievance_brief")

        disciplinary = _resolve_document_command(cfg, "disciplinary_brief")
        self.assertEqual(disciplinary.doc_type, "disciplinary_grievance_brief")
        self.assertEqual(disciplinary.template_key, "disciplinary_grievance_brief")

    def test_existing_exact_folder_policy_is_applied(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bellsouth_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                ),
                "mobility_formal_grievance_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                )
            }
        )
        bellsouth_doc_req = DocumentRequest(
            doc_type="bellsouth_meeting_request",
            template_key="bellsouth_formal_grievance_meeting_request",
            requires_signature=True,
        )
        self.assertTrue(_doc_requires_existing_exact_folder(cfg=cfg, doc_req=bellsouth_doc_req))

        mobility_doc_req = DocumentRequest(
            doc_type="mobility_formal_grievance_meeting_request",
            template_key="mobility_formal_grievance_meeting_request",
            requires_signature=True,
        )
        self.assertTrue(_doc_requires_existing_exact_folder(cfg=cfg, doc_req=mobility_doc_req))

    def test_existing_folder_mode_requires_incoming_grievance_id(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_existing_folder_mode("")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_auto_grievance_id_is_statement_only(self) -> None:
        self.assertTrue(
            _doc_uses_auto_grievance_id(
                DocumentRequest(doc_type="statement_of_occurrence", template_key="statement_of_occurrence")
            )
        )
        self.assertFalse(
            _doc_uses_auto_grievance_id(
                DocumentRequest(doc_type="bellsouth_meeting_request", template_key="bellsouth_formal_grievance_meeting_request")
            )
        )

    def test_bellsouth_is_not_staged_3g3a(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bst_grievance_form_3g3a": DocumentPolicyConfig(
                    folder_resolution="default",
                    default_signer_field="",
                    default_requires_signature=True,
                    staged_flow_enabled=True,
                )
            }
        )
        self.assertFalse(
            is_3g3a_staged(
                cfg=cfg,
                doc_type="bellsouth_meeting_request",
                template_key="bellsouth_formal_grievance_meeting_request",
            )
        )


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

    def test_union_rep_email_signer_works_when_grievant_email_missing(self) -> None:
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
            request_id="req-2b",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            narrative="test",
            template_data={"union_rep_email": "grievances@cwa3106.com"},
        )

        signer, source = _preferred_signer_email_for_doc(
            payload=payload,
            doc_type="bellsouth_meeting_request",
            template_key="bellsouth_formal_grievance_meeting_request",
            cfg=cfg,
        )
        self.assertEqual(signer, "grievances@cwa3106.com")
        self.assertEqual(source, "template_data.union_rep_email")

    def test_mobility_union_rep_email_is_default_signer(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "mobility_formal_grievance_meeting_request": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
                    default_signer_field="union_rep_email",
                    default_requires_signature=True,
                )
            }
        )
        payload = IntakeRequest(
            request_id="req-2c",
            contract="AT&T Mobility",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="grievant@example.com",
            narrative="test",
            template_data={"union_rep_email": "mobility-rep@example.com"},
        )

        signer, source = _preferred_signer_email_for_doc(
            payload=payload,
            doc_type="mobility_formal_grievance_meeting_request",
            template_key="mobility_formal_grievance_meeting_request",
            cfg=cfg,
        )
        self.assertEqual(signer, "mobility-rep@example.com")
        self.assertEqual(source, "template_data.union_rep_email")


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
        self.assertEqual(context["meeting_requested_date"], "02/21/2026")

    def test_meeting_requested_fields_default_to_tbd_when_missing(self) -> None:
        payload = IntakeRequest(
            request_id="req-4",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            narrative="Meeting request context",
            template_data={},
        )
        cfg = SimpleNamespace(rendering=SimpleNamespace(layout_policies={}))

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="bellsouth_meeting_request",
            grievance_number=None,
        )
        self.assertEqual(context["meeting_requested_date"], "TBD")
        self.assertEqual(context["meeting_requested_time"], "TBD")
        self.assertEqual(context["meeting_requested_place"], "TBD")

    def test_meeting_requested_fields_default_to_tbd_when_empty_or_null(self) -> None:
        payload = IntakeRequest(
            request_id="req-5",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            narrative="Meeting request context",
            template_data={
                "meeting_requested_date": "",
                "meeting_requested_time": None,
                "meeting_requested_place": "null",
            },
        )
        cfg = SimpleNamespace(rendering=SimpleNamespace(layout_policies={}))

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="bellsouth_meeting_request",
            grievance_number=None,
        )
        self.assertEqual(context["meeting_requested_date"], "TBD")
        self.assertEqual(context["meeting_requested_time"], "TBD")
        self.assertEqual(context["meeting_requested_place"], "TBD")

    def test_meeting_requested_fields_preserve_literal_tbd(self) -> None:
        payload = IntakeRequest(
            request_id="req-6",
            contract="BellSouth",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            narrative="Meeting request context",
            template_data={
                "meeting_requested_date": "TBD",
                "meeting_requested_time": "TBD",
                "meeting_requested_place": "TBD",
                "reply_to_name_1": "CWA Local 3106",
                "reply_to_address_1": "4076 Union Hall Pl",
            },
        )
        cfg = SimpleNamespace(rendering=SimpleNamespace(layout_policies={}))

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="bellsouth_meeting_request",
            grievance_number=None,
        )
        self.assertEqual(context["meeting_requested_date"], "TBD")
        self.assertEqual(context["meeting_requested_time"], "TBD")
        self.assertEqual(context["meeting_requested_place"], "TBD")
        self.assertEqual(context["reply_to_name_1"], "CWA Local 3106")
        self.assertEqual(context["reply_to_address_1"], "4076 Union Hall Pl")

    def test_mobility_meeting_requested_fields_default_to_tbd_when_missing(self) -> None:
        payload = IntakeRequest(
            request_id="req-6b",
            contract="AT&T Mobility",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            narrative="Mobility meeting request context",
            template_data={},
        )
        cfg = SimpleNamespace(rendering=SimpleNamespace(layout_policies={}))

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="mobility_formal_grievance_meeting_request",
            grievance_number=None,
        )
        self.assertEqual(context["meeting_requested_date"], "TBD")
        self.assertEqual(context["meeting_requested_time"], "TBD")
        self.assertEqual(context["meeting_requested_place"], "TBD")


class FolderMatcherTests(unittest.TestCase):
    def test_exact_grievance_prefix_matcher(self) -> None:
        self.assertTrue(
            GraphUploader._matches_grievance_id_prefix(grievance_id="2026001", folder_name="2026001 John Doe")
        )
        self.assertTrue(GraphUploader._matches_grievance_id_prefix(grievance_id="2026001", folder_name="2026001"))
        self.assertFalse(
            GraphUploader._matches_grievance_id_prefix(grievance_id="2026001", folder_name="X2026001 John Doe")
        )

    def test_document_basenames_are_human_readable(self) -> None:
        grievance_id = "2026001"
        member_name = "John Doe"
        self.assertEqual(
            _build_document_basename(
                doc_type="mobility_formal_grievance_meeting_request",
                grievance_id=grievance_id,
                member_name=member_name,
            ),
            "2026001 - john doe - mobility meeting request",
        )
        self.assertEqual(
            _build_document_basename(
                doc_type="grievance_data_request_form",
                grievance_id=grievance_id,
                member_name=member_name,
            ),
            "2026001 - john doe - grievance data request",
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
