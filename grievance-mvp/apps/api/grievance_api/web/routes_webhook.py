from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import requests
from fastapi import APIRouter, Request, HTTPException

from ..db.db import Db, utcnow
from ..services.graph_mail import MailAttachment
from ..services.notification_service import NotificationService
from ..services.sharepoint_graph import GraphUploader

router = APIRouter()


def _parse_json(raw_body: bytes) -> dict:
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a JSON object")
    return payload


def _event_type(payload: dict) -> str:
    for key in ("event", "event_type", "type", "status"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return ""


def _is_completion_event(payload: dict) -> bool:
    et = _event_type(payload)
    completed_tokens = ("completed", "done", "finished")
    return any(tok in et for tok in completed_tokens)


def _resolve_submission_id(payload: dict) -> str | None:
    candidates = [
        payload.get("submission_id"),
        payload.get("submissionId"),
        payload.get("id"),
    ]
    sub_obj = payload.get("submission")
    if isinstance(sub_obj, dict):
        candidates.append(sub_obj.get("id"))
    data_obj = payload.get("data")
    if isinstance(data_obj, dict):
        candidates.extend([data_obj.get("submission_id"), data_obj.get("submissionId"), data_obj.get("id")])
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _resolve_grievance_id(payload: dict) -> str | None:
    direct = payload.get("grievance_id")
    if direct:
        return str(direct)
    meta = payload.get("metadata")
    if isinstance(meta, dict) and meta.get("grievance_id"):
        return str(meta.get("grievance_id"))
    data = payload.get("data")
    if isinstance(data, dict) and data.get("grievance_id"):
        return str(data.get("grievance_id"))
    return None


def _find_document_link(payload: dict) -> str:
    for key in ("signed_pdf_url", "completed_pdf_url", "download_url", "file_url"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    documents = payload.get("documents")
    if isinstance(documents, list):
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            for key in ("download_url", "url", "file_url"):
                val = doc.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return ""


def _find_signing_link(payload: dict) -> str:
    for key in ("signing_url", "submission_url", "submitter_url", "url"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    submission = payload.get("submission")
    if isinstance(submission, dict):
        for key in ("url", "signing_url", "submitter_url"):
            val = submission.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _download_pdf_bytes(url: str) -> bytes | None:
    if not url:
        return None
    try:
        r = requests.get(url, timeout=30)
        if 200 <= r.status_code < 300 and r.content:
            return r.content
    except Exception:
        return None
    return None


def _approval_url(base: str | None, grievance_id: str) -> str:
    if not base:
        return ""
    return f"{base.rstrip('/')}/{grievance_id}"


def verify_docuseal_webhook(raw_body: bytes, header_sig: str | None, secret: str) -> None:
    """Best-effort HMAC verification. Disabled when secret is empty or placeholder."""
    normalized_secret = (secret or "").strip()
    if not normalized_secret or normalized_secret.upper().startswith("REPLACE"):
        return
    if not header_sig:
        raise ValueError("Missing signature header")

    provided = header_sig.strip()
    if "=" in provided:
        provided = provided.split("=", 1)[1]
    expected = hmac.new(normalized_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided.lower(), expected.lower()):
        raise ValueError("Signature mismatch")


@router.post("/webhook/docuseal")
async def webhook_docuseal(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger
    notifications: NotificationService = request.app.state.notifications
    graph: GraphUploader = request.app.state.graph

    raw = await request.body()

    try:
        verify_docuseal_webhook(raw, request.headers.get("X-Signature"), cfg.docuseal.webhook_secret)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = _parse_json(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON")

    receipt_key = str(payload.get("event_id") or payload.get("id") or hashlib.sha256(raw).hexdigest())

    if await db.receipt_seen("docuseal", receipt_key):
        logger.info("webhook_deduped", extra={"correlation_id": receipt_key})
        return {"ok": True, "deduped": True}

    await db.store_receipt("docuseal", receipt_key, raw.decode("utf-8"))

    grievance_id = _resolve_grievance_id(payload)
    submission_id = _resolve_submission_id(payload)
    if not grievance_id and submission_id:
        row = await db.fetchone("SELECT id FROM grievances WHERE docuseal_submission_id=?", (submission_id,))
        if row:
            grievance_id = row[0]
    if not grievance_id:
        raise HTTPException(status_code=400, detail="Could not resolve grievance_id")

    await db.add_event(grievance_id, "docuseal_webhook_received", {"receipt_key": receipt_key})
    logger.info("docuseal_webhook_received", extra={"correlation_id": grievance_id})

    if not _is_completion_event(payload):
        await db.mark_receipt_handled("docuseal", receipt_key)
        return {"ok": True, "handled": False, "reason": "non_completion_event"}

    grievance = await db.fetchone(
        """SELECT signer_email, signer_lastname, pdf_path, docuseal_signing_link
           FROM grievances WHERE id=?""",
        (grievance_id,),
    )
    if not grievance:
        raise HTTPException(status_code=404, detail="grievance not found")
    signer_email, signer_lastname, pdf_path, signing_url = grievance

    pdf_bytes: bytes | None = None
    docuseal_document_link = _find_document_link(payload)
    if docuseal_document_link:
        pdf_bytes = _download_pdf_bytes(docuseal_document_link)
    if pdf_bytes is None and pdf_path:
        p = Path(pdf_path)
        if p.exists():
            pdf_bytes = p.read_bytes()

    document_link = docuseal_document_link
    attachments: list[MailAttachment] | None = None

    if cfg.email.artifact_delivery_mode == "sharepoint_link" and pdf_bytes:
        try:
            upload = graph.upload_to_sharepoint_path(
                site_hostname=cfg.graph.site_hostname,
                site_path=cfg.graph.site_path,
                library=cfg.graph.document_library,
                folder_path=f"grievances/{grievance_id}",
                filename=f"{grievance_id}.pdf",
                file_bytes=pdf_bytes,
            )
            if upload.web_url:
                document_link = upload.web_url
        except Exception:
            await db.add_event(grievance_id, "sharepoint_upload_failed", {})
            logger.exception("sharepoint_upload_failed", extra={"correlation_id": grievance_id})
    elif cfg.email.artifact_delivery_mode == "attach_pdf" and pdf_bytes:
        if len(pdf_bytes) <= cfg.email.max_attachment_bytes:
            attachments = [
                MailAttachment(
                    filename=f"{grievance_id}.pdf",
                    content_type="application/pdf",
                    content_bytes=pdf_bytes,
                )
            ]

    approval_url = _approval_url(cfg.email.approval_request_url_base, grievance_id)
    signing_record_url = signing_url or _find_signing_link(payload) or (document_link or "")
    common_context = {
        "grievance_id": grievance_id,
        "signer_email": signer_email,
        "signer_lastname": signer_lastname,
        "document_link": document_link or "",
        "copy_link": (document_link or "") if cfg.email.allow_signer_copy_link else "Not permitted",
        "approval_url": approval_url,
        "docuseal_signing_url": signing_record_url,
        "completed_at_utc": utcnow(),
    }

    if cfg.email.enabled:
        await notifications.send_one(
            grievance_id=grievance_id,
            recipient_email=signer_email,
            template_key="completion_signer",
            context=common_context,
            idempotency_key=f"docuseal:{receipt_key}:completion_signer:{signer_email.lower()}",
            attachments=attachments if cfg.email.allow_signer_copy_link else None,
        )
        for recipient in cfg.email.internal_recipients:
            await notifications.send_one(
                grievance_id=grievance_id,
                recipient_email=recipient,
                template_key="completion_internal",
                context=common_context,
                idempotency_key=f"docuseal:{receipt_key}:completion_internal:{recipient.lower()}",
                attachments=attachments,
            )
        if cfg.email.derek_email:
            await notifications.send_one(
                grievance_id=grievance_id,
                recipient_email=cfg.email.derek_email,
                template_key="completion_approval",
                context=common_context,
                idempotency_key=f"docuseal:{receipt_key}:completion_approval:{cfg.email.derek_email.lower()}",
                attachments=attachments,
            )

    await db.exec(
        "UPDATE grievances SET status=?, completed_at_utc=? WHERE id=?",
        ("completed", utcnow(), grievance_id),
    )
    await db.add_event(
        grievance_id,
        "docuseal_completion_processed",
        {"receipt_key": receipt_key, "document_link_present": bool(document_link)},
    )
    await db.mark_receipt_handled("docuseal", receipt_key)
    logger.info("docuseal_completion_processed", extra={"correlation_id": grievance_id})
    return {"ok": True, "handled": True}
