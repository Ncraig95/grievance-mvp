from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..db.db import Db, utcnow
from ..services.case_folder_naming import build_case_folder_member_name, resolve_contract_label
from ..services.graph_mail import MailAttachment
from ..services.notification_service import NotificationService

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
    return any(token in et for token in ("completed", "finished", "done"))


def _resolve_submission_id(payload: dict) -> str | None:
    candidates = [
        payload.get("submission_id"),
        payload.get("submissionId"),
        payload.get("id"),
    ]
    for container_key in ("submission", "data"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            candidates.extend([container.get("submission_id"), container.get("submissionId"), container.get("id")])

    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _find_signing_url(payload: dict) -> str:
    for key in ("signing_url", "submitter_url", "submission_url", "url"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    submission = payload.get("submission")
    if isinstance(submission, dict):
        for key in ("signing_url", "submitter_url", "url"):
            val = submission.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _extract_first_pdf(zip_bytes: bytes) -> tuple[str, bytes] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".pdf"):
                    return name, zf.read(name)
    except Exception:
        return None
    return None


def _approval_url(base: str | None, case_id: str) -> str:
    if not base:
        return ""
    return f"{base.rstrip('/')}/{case_id}"


def verify_docuseal_webhook(raw_body: bytes, header_sig: str | None, secret: str) -> None:
    normalized_secret = (secret or "").strip()
    if not normalized_secret or normalized_secret.upper().startswith("REPLACE"):
        return
    if not header_sig:
        raise ValueError("Missing webhook signature header")

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
    docuseal = request.app.state.docuseal
    graph = request.app.state.graph
    notifications: NotificationService = request.app.state.notifications

    raw = await request.body()

    signature_header = request.headers.get("X-DocuSeal-Signature") or request.headers.get("X-Signature")
    try:
        verify_docuseal_webhook(raw, signature_header, cfg.docuseal.webhook_secret)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = _parse_json(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON")

    submission_id = _resolve_submission_id(payload)
    receipt_key = str(payload.get("event_id") or submission_id or hashlib.sha256(raw).hexdigest())

    if await db.receipt_seen("docuseal", receipt_key):
        logger.info("webhook_deduped", extra={"correlation_id": receipt_key})
        return {"ok": True, "deduped": True}

    await db.store_receipt("docuseal", receipt_key, raw.decode("utf-8"))

    if not submission_id:
        await db.mark_receipt_handled("docuseal", receipt_key)
        return {"ok": True, "handled": False, "reason": "missing_submission_id"}

    row = await db.fetchone(
        """SELECT d.id, d.case_id, d.doc_type, d.signer_order_json, d.pdf_path, d.docuseal_signing_link,
                  c.grievance_id, c.member_name, c.member_email, c.intake_payload_json
           FROM documents d
           JOIN cases c ON c.id = d.case_id
           WHERE d.docuseal_submission_id=?""",
        (submission_id,),
    )
    if not row:
        await db.mark_receipt_handled("docuseal", receipt_key)
        logger.warning("docuseal_webhook_unknown_submission", extra={"correlation_id": submission_id})
        return {"ok": True, "handled": False, "reason": "unknown_submission"}

    (
        document_id,
        case_id,
        doc_type,
        signer_order_json,
        pdf_path,
        signing_link,
        grievance_id,
        member_name,
        member_email,
        intake_payload_json,
    ) = row
    folder_member_name = build_case_folder_member_name(
        member_name,
        resolve_contract_label(intake_payload_json),
    )

    await db.add_event(case_id, document_id, "docuseal_webhook_received", {"event_type": _event_type(payload), "receipt_key": receipt_key})

    if not _is_completion_event(payload):
        await db.mark_receipt_handled("docuseal", receipt_key)
        return {"ok": True, "handled": False, "reason": "non_completion_event"}

    case_dir = Path(cfg.data_root) / case_id
    doc_dir = case_dir / document_id
    doc_dir.mkdir(parents=True, exist_ok=True)

    signed_pdf_bytes: bytes | None = None
    signed_pdf_path: str | None = None
    audit_zip_path: str | None = None

    try:
        artifacts = docuseal.download_completed_artifacts(submission_id=submission_id)
        zip_bytes = artifacts.get("completed_zip_bytes")
        if isinstance(zip_bytes, (bytes, bytearray)) and len(zip_bytes) > 0:
            audit_zip_path = str(doc_dir / "docuseal_completed.zip")
            Path(audit_zip_path).write_bytes(bytes(zip_bytes))
            extracted = _extract_first_pdf(bytes(zip_bytes))
            if extracted:
                _, signed_pdf_bytes = extracted
                signed_pdf_path = str(doc_dir / "signed.pdf")
                Path(signed_pdf_path).write_bytes(signed_pdf_bytes)
        await db.add_event(case_id, document_id, "docuseal_artifacts_downloaded", {})
    except Exception as exc:
        await db.add_event(case_id, document_id, "docuseal_artifact_download_failed", {"error": str(exc)})
        logger.exception("docuseal_artifact_download_failed", extra={"correlation_id": case_id, "document_id": document_id})

    if signed_pdf_bytes is None and pdf_path and Path(pdf_path).exists():
        signed_pdf_bytes = Path(pdf_path).read_bytes()
        signed_pdf_path = pdf_path

    sharepoint_generated_url: str | None = None
    sharepoint_signed_url: str | None = None
    sharepoint_audit_url: str | None = None
    sharepoint_case_folder: str | None = None
    sharepoint_case_web_url: str | None = None

    generated_pdf_path = Path(pdf_path) if pdf_path else None
    try:
        if cfg.graph.site_hostname and cfg.graph.site_path and cfg.graph.document_library:
            case_folder = graph.ensure_case_folder(
                site_hostname=cfg.graph.site_hostname,
                site_path=cfg.graph.site_path,
                library=cfg.graph.document_library,
                case_parent_folder=cfg.graph.case_parent_folder,
                grievance_id=grievance_id,
                member_name=folder_member_name,
            )
            sharepoint_case_folder = case_folder.folder_name
            sharepoint_case_web_url = case_folder.web_url

            if generated_pdf_path and generated_pdf_path.exists():
                uploaded_generated = graph.upload_to_case_subfolder(
                    site_hostname=cfg.graph.site_hostname,
                    site_path=cfg.graph.site_path,
                    library=cfg.graph.document_library,
                    case_folder_name=case_folder.folder_name,
                    case_parent_folder=cfg.graph.case_parent_folder,
                    subfolder=cfg.graph.generated_subfolder,
                    filename=f"{doc_type}.pdf",
                    file_bytes=generated_pdf_path.read_bytes(),
                )
                sharepoint_generated_url = uploaded_generated.web_url

            if signed_pdf_bytes:
                uploaded_signed = graph.upload_to_case_subfolder(
                    site_hostname=cfg.graph.site_hostname,
                    site_path=cfg.graph.site_path,
                    library=cfg.graph.document_library,
                    case_folder_name=case_folder.folder_name,
                    case_parent_folder=cfg.graph.case_parent_folder,
                    subfolder=cfg.graph.signed_subfolder,
                    filename=f"{doc_type}_signed.pdf",
                    file_bytes=signed_pdf_bytes,
                )
                sharepoint_signed_url = uploaded_signed.web_url

            if audit_zip_path and Path(audit_zip_path).exists():
                uploaded_audit = graph.upload_to_case_subfolder(
                    site_hostname=cfg.graph.site_hostname,
                    site_path=cfg.graph.site_path,
                    library=cfg.graph.document_library,
                    case_folder_name=case_folder.folder_name,
                    case_parent_folder=cfg.graph.case_parent_folder,
                    subfolder=cfg.graph.audit_subfolder,
                    filename=f"{doc_type}_audit.zip",
                    file_bytes=Path(audit_zip_path).read_bytes(),
                )
                sharepoint_audit_url = uploaded_audit.web_url
    except Exception as exc:
        await db.add_event(case_id, document_id, "sharepoint_upload_failed", {"error": str(exc)})
        logger.exception("sharepoint_upload_failed", extra={"correlation_id": case_id, "document_id": document_id})

    await db.exec(
        """UPDATE documents
           SET status=?, completed_at_utc=?, signed_pdf_path=?, audit_zip_path=?,
               sharepoint_generated_url=?, sharepoint_signed_url=?, sharepoint_audit_url=?
           WHERE id=?""",
        (
            "signed",
            utcnow(),
            signed_pdf_path,
            audit_zip_path,
            sharepoint_generated_url,
            sharepoint_signed_url,
            sharepoint_audit_url,
            document_id,
        ),
    )

    if sharepoint_case_folder or sharepoint_case_web_url:
        await db.exec(
            "UPDATE cases SET sharepoint_case_folder=?, sharepoint_case_web_url=? WHERE id=?",
            (sharepoint_case_folder, sharepoint_case_web_url, case_id),
        )

    signer_emails: list[str] = []
    if signer_order_json:
        try:
            parsed = json.loads(signer_order_json)
            if isinstance(parsed, list):
                signer_emails = [str(s).strip() for s in parsed if str(s).strip()]
        except Exception:
            signer_emails = []
    if not signer_emails and member_email:
        signer_emails = [member_email]

    docuseal_signing_url = signing_link or _find_signing_url(payload)
    approval_url = _approval_url(cfg.email.approval_request_url_base, case_id)
    document_link = sharepoint_signed_url or sharepoint_generated_url or docuseal_signing_url

    attachments: list[MailAttachment] | None = None
    if (
        cfg.email.artifact_delivery_mode == "attach_pdf"
        and signed_pdf_bytes
        and len(signed_pdf_bytes) <= cfg.email.max_attachment_bytes
    ):
        attachments = [
            MailAttachment(
                filename=f"{doc_type}_signed.pdf",
                content_type="application/pdf",
                content_bytes=signed_pdf_bytes,
            )
        ]

    common_context = {
        "case_id": case_id,
        "grievance_id": grievance_id,
        "document_id": document_id,
        "document_type": doc_type,
        "docuseal_signing_url": docuseal_signing_url,
        "document_link": document_link,
        "copy_link": document_link if cfg.email.allow_signer_copy_link else "Not permitted",
        "approval_url": approval_url,
        "status": "signed",
        "completed_at_utc": utcnow(),
    }

    if cfg.email.enabled:
        for signer in signer_emails:
            await notifications.send_one(
                case_id=case_id,
                document_id=document_id,
                recipient_email=signer,
                template_key="completion_signer",
                context={**common_context, "signer_email": signer},
                idempotency_key=f"docuseal:{receipt_key}:completion_signer:{signer.lower()}",
                attachments=attachments if cfg.email.allow_signer_copy_link else None,
            )

        for recipient in cfg.email.internal_recipients:
            await notifications.send_one(
                case_id=case_id,
                document_id=document_id,
                recipient_email=recipient,
                template_key="completion_internal",
                context=common_context,
                idempotency_key=f"docuseal:{receipt_key}:completion_internal:{recipient.lower()}",
                attachments=attachments,
            )

        if cfg.email.derek_email:
            await notifications.send_one(
                case_id=case_id,
                document_id=document_id,
                recipient_email=cfg.email.derek_email,
                template_key="completion_approval",
                context=common_context,
                idempotency_key=f"docuseal:{receipt_key}:completion_approval:{cfg.email.derek_email.lower()}",
                attachments=attachments,
            )

    remaining = await db.fetchone(
        """SELECT COUNT(1)
           FROM documents
           WHERE case_id=?
             AND requires_signature=1
             AND status NOT IN ('signed', 'pending_approval', 'approved', 'uploaded')""",
        (case_id,),
    )
    if remaining and int(remaining[0]) == 0:
        await db.exec(
            "UPDATE cases SET status='pending_approval', approval_status='pending' WHERE id=?",
            (case_id,),
        )

    await db.add_event(case_id, document_id, "docuseal_completion_processed", {"receipt_key": receipt_key})
    await db.mark_receipt_handled("docuseal", receipt_key)
    logger.info("docuseal_completion_processed", extra={"correlation_id": case_id, "document_id": document_id})
    return {"ok": True, "handled": True}
