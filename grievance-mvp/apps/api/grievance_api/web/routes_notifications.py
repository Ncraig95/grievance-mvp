from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from ..db.db import Db
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
        "SELECT grievance_id, member_name, member_email, status FROM cases WHERE id=?",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    grievance_id, member_name, member_email, case_status = case_row

    document_id = body.document_id
    doc_type = ""
    signing_url = ""
    document_link = ""
    signer_order_json = ""
    if document_id:
        doc_row = await db.fetchone(
            """SELECT doc_type, docuseal_signing_link, COALESCE(sharepoint_signed_url, sharepoint_generated_url, ''), signer_order_json
               FROM documents WHERE id=? AND case_id=?""",
            (document_id, case_id),
        )
        if not doc_row:
            raise HTTPException(status_code=404, detail="document_id not found for case")
        doc_type, signing_url, document_link, signer_order_json = doc_row

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
