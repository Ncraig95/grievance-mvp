from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..db.db import Db
from ..services.contract_timeline import calculate_deadline, deadline_days_for_contract, resolve_contract_and_incident_date
from ..services.graph_mail import MailAttachment
from ..services.notification_service import NotificationService
from .models import ResendNotificationRequest, ResendNotificationResult

router = APIRouter()


def _approval_url(base: str | None, case_id: str) -> str:
    if not base:
        return ""
    return f"{base.rstrip('/')}/{case_id}"


def _parse_signers(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
        if not isinstance(values, list):
            return []
        return [str(v).strip() for v in values if str(v).strip()]
    except Exception:
        return []


def _build_signed_pdf_attachment(
    *,
    cfg,  # noqa: ANN001
    doc_type: str,
    signed_pdf_path: str | None,
) -> list[MailAttachment] | None:
    path = Path(str(signed_pdf_path or "").strip())
    if not path.exists() or not path.is_file():
        return None
    size_bytes = path.stat().st_size
    if size_bytes <= 0 or size_bytes > cfg.email.max_attachment_bytes:
        return None
    return [
        MailAttachment(
            filename=f"{doc_type}_signed.pdf",
            content_type="application/pdf",
            content_bytes=path.read_bytes(),
        )
    ]


@router.post(
    "/cases/{case_id}/notifications/resend",
    response_model=list[ResendNotificationResult],
)
async def resend_notification(case_id: str, body: ResendNotificationRequest, request: Request):
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    logger = request.app.state.logger
    notifications: NotificationService = request.app.state.notifications

    case_row = await db.fetchone(
        "SELECT grievance_id, grievance_number, member_name, member_email, status, intake_payload_json FROM cases WHERE id=?",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    grievance_id, grievance_number, member_name, member_email, case_status, intake_payload_json = case_row
    contract_label, incident_dt = resolve_contract_and_incident_date(intake_payload_json)
    deadline_days = deadline_days_for_contract(contract_label)
    deadline_dt = calculate_deadline(incident_dt, deadline_days)

    document_id = body.document_id
    doc_type = ""
    signing_url = ""
    document_link = ""
    signer_order_json = ""
    signed_pdf_path = ""
    if document_id:
        doc_row = await db.fetchone(
            """SELECT doc_type, docuseal_signing_link, COALESCE(sharepoint_signed_url, sharepoint_generated_url, ''), signer_order_json,
                      COALESCE(signed_pdf_path, pdf_path, '')
               FROM documents WHERE id=? AND case_id=?""",
            (document_id, case_id),
        )
        if not doc_row:
            raise HTTPException(status_code=404, detail="document_id not found for case")
        doc_type, signing_url, document_link, signer_order_json, signed_pdf_path = doc_row

    template_key = body.template_key.strip()
    if not template_key:
        raise HTTPException(status_code=400, detail="template_key is required")

    recipients = [r.strip() for r in (body.recipients or []) if r.strip()]
    if not recipients:
        signers = _parse_signers(signer_order_json)
        if template_key in {"signature_request", "reminder_signature", "completion_signer"}:
            recipients = signers or ([member_email] if member_email else [])
        elif template_key == "completion_internal":
            recipients = list(cfg.email.internal_recipients)
        elif template_key == "completion_approval":
            recipients = [cfg.email.derek_email] if cfg.email.derek_email else []
        elif template_key == "status_update":
            recipients = [*(signers or ([member_email] if member_email else [])), *cfg.email.internal_recipients]
            if cfg.email.derek_email:
                recipients.append(cfg.email.derek_email)

    deduped: list[str] = []
    seen: set[str] = set()
    for recipient in recipients:
        key = recipient.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(recipient)
    recipients = deduped

    if not recipients:
        raise HTTPException(status_code=400, detail="No recipients available for this template")

    base_context = {
        "case_id": case_id,
        "grievance_id": grievance_id,
        "projected_grievance_number": grievance_number or grievance_id,
        "contract_name": contract_label or "",
        "incident_date": incident_dt.isoformat() if incident_dt else "",
        "deadline_days": str(deadline_days or ""),
        "deadline_date": deadline_dt.isoformat() if deadline_dt else "",
        "document_id": document_id or "",
        "document_type": doc_type,
        "member_name": member_name,
        "signer_email": member_email or "",
        "docuseal_signing_url": signing_url,
        "document_link": document_link,
        "copy_link": document_link if cfg.email.allow_signer_copy_link else "Not permitted",
        "approval_url": _approval_url(cfg.email.approval_request_url_base, case_id),
        "status": case_status,
    }
    base_context.update(body.context_overrides)

    signed_pdf_attachments = _build_signed_pdf_attachment(
        cfg=cfg,
        doc_type=doc_type,
        signed_pdf_path=signed_pdf_path,
    )

    resend_attachments: list[MailAttachment] | None = None
    if template_key == "completion_signer":
        # Always try to include signed artifact copy for signer completion emails.
        resend_attachments = signed_pdf_attachments
    elif template_key in {"completion_internal", "completion_approval"} and cfg.email.artifact_delivery_mode == "attach_pdf":
        resend_attachments = signed_pdf_attachments

    out: list[ResendNotificationResult] = []
    for recipient in recipients:
        idem = f"{body.idempotency_key}:{template_key}:{(document_id or '')}:{recipient.lower()}"
        try:
            result = await notifications.send_one(
                case_id=case_id,
                document_id=document_id,
                recipient_email=recipient,
                template_key=template_key,
                context=base_context,
                idempotency_key=idem,
                allow_resend=True,
                attachments=resend_attachments,
            )
            out.append(
                ResendNotificationResult(
                    recipient_email=result.recipient_email,
                    status=result.status,
                    deduped=result.deduped,
                    graph_message_id=result.graph_message_id,
                    resend_count=result.resend_count,
                )
            )
        except RuntimeError as exc:
            message = str(exc)
            if "resend cooldown active" in message:
                raise HTTPException(status_code=429, detail=message) from exc
            if "email delivery disabled" in message or "Graph mailer is not configured" in message:
                raise HTTPException(status_code=503, detail=message) from exc
            logger.exception("notification_resend_failed", extra={"correlation_id": case_id, "document_id": document_id})
            raise HTTPException(status_code=400, detail=f"resend failed for {recipient}: {message}") from exc
        except Exception as exc:
            logger.exception("notification_resend_failed", extra={"correlation_id": case_id, "document_id": document_id})
            raise HTTPException(status_code=500, detail=f"resend failed for {recipient}: {exc}") from exc

    return out
