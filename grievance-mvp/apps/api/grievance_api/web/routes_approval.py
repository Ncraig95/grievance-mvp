from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..db.db import Db, utcnow
from ..services.audit_backups import fanout_audit_backups, merge_backup_locations_json
from ..services.case_folder_naming import build_case_folder_member_name, resolve_contract_label
from ..services.notification_service import NotificationService
from ..services.signature_workflow import send_document_for_signature, signer_order_from_json
from .models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    AssignGrievanceNumberRequest,
    CaseStatusResponse,
    DocumentStatus,
)

router = APIRouter()


async def _load_case_status(db: Db, case_id: str) -> CaseStatusResponse:
    case_row = await db.fetchone(
        "SELECT grievance_id, status, approval_status, grievance_number FROM cases WHERE id=?",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    docs = await db.fetchall(
        "SELECT id, doc_type, status, docuseal_signing_link FROM documents WHERE case_id=? ORDER BY created_at_utc",
        (case_id,),
    )

    return CaseStatusResponse(
        case_id=case_id,
        grievance_id=case_row[0],
        status=case_row[1],
        approval_status=case_row[2],
        grievance_number=case_row[3],
        documents=[DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3]) for d in docs],
    )


async def _recompute_case_status(db: Db, case_id: str, current_case_status: str) -> str:
    rollup = await db.fetchone(
        """SELECT
             SUM(CASE WHEN requires_signature=1 AND status='pending_grievance_number' THEN 1 ELSE 0 END),
             SUM(CASE WHEN requires_signature=1 AND status='sent_for_signature' THEN 1 ELSE 0 END),
             SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END)
           FROM documents
           WHERE case_id=?""",
        (case_id,),
    )
    limbo = int((rollup[0] if rollup else 0) or 0)
    awaiting = int((rollup[1] if rollup else 0) or 0)
    failed = int((rollup[2] if rollup else 0) or 0)

    if awaiting > 0:
        return "awaiting_signatures"
    if limbo > 0:
        return "pending_grievance_number"
    if failed > 0 and current_case_status not in {"approved", "rejected"}:
        return "failed"
    if current_case_status in {"processing", "pending_grievance_number"}:
        return "pending_approval"
    return current_case_status


