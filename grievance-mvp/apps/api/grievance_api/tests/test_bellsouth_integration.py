from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    _should_clear_3g3a_stage_marks,
    _validate_existing_folder_mode,
    _preferred_signer_email_for_doc,
    _resolve_document_command,
    _upload_generated_pdf_to_case_folder,
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
                "non_discipline_grievance_brief": "/tmp/non-discipline.docx",
                "disciplinary_grievance_brief": "/tmp/disciplinary.docx",
                "mobility_record_of_grievance": "/tmp/mobility-record.docx",
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

        non_discipline = _resolve_document_command(cfg, "non_discipline_brief")
        self.assertEqual(non_discipline.doc_type, "non_discipline_grievance_brief")
        self.assertEqual(non_discipline.template_key, "non_discipline_grievance_brief")

        non_disciplinary = _resolve_document_command(cfg, "non_disciplinary_grievance_brief")
        self.assertEqual(non_disciplinary.doc_type, "non_discipline_grievance_brief")
        self.assertEqual(non_disciplinary.template_key, "non_discipline_grievance_brief")

        disciplinary = _resolve_document_command(cfg, "disciplinary_brief")
        self.assertEqual(disciplinary.doc_type, "disciplinary_grievance_brief")
        self.assertEqual(disciplinary.template_key, "disciplinary_grievance_brief")

        mobility_record = _resolve_document_command(cfg, "mobility_record_of_grievance")
        self.assertEqual(mobility_record.doc_type, "mobility_record_of_grievance")
        self.assertEqual(mobility_record.template_key, "mobility_record_of_grievance")

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


class _UploadGraphStub:
    def __init__(self) -> None:
        self.upload_calls: list[dict[str, object]] = []

    def upload_to_case_subfolder(self, **kwargs):  # noqa: ANN003
        self.upload_calls.append(dict(kwargs))
        filename = str(kwargs["filename"])
        return SimpleNamespace(
            path=f"/Documents/Grievances/Generated/{filename}",
            web_url=f"https://sharepoint.local/{filename}",
        )


class GeneratedUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_generated_pdf_to_case_folder_records_sharepoint_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "generated.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 test")

            db = SimpleNamespace(add_event=AsyncMock())
            graph = _UploadGraphStub()
            cfg = SimpleNamespace(
                graph=SimpleNamespace(
                    site_hostname="contoso.sharepoint.com",
                    site_path="/sites/Grievances",
                    document_library="Documents",
                    case_parent_folder="Grievances",
                    generated_subfolder="Generated",
                )
            )

            url = await _upload_generated_pdf_to_case_folder(
                cfg=cfg,
                db=db,
                graph=graph,
                case_id="C2026001",
                document_id="D2026001",
                doc_type="data_request_letterhead",
                sharepoint_case_folder="2026001 John Doe",
                pdf_path=str(pdf_path),
            )

        self.assertEqual(url, "https://sharepoint.local/data_request_letterhead_D2026001.pdf")
        self.assertEqual(len(graph.upload_calls), 1)
        self.assertEqual(graph.upload_calls[0]["case_folder_name"], "2026001 John Doe")
        self.assertEqual(graph.upload_calls[0]["subfolder"], "Generated")
        db.add_event.assert_awaited_once()

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

    def test_statement_of_occurrence_does_not_clear_3g3a_stage_marks(self) -> None:
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
            _should_clear_3g3a_stage_marks(
                cfg=cfg,
                doc_req=DocumentRequest(
                    doc_type="statement_of_occurrence",
                    template_key="statement_of_occurrence",
                    requires_signature=True,
                ),
            )
        )

    def test_3g3a_documents_clear_stage_marks_before_render(self) -> None:
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
        self.assertTrue(
            _should_clear_3g3a_stage_marks(
                cfg=cfg,
                doc_req=DocumentRequest(
                    doc_type="bst_grievance_form_3g3a",
                    template_key="bst_grievance_form_3g3a",
                    requires_signature=True,
                ),
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

    def test_mobility_record_defaults_use_case_values(self) -> None:
        payload = IntakeRequest(
            request_id="req-6c",
            contract="AT&T Mobility",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            incident_date="2026-04-02",
            narrative="Initial union statement",
            template_data={"local_number": "3106", "work_location": "Jacksonville, FL"},
        )
        cfg = SimpleNamespace(rendering=SimpleNamespace(layout_policies={}))

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="mobility_record_of_grievance",
            grievance_number=None,
        )
        self.assertEqual(context["cw_grievance_number"], "2026001")
        self.assertEqual(context["date_grievance_occurred"], "2026-04-02")
        self.assertEqual(context["specific_location_state"], "Jacksonville, FL")
        self.assertEqual(context["employee_work_group_name"], "John Doe")
        self.assertEqual(context["union_statement"], "Initial union statement")

    def test_non_discipline_dates_are_normalized(self) -> None:
        payload = IntakeRequest(
            request_id="req-6d",
            contract="CWA",
            grievant_firstname="John",
            grievant_lastname="Doe",
            grievant_email="john@example.com",
            narrative="Non-discipline brief",
            template_data={
                "grievant_name": "John Doe",
                "local_number": "3106",
                "location": "Jacksonville, FL",
                "grievant_or_work_group": "John Doe",
                "grievant_home_address": "123 Main St",
                "date_grievance_occurred": "04/02/2026",
                "date_grievance_filed": "04/03/2026",
                "date_grievance_appealed_to_executive_level": "04/10/2026",
                "issue_or_condition_involved": "Issue text",
                "action_taken": "Action text",
                "chronology_of_facts": "Facts text",
                "analysis_of_grievance": "Analysis text",
                "current_status": "Current status",
                "union_position": "Union position",
                "company_position": "Company position",
                "recommendation": "Recommendation",
            },
        )
        cfg = SimpleNamespace(rendering=SimpleNamespace(layout_policies={}))

        context, _ = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id="C1",
            grievance_id="2026001",
            document_id="D1",
            doc_type="non_discipline_grievance_brief",
            grievance_number=None,
        )
        self.assertEqual(context["date_grievance_occurred"], "2026-04-02")
        self.assertEqual(context["date_grievance_filed"], "2026-04-03")
        self.assertEqual(context["date_grievance_appealed_to_executive_level"], "2026-04-10")


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
        self.assertEqual(
            _build_document_basename(
                doc_type="mobility_record_of_grievance",
                grievance_id=grievance_id,
                member_name=member_name,
            ),
            "2026001 - john doe - mobility record of grievance",
        )
        self.assertEqual(
            _build_document_basename(
                doc_type="non_discipline_grievance_brief",
                grievance_id=grievance_id,
                member_name=member_name,
            ),
            "2026001 - john doe - non discipline grievance brief",
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