@router.get("/cases/{case_id}/approval", response_model=ApprovalDecisionResponse)
async def get_approval_status(case_id: str, request: Request):
    db: Db = request.app.state.db
    row = await db.fetchone(
        "SELECT status, approval_status, grievance_number FROM cases WHERE id=?",
        (case_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="case_id not found")
    return ApprovalDecisionResponse(
        case_id=case_id,
        status=row[0],
        approval_status=row[1],
        grievance_number=row[2],
    )


@router.post("/cases/{case_id}/grievance-number", response_model=CaseStatusResponse)
async def assign_grievance_number(case_id: str, body: AssignGrievanceNumberRequest, request: Request):
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    logger = request.app.state.logger
    docuseal = request.app.state.docuseal
    notifications: NotificationService = request.app.state.notifications

    case_row = await db.fetchone(
        "SELECT grievance_id, status, approval_status, member_email FROM cases WHERE id=?",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    grievance_id, current_status, approval_status, member_email = case_row
    if approval_status in {"approved", "rejected"}:
        raise HTTPException(status_code=409, detail="Cannot assign grievance number after final approval decision")

    grievance_number = (body.grievance_number or "").strip()
    if not grievance_number:
        raise HTTPException(status_code=400, detail="grievance_number is required")

    await db.exec(
        "UPDATE cases SET grievance_number=? WHERE id=?",
        (grievance_number, case_id),
    )
    await db.add_event(
        case_id,
        None,
        "grievance_number_assigned",
        {
            "grievance_number": grievance_number,
            "assigned_by": (body.assigned_by or "").strip(),
        },
    )

    queued_docs = await db.fetchall(
        """SELECT id, doc_type, template_key, signer_order_json, pdf_path
           FROM documents
           WHERE case_id=?
             AND requires_signature=1
             AND status='pending_grievance_number'
           ORDER BY created_at_utc""",
        (case_id,),
    )

    dispatched = 0
    failed = 0
    for row in queued_docs:
        document_id, doc_type, template_key, signer_order_json, pdf_path = row
        if not pdf_path or not Path(pdf_path).exists():
            failed += 1
            await db.exec(
                "UPDATE documents SET status='failed' WHERE id=?",
                (document_id,),
            )
            await db.add_event(
                case_id,
                document_id,
                "docuseal_create_failed",
                {"error": "missing_pdf_path", "doc_type": doc_type},
            )
            continue

        signer_order = signer_order_from_json(signer_order_json, member_email)
        outcome = await send_document_for_signature(
            cfg=cfg,
            db=db,
            logger=logger,
            docuseal=docuseal,
            notifications=notifications,
            case_id=case_id,
            grievance_id=grievance_id,
            document_id=document_id,
            doc_type=doc_type,
            template_key=template_key,
            pdf_bytes=Path(pdf_path).read_bytes(),
            alignment_pdf_bytes=None,
            signer_order=signer_order,
            correlation_id=case_id,
            idempotency_prefix=f"grievance_number:{case_id}:{document_id}",
        )
        if outcome.status == "sent_for_signature":
            dispatched += 1
        else:
            failed += 1

    new_case_status = await _recompute_case_status(db, case_id, current_status)
    await db.exec(
        "UPDATE cases SET status=?, grievance_number=? WHERE id=?",
        (new_case_status, grievance_number, case_id),
    )
    await db.add_event(
        case_id,
        None,
        "grievance_number_release_processed",
        {
            "queued_document_count": len(queued_docs),
            "dispatched_for_signature": dispatched,
            "failed_document_count": failed,
            "status": new_case_status,
        },
    )

    if failed > 0:
        logger.warning(
            "grievance_number_release_partial_failure",
            extra={"correlation_id": case_id, "dispatched": dispatched, "failed": failed},
        )

    return await _load_case_status(db, case_id)


@router.post("/cases/{case_id}/approval", response_model=ApprovalDecisionResponse)
async def decide_approval(case_id: str, body: ApprovalDecisionRequest, request: Request):
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    graph = request.app.state.graph
    logger = request.app.state.logger
    notifications: NotificationService = request.app.state.notifications

    case_row = await db.fetchone(
        "SELECT grievance_id, status, approval_status, member_name, member_email, intake_payload_json FROM cases WHERE id=?",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    grievance_id, current_status, _approval_status, member_name, member_email, intake_payload_json = case_row
    folder_member_name = build_case_folder_member_name(
        member_name,
        resolve_contract_label(intake_payload_json),
    )

    if cfg.email.derek_email and body.approver_email.strip().lower() != cfg.email.derek_email.lower():
        raise HTTPException(status_code=403, detail="Only configured approver may approve this case")

    if body.approve:
        new_case_status = "approved"
        new_approval_status = "approved"
        await db.exec(
            """UPDATE cases
               SET status=?, approval_status=?, approver_email=?, approved_at_utc=?, approval_notes=?, grievance_number=?
               WHERE id=?""",
            (
                new_case_status,
                new_approval_status,
                body.approver_email,
                utcnow(),
                body.notes,
                body.grievance_number,
                case_id,
            ),
        )
        await db.exec(
            """UPDATE documents
               SET status='approved'
               WHERE case_id=?
                 AND status IN ('signed', 'pending_approval', 'created', 'pending_grievance_number')""",
            (case_id,),
        )
        await db.add_event(
            case_id,
            None,
            "case_approved",
            {
                "approver_email": body.approver_email,
                "grievance_number": body.grievance_number,
            },
        )

        if cfg.graph.site_hostname and cfg.graph.site_path and cfg.graph.document_library:
            try:
                case_folder = graph.ensure_case_folder(
                    site_hostname=cfg.graph.site_hostname,
                    site_path=cfg.graph.site_path,
                    library=cfg.graph.document_library,
                    case_parent_folder=cfg.graph.case_parent_folder,
                    grievance_id=grievance_id,
                    member_name=folder_member_name,
                )
                await db.exec(
                    "UPDATE cases SET sharepoint_case_folder=?, sharepoint_case_web_url=? WHERE id=?",
                    (case_folder.folder_name, case_folder.web_url, case_id),
                )
                await db.add_event(
                    case_id,
                    None,
                    "sharepoint_upload_target_resolved",
                    {
                        "folder_id": case_folder.folder_id,
                        "folder_name": case_folder.folder_name,
                        "folder_web_url": case_folder.web_url,
                        "case_parent_folder": cfg.graph.case_parent_folder,
                    },
                )

                docs = await db.fetchall(
                    """SELECT id, doc_type, pdf_path, signed_pdf_path, audit_zip_path,
                              sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url,
                              audit_backup_locations_json
                       FROM documents
                       WHERE case_id=?""",
                    (case_id,),
                )

                uploaded_docs = 0
                for row in docs:
                    (
                        document_id,
                        doc_type,
                        pdf_path,
                        signed_pdf_path,
                        audit_zip_path,
                        sp_generated,
                        sp_signed,
                        sp_audit,
                        sp_audit_backups,
                    ) = row
                    new_generated = sp_generated
                    new_signed = sp_signed
                    new_audit = sp_audit
                    new_audit_backups = sp_audit_backups
                    generated_upload_name = f"{doc_type}_{document_id}.pdf"
                    signed_upload_name = f"{doc_type}_{document_id}_signed.pdf"

                    if pdf_path and Path(pdf_path).exists() and not sp_generated:
                        uploaded_generated = graph.upload_to_case_subfolder(
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_folder_name=case_folder.folder_name,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            subfolder=cfg.graph.generated_subfolder,
                            filename=generated_upload_name,
                            file_bytes=Path(pdf_path).read_bytes(),
                        )
                        new_generated = uploaded_generated.web_url
                        await db.add_event(
                            case_id,
                            document_id,
                            "sharepoint_generated_uploaded",
                            {
                                "filename": generated_upload_name,
                                "subfolder": cfg.graph.generated_subfolder,
                                "path": uploaded_generated.path,
                                "web_url": uploaded_generated.web_url,
                            },
                        )

                    if signed_pdf_path and Path(signed_pdf_path).exists() and not sp_signed:
                        uploaded_signed = graph.upload_to_case_subfolder(
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_folder_name=case_folder.folder_name,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            subfolder=cfg.graph.signed_subfolder,
                            filename=signed_upload_name,
                            file_bytes=Path(signed_pdf_path).read_bytes(),
                        )
                        new_signed = uploaded_signed.web_url
                        await db.add_event(
                            case_id,
                            document_id,
                            "sharepoint_signed_uploaded",
                            {
                                "filename": signed_upload_name,
                                "subfolder": cfg.graph.signed_subfolder,
                                "path": uploaded_signed.path,
                                "web_url": uploaded_signed.web_url,
                            },
                        )

                    if audit_zip_path and Path(audit_zip_path).exists():
                        audit_ext = Path(audit_zip_path).suffix or ".zip"
                        audit_upload_name = f"{doc_type}_{document_id}_audit{audit_ext}"
                        backup_outcome = fanout_audit_backups(
                            graph=graph,
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            case_folder_name=case_folder.folder_name,
                            primary_subfolder=cfg.graph.audit_subfolder,
                            extra_subfolders=cfg.graph.audit_backup_subfolders,
                            local_backup_roots=cfg.graph.audit_local_backup_roots,
                            filename=audit_upload_name,
                            file_bytes=Path(audit_zip_path).read_bytes(),
                        )
                        if backup_outcome.primary_web_url:
                            new_audit = backup_outcome.primary_web_url
                        new_audit_backups = merge_backup_locations_json(sp_audit_backups, backup_outcome)
                        await db.add_event(
                            case_id,
                            document_id,
                            "sharepoint_audit_uploaded",
                            {
                                "filename": audit_upload_name,
                                "subfolder": cfg.graph.audit_subfolder,
                                "primary_web_url": backup_outcome.primary_web_url,
                                "sharepoint_copy_count": len(backup_outcome.sharepoint_copies),
                                "local_copy_count": len(backup_outcome.local_paths),
                            },
                        )
                        if backup_outcome.failures:
                            await db.add_event(
                                case_id,
                                document_id,
                                "audit_backup_partial_failure",
                                {
                                    "failure_count": len(backup_outcome.failures),
                                    "destinations": [failure.destination for failure in backup_outcome.failures],
                                },
                            )

                    status_value = "uploaded" if new_generated else "approved"
                    if (
                        (new_generated != sp_generated)
                        or (new_signed != sp_signed)
                        or (new_audit != sp_audit)
                        or (new_audit_backups != sp_audit_backups)
                    ):
                        uploaded_docs += 1
                        await db.exec(
                            """UPDATE documents
                               SET sharepoint_generated_url=?, sharepoint_signed_url=?, sharepoint_audit_url=?,
                                   audit_backup_locations_json=?, status=?
                               WHERE id=?""",
                            (new_generated, new_signed, new_audit, new_audit_backups, status_value, document_id),
                        )

                await db.add_event(
                    case_id,
                    None,
                    "approval_sharepoint_sync_completed",
                    {
                        "uploaded_document_count": uploaded_docs,
                        "case_folder": case_folder.folder_name,
                    },
                )
            except Exception as exc:
                logger.exception("approval_sharepoint_sync_failed", extra={"correlation_id": case_id})
                await db.add_event(
                    case_id,
                    None,
                    "approval_sharepoint_sync_failed",
                    {"error": str(exc)},
                )
    else:
        new_case_status = "rejected"
        new_approval_status = "rejected"
        await db.exec(
            """UPDATE cases
               SET status=?, approval_status=?, approver_email=?, approved_at_utc=?, approval_notes=?
               WHERE id=?""",
            (
                new_case_status,
                new_approval_status,
                body.approver_email,
                utcnow(),
                body.notes,
                case_id,
            ),
        )
        await db.exec(
            "UPDATE documents SET status='failed' WHERE case_id=? AND status NOT IN ('uploaded')",
            (case_id,),
        )
        await db.add_event(
            case_id,
            None,
            "case_rejected",
            {
                "approver_email": body.approver_email,
                "notes": body.notes,
            },
        )

    if cfg.email.enabled:
        recipients = [*cfg.email.internal_recipients]
        if member_email:
            recipients.append(member_email)
        if cfg.email.derek_email:
            recipients.append(cfg.email.derek_email)

        deduped: list[str] = []
        seen: set[str] = set()
        for recipient in recipients:
            lowered = recipient.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            deduped.append(recipient)

        for recipient in deduped:
            try:
                await notifications.send_one(
                    case_id=case_id,
                    template_key="status_update",
                    recipient_email=recipient,
                    context={
                        "case_id": case_id,
                        "grievance_id": grievance_id,
                        "status": new_case_status,
                        "document_type": "case",
                        "docuseal_signing_url": "",
                        "document_link": "",
                        "approval_url": f"{(cfg.email.approval_request_url_base or '').rstrip('/')}/{case_id}",
                    },
                    idempotency_key=f"approval:{case_id}:{new_case_status}:{recipient.lower()}",
                )
            except Exception:
                logger.exception("approval_status_update_email_failed", extra={"correlation_id": case_id})
                await db.add_event(case_id, None, "approval_status_update_email_failed", {"recipient": recipient})

    return ApprovalDecisionResponse(
        case_id=case_id,
        status=new_case_status,
        approval_status=new_approval_status,
        grievance_number=body.grievance_number,
    )
