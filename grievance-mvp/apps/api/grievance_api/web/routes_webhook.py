from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from pathlib import Path
from typing import Mapping

import requests
from fastapi import APIRouter, HTTPException, Request

from ..db.db import Db, utcnow
from ..services.audit_backups import fanout_audit_backups, merge_backup_locations_json
from ..services.case_folder_naming import build_case_folder_member_name, resolve_contract_label
from ..services.contract_timeline import calculate_deadline, deadline_days_for_contract, resolve_contract_and_incident_date
from ..services.graph_mail import MailAttachment
from ..services.notification_service import NotificationService
from ..services.staged_signature_workflow import (
    create_or_send_stage,
    is_3g3a_staged,
    record_stage_artifact,
    stage_alignment_pdf_path,
    stage_file_path,
)

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


def _build_receipt_key(payload: dict, raw_body: bytes, submission_id: str | None) -> str:
    for key in ("event_id", "eventId"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return f"event:{text}"

    # Do not use submission_id as the dedupe key by itself.
    # Different events (form.viewed vs submission.completed) share a submission id.
    _ = submission_id
    return f"sha256:{hashlib.sha256(raw_body).hexdigest()}"


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


def _iter_document_urls(payload: dict) -> list[str]:
    urls: list[str] = []
    stack: list[object] = [payload]
    seen: set[int] = set()

    while stack:
        cur = stack.pop()
        cur_id = id(cur)
        if cur_id in seen:
            continue
        seen.add(cur_id)

        if isinstance(cur, dict):
            name = str(cur.get("name") or "").lower()
            url_val = cur.get("url")
            if isinstance(url_val, str) and url_val.strip():
                if "audit" not in name and url_val.lower().rstrip().endswith(".pdf"):
                    urls.append(url_val.strip())
            for value in cur.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            for item in cur:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return urls


def _find_audit_url(payload: dict) -> str:
    stack: list[object] = [payload]
    seen: set[int] = set()

    while stack:
        cur = stack.pop()
        cur_id = id(cur)
        if cur_id in seen:
            continue
        seen.add(cur_id)

        if isinstance(cur, dict):
            for key in ("audit_log_url", "auditUrl", "audit_url"):
                val = cur.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for value in cur.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            for item in cur:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return ""


def _download_public_bytes(url: str, *, timeout: int = 30) -> bytes | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception:
        return None
    if 200 <= resp.status_code < 300 and resp.content:
        return resp.content
    return None


def _approval_url(base: str | None, case_id: str) -> str:
    if not base:
        return ""
    return f"{base.rstrip('/')}/{case_id}"


def _extract_bearer_token(headers: Mapping[str, str]) -> str | None:
    auth_header = headers.get("Authorization")
    if not auth_header:
        return None
    prefix = "bearer "
    if auth_header.lower().startswith(prefix):
        token = auth_header[len(prefix) :].strip()
        return token or None
    return None


def verify_docuseal_webhook(raw_body: bytes, headers: Mapping[str, str], secret: str) -> None:
    normalized_secret = (secret or "").strip()
    if not normalized_secret or normalized_secret.upper().startswith("REPLACE"):
        return

    signature_header = headers.get("X-DocuSeal-Signature") or headers.get("X-Signature")
    if signature_header:
        provided = signature_header.strip()
        if "=" in provided:
            provided = provided.split("=", 1)[1]

        expected = hmac.new(normalized_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided.lower(), expected.lower()):
            raise ValueError("Signature mismatch")
        return

    token_header = (
        headers.get("X-Webhook-Token")
        or headers.get("X-DocuSeal-Webhook-Token")
        or headers.get("X-Webhook-Secret")
        or headers.get("X-DocuSeal-Secret")
        or _extract_bearer_token(headers)
    )
    if token_header and hmac.compare_digest(token_header.strip(), normalized_secret):
        return

    raise ValueError("Missing or invalid webhook authentication")


@router.post("/webhook/docuseal")
async def webhook_docuseal(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger
    docuseal = request.app.state.docuseal
    graph = request.app.state.graph
    notifications: NotificationService = request.app.state.notifications

    raw = await request.body()

    try:
        verify_docuseal_webhook(raw, request.headers, cfg.docuseal.webhook_secret)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = _parse_json(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON")

    submission_id = _resolve_submission_id(payload)
    receipt_key = _build_receipt_key(payload, raw, submission_id)

    if not await db.try_claim_receipt("docuseal", receipt_key, raw.decode("utf-8")):
        logger.info("webhook_deduped", extra={"correlation_id": receipt_key})
        return {"ok": True, "deduped": True}

    if not submission_id:
        await db.mark_receipt_handled("docuseal", receipt_key)
        return {"ok": True, "handled": False, "reason": "missing_submission_id"}

    row = await db.fetchone(
        """SELECT d.id, d.case_id, d.doc_type, d.template_key, d.signer_order_json, d.pdf_path, d.docuseal_signing_link,
                  c.grievance_id, c.grievance_number, c.member_name, c.member_email, c.intake_payload_json,
                  c.sharepoint_case_folder, c.sharepoint_case_web_url
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
        template_key,
        signer_order_json,
        pdf_path,
        signing_link,
        grievance_id,
        grievance_number,
        member_name,
        member_email,
        intake_payload_json,
        existing_case_folder_name,
        existing_case_folder_web_url,
    ) = row
    contract_label, incident_dt = resolve_contract_and_incident_date(intake_payload_json)
    if not contract_label:
        contract_label = resolve_contract_label(intake_payload_json)
    deadline_days = deadline_days_for_contract(contract_label)
    deadline_dt = calculate_deadline(incident_dt, deadline_days)
    projected_grievance_number = grievance_number or grievance_id
    folder_member_name = build_case_folder_member_name(
        member_name,
        resolve_contract_label(intake_payload_json),
    )

    await db.add_event(case_id, document_id, "docuseal_webhook_received", {"event_type": _event_type(payload), "receipt_key": receipt_key})

    if not _is_completion_event(payload):
        await db.mark_receipt_handled("docuseal", receipt_key)
        return {"ok": True, "handled": False, "reason": "non_completion_event"}

    completion_receipt_key = f"completion:{document_id}:{submission_id}"
    completion_claim_raw = json.dumps(
        {"source_receipt_key": receipt_key, "event_type": _event_type(payload)},
        ensure_ascii=False,
    )
    if not await db.try_claim_receipt("docuseal", completion_receipt_key, completion_claim_raw):
        await db.mark_receipt_handled("docuseal", receipt_key)
        logger.info(
            "docuseal_completion_deduped",
            extra={"correlation_id": case_id, "document_id": document_id},
        )
        return {"ok": True, "deduped": True, "reason": "completion_already_processed"}
    try:
        stage_row = await db.get_document_stage_by_submission(submission_id=submission_id)
        if stage_row and is_3g3a_staged(cfg=cfg, doc_type=doc_type, template_key=template_key):
            stage_id = int(stage_row[0])
            stage_no = int(stage_row[3])
            stage_status = str(stage_row[5] or "")
            if stage_status == "completed":
                await db.mark_receipt_handled("docuseal", completion_receipt_key)
                await db.mark_receipt_handled("docuseal", receipt_key)
                return {"ok": True, "deduped": True, "reason": "stage_already_completed"}

            case_dir = Path(cfg.data_root) / case_id
            doc_dir = case_dir / document_id
            doc_dir.mkdir(parents=True, exist_ok=True)
            stage_dir = doc_dir / "Stages" / f"Stage{stage_no}"
            stage_dir.mkdir(parents=True, exist_ok=True)

            staged_signed_bytes: bytes | None = None
            staged_audit_bytes: bytes | None = None
            staged_audit_ext = ".zip"
            submission_details: dict | None = None

            try:
                artifacts = docuseal.download_completed_artifacts(submission_id=submission_id)
                maybe_submission = artifacts.get("submission")
                if isinstance(maybe_submission, dict):
                    submission_details = maybe_submission
                zip_bytes = artifacts.get("completed_zip_bytes")
                if isinstance(zip_bytes, (bytes, bytearray)) and len(zip_bytes) > 0:
                    staged_audit_bytes = bytes(zip_bytes)
                    extracted = _extract_first_pdf(staged_audit_bytes)
                    if extracted:
                        _, staged_signed_bytes = extracted
            except Exception as exc:
                await db.add_event(
                    case_id,
                    document_id,
                    "document_stage_artifact_download_failed",
                    {"stage_no": stage_no, "error": str(exc)},
                )

            if staged_signed_bytes is None:
                doc_urls = _iter_document_urls(payload)
                if not doc_urls and submission_details:
                    doc_urls = _iter_document_urls(submission_details)
                if doc_urls:
                    staged_signed_bytes = _download_public_bytes(doc_urls[0])

            if staged_audit_bytes is None:
                audit_url = _find_audit_url(payload)
                if not audit_url and submission_details:
                    audit_url = _find_audit_url(submission_details)
                downloaded_audit = _download_public_bytes(audit_url)
                if downloaded_audit:
                    staged_audit_bytes = downloaded_audit
                    staged_audit_ext = ".zip" if downloaded_audit[:2] == b"PK" else ".pdf"

            if staged_signed_bytes:
                signed_path = stage_file_path(
                    case_dir=case_dir,
                    document_id=document_id,
                    stage_no=stage_no,
                    filename=f"{doc_type}_stage{stage_no}_signed.pdf",
                )
                signed_path.parent.mkdir(parents=True, exist_ok=True)
                signed_path.write_bytes(staged_signed_bytes)
                await record_stage_artifact(
                    db=db,
                    stage_id=stage_id,
                    artifact_type="pdf_signed",
                    storage_backend="local",
                    storage_path=str(signed_path),
                    content_bytes=staged_signed_bytes,
                )

            if staged_audit_bytes:
                audit_path = stage_file_path(
                    case_dir=case_dir,
                    document_id=document_id,
                    stage_no=stage_no,
                    filename=f"{doc_type}_stage{stage_no}_audit{staged_audit_ext}",
                )
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                audit_path.write_bytes(staged_audit_bytes)
                await record_stage_artifact(
                    db=db,
                    stage_id=stage_id,
                    artifact_type="audit",
                    storage_backend="local",
                    storage_path=str(audit_path),
                    content_bytes=staged_audit_bytes,
                )

            if cfg.graph.site_hostname and cfg.graph.site_path and cfg.graph.document_library:
                try:
                    if existing_case_folder_name:
                        case_folder = graph.find_case_folder_by_grievance_id_exact(
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            grievance_id=grievance_id,
                        )
                    else:
                        case_folder = graph.ensure_case_folder(
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            grievance_id=grievance_id,
                            member_name=folder_member_name,
                        )
                    if staged_signed_bytes:
                        signed_upload = graph.upload_to_case_subfolder(
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_folder_name=case_folder.folder_name,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            subfolder=cfg.graph.signed_subfolder,
                            filename=f"{doc_type}_{document_id}_stage{stage_no}_signed.pdf",
                            file_bytes=staged_signed_bytes,
                        )
                        await record_stage_artifact(
                            db=db,
                            stage_id=stage_id,
                            artifact_type="pdf_signed",
                            storage_backend="sharepoint",
                            storage_path=signed_upload.web_url or signed_upload.path,
                            content_bytes=staged_signed_bytes,
                        )
                    if staged_audit_bytes:
                        audit_upload = graph.upload_to_case_subfolder(
                            site_hostname=cfg.graph.site_hostname,
                            site_path=cfg.graph.site_path,
                            library=cfg.graph.document_library,
                            case_folder_name=case_folder.folder_name,
                            case_parent_folder=cfg.graph.case_parent_folder,
                            subfolder=cfg.graph.audit_subfolder,
                            filename=f"{doc_type}_{document_id}_stage{stage_no}_audit{staged_audit_ext}",
                            file_bytes=staged_audit_bytes,
                        )
                        await record_stage_artifact(
                            db=db,
                            stage_id=stage_id,
                            artifact_type="audit",
                            storage_backend="sharepoint",
                            storage_path=audit_upload.web_url or audit_upload.path,
                            content_bytes=staged_audit_bytes,
                        )
                except Exception as exc:
                    await db.add_event(
                        case_id,
                        document_id,
                        "document_stage_sharepoint_upload_failed",
                        {"stage_no": stage_no, "error": str(exc)},
                    )

            await db.replace_document_stage_fields(document_stage_id=stage_id, fields={"webhook_payload": payload})
            await db.complete_document_stage(stage_id=stage_id)
            await db.add_event(
                case_id,
                document_id,
                "document_stage_completed",
                {"stage_no": stage_no, "stage_id": stage_id},
            )

            if stage_no < 3:
                signer_order: list[str] = []
                try:
                    parsed_signers = json.loads(signer_order_json or "[]")
                    if isinstance(parsed_signers, list):
                        signer_order = [str(s).strip() for s in parsed_signers if str(s).strip()]
                except Exception:
                    signer_order = []
                if len(signer_order) < 3:
                    await db.exec("UPDATE documents SET status='failed' WHERE id=?", (document_id,))
                    await db.add_event(
                        case_id,
                        document_id,
                        "document_stage_advance_failed",
                        {"stage_no": stage_no, "reason": "missing_signers"},
                    )
                    await db.mark_receipt_handled("docuseal", completion_receipt_key)
                    await db.mark_receipt_handled("docuseal", receipt_key)
                    return {"ok": True, "handled": True, "reason": "stage_advance_failed"}

                next_stage_no = stage_no + 1
                next_base_pdf: bytes | None = staged_signed_bytes
                if not next_base_pdf and pdf_path and Path(pdf_path).exists():
                    next_base_pdf = Path(pdf_path).read_bytes()

                align_path = stage_alignment_pdf_path(
                    case_dir=case_dir,
                    document_id=document_id,
                    stage_no=next_stage_no,
                )
                if not next_base_pdf or not align_path.exists():
                    await db.exec("UPDATE documents SET status='failed' WHERE id=?", (document_id,))
                    await db.add_event(
                        case_id,
                        document_id,
                        "document_stage_advance_failed",
                        {
                            "stage_no": stage_no,
                            "reason": "missing_next_stage_base_or_alignment",
                            "alignment_path": str(align_path),
                        },
                    )
                    await db.mark_receipt_handled("docuseal", completion_receipt_key)
                    await db.mark_receipt_handled("docuseal", receipt_key)
                    return {"ok": True, "handled": True, "reason": "stage_advance_failed"}

                next_alignment_pdf = align_path.read_bytes()
                next_outcome = await create_or_send_stage(
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
                    pdf_bytes=next_base_pdf,
                    alignment_pdf_bytes=next_alignment_pdf,
                    signer_email=signer_order[next_stage_no - 1],
                    full_signer_chain=signer_order,
                    stage_no=next_stage_no,
                    correlation_id=case_id,
                    idempotency_prefix=f"webhook-stage:{case_id}:{document_id}:{next_stage_no}",
                )
                if next_outcome.status.startswith("sent_for_signature"):
                    await db.exec("UPDATE cases SET status='awaiting_signatures' WHERE id=?", (case_id,))
                else:
                    await db.exec("UPDATE documents SET status='failed' WHERE id=?", (document_id,))

                await db.mark_receipt_handled("docuseal", completion_receipt_key)
                await db.mark_receipt_handled("docuseal", receipt_key)
                return {"ok": True, "handled": True, "stage_no": stage_no, "next_status": next_outcome.status}

        case_dir = Path(cfg.data_root) / case_id
        doc_dir = case_dir / document_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        signed_pdf_bytes: bytes | None = None
        signed_pdf_path: str | None = None
        audit_zip_path: str | None = None
        audit_file_name: str = f"{doc_type}_audit.zip"
        submission_details: dict | None = None

        try:
            artifacts = docuseal.download_completed_artifacts(submission_id=submission_id)
            maybe_submission = artifacts.get("submission")
            if isinstance(maybe_submission, dict):
                submission_details = maybe_submission
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

        if signed_pdf_bytes is None:
            candidate_doc_url = ""
            doc_urls = _iter_document_urls(payload)
            if not doc_urls and submission_details:
                doc_urls = _iter_document_urls(submission_details)
            if doc_urls:
                candidate_doc_url = doc_urls[0]
            downloaded_signed = _download_public_bytes(candidate_doc_url)
            if downloaded_signed:
                signed_pdf_bytes = downloaded_signed
                signed_pdf_path = str(doc_dir / "signed.pdf")
                Path(signed_pdf_path).write_bytes(signed_pdf_bytes)
                await db.add_event(
                    case_id,
                    document_id,
                    "docuseal_signed_pdf_downloaded",
                    {"source": "document_url"},
                )

        if audit_zip_path is None:
            audit_url = _find_audit_url(payload)
            if not audit_url and submission_details:
                audit_url = _find_audit_url(submission_details)
            downloaded_audit = _download_public_bytes(audit_url)
            if downloaded_audit:
                ext = ".zip" if downloaded_audit[:2] == b"PK" else ".pdf"
                audit_zip_path = str(doc_dir / f"docuseal_audit_log{ext}")
                Path(audit_zip_path).write_bytes(downloaded_audit)
                audit_file_name = f"{doc_type}_audit{ext}"
                await db.add_event(
                    case_id,
                    document_id,
                    "docuseal_audit_downloaded",
                    {"source": "audit_log_url", "file_extension": ext},
                )

        if signed_pdf_bytes is None and pdf_path and Path(pdf_path).exists():
            signed_pdf_bytes = Path(pdf_path).read_bytes()
            signed_pdf_path = pdf_path

        sharepoint_generated_url: str | None = None
        sharepoint_signed_url: str | None = None
        sharepoint_audit_url: str | None = None
        audit_backup_locations_json: str | None = None
        sharepoint_case_folder: str | None = None
        sharepoint_case_web_url: str | None = None

        generated_pdf_path = Path(pdf_path) if pdf_path else None
        try:
            if cfg.graph.site_hostname and cfg.graph.site_path and cfg.graph.document_library:
                generated_upload_name = f"{doc_type}_{document_id}.pdf"
                signed_upload_name = f"{doc_type}_{document_id}_signed.pdf"
                audit_ext = Path(audit_file_name).suffix or ".zip"
                audit_upload_name = f"{doc_type}_{document_id}_audit{audit_ext}"
                if existing_case_folder_name:
                    case_folder = graph.find_case_folder_by_grievance_id_exact(
                        site_hostname=cfg.graph.site_hostname,
                        site_path=cfg.graph.site_path,
                        library=cfg.graph.document_library,
                        case_parent_folder=cfg.graph.case_parent_folder,
                        grievance_id=grievance_id,
                    )
                else:
                    case_folder = graph.ensure_case_folder(
                        site_hostname=cfg.graph.site_hostname,
                        site_path=cfg.graph.site_path,
                        library=cfg.graph.document_library,
                        case_parent_folder=cfg.graph.case_parent_folder,
                        grievance_id=grievance_id,
                        member_name=folder_member_name,
                    )
                sharepoint_case_folder = case_folder.folder_name
                sharepoint_case_web_url = case_folder.web_url or existing_case_folder_web_url
                await db.add_event(
                    case_id,
                    document_id,
                    "sharepoint_upload_target_resolved",
                    {
                        "folder_id": case_folder.folder_id,
                        "folder_name": case_folder.folder_name,
                        "folder_web_url": sharepoint_case_web_url,
                        "case_parent_folder": cfg.graph.case_parent_folder,
                    },
                )

                if generated_pdf_path and generated_pdf_path.exists():
                    uploaded_generated = graph.upload_to_case_subfolder(
                        site_hostname=cfg.graph.site_hostname,
                        site_path=cfg.graph.site_path,
                        library=cfg.graph.document_library,
                        case_folder_name=case_folder.folder_name,
                        case_parent_folder=cfg.graph.case_parent_folder,
                        subfolder=cfg.graph.generated_subfolder,
                        filename=generated_upload_name,
                        file_bytes=generated_pdf_path.read_bytes(),
                    )
                    sharepoint_generated_url = uploaded_generated.web_url
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

                if signed_pdf_bytes:
                    uploaded_signed = graph.upload_to_case_subfolder(
                        site_hostname=cfg.graph.site_hostname,
                        site_path=cfg.graph.site_path,
                        library=cfg.graph.document_library,
                        case_folder_name=case_folder.folder_name,
                        case_parent_folder=cfg.graph.case_parent_folder,
                        subfolder=cfg.graph.signed_subfolder,
                        filename=signed_upload_name,
                        file_bytes=signed_pdf_bytes,
                    )
                    sharepoint_signed_url = uploaded_signed.web_url
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
                    sharepoint_audit_url = backup_outcome.primary_web_url
                    audit_backup_locations_json = merge_backup_locations_json(None, backup_outcome)
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
                    else:
                        await db.add_event(
                            case_id,
                            document_id,
                            "audit_backup_completed",
                            {
                                "sharepoint_copy_count": len(backup_outcome.sharepoint_copies),
                                "local_copy_count": len(backup_outcome.local_paths),
                            },
                        )
        except Exception as exc:
            await db.add_event(case_id, document_id, "sharepoint_upload_failed", {"error": str(exc)})
            logger.exception("sharepoint_upload_failed", extra={"correlation_id": case_id, "document_id": document_id})

        await db.exec(
            """UPDATE documents
               SET status=?, completed_at_utc=?, signed_pdf_path=?, audit_zip_path=?,
                   sharepoint_generated_url=?, sharepoint_signed_url=?, sharepoint_audit_url=?,
                   audit_backup_locations_json=?
               WHERE id=?""",
            (
                "signed",
                utcnow(),
                signed_pdf_path,
                audit_zip_path,
                sharepoint_generated_url,
                sharepoint_signed_url,
                sharepoint_audit_url,
                audit_backup_locations_json,
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
            "projected_grievance_number": projected_grievance_number,
            "contract_name": contract_label or "",
            "incident_date": incident_dt.isoformat() if incident_dt else "",
            "deadline_days": str(deadline_days or ""),
            "deadline_date": deadline_dt.isoformat() if deadline_dt else "",
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
            completion_idem_prefix = f"docuseal_completion:{document_id}:{submission_id}"
            for signer in signer_emails:
                await notifications.send_one(
                    case_id=case_id,
                    document_id=document_id,
                    recipient_email=signer,
                    template_key="completion_signer",
                    context={**common_context, "signer_email": signer},
                    idempotency_key=f"{completion_idem_prefix}:completion_signer:{signer.lower()}",
                    attachments=attachments if cfg.email.allow_signer_copy_link else None,
                )

            for recipient in cfg.email.internal_recipients:
                await notifications.send_one(
                    case_id=case_id,
                    document_id=document_id,
                    recipient_email=recipient,
                    template_key="completion_internal",
                    context=common_context,
                    idempotency_key=f"{completion_idem_prefix}:completion_internal:{recipient.lower()}",
                    attachments=attachments,
                )

            if cfg.require_approver_decision and cfg.email.derek_email:
                await notifications.send_one(
                    case_id=case_id,
                    document_id=document_id,
                    recipient_email=cfg.email.derek_email,
                    template_key="completion_approval",
                    context=common_context,
                    idempotency_key=f"{completion_idem_prefix}:completion_approval:{cfg.email.derek_email.lower()}",
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
            if cfg.require_approver_decision:
                await db.exec(
                    "UPDATE cases SET status='pending_approval', approval_status='pending' WHERE id=?",
                    (case_id,),
                )
            else:
                approved_ts = utcnow()
                await db.exec(
                    """UPDATE documents
                       SET status='approved'
                       WHERE case_id=?
                         AND status IN ('signed', 'pending_approval', 'created')""",
                    (case_id,),
                )
                await db.exec(
                    """UPDATE cases
                       SET status='approved',
                           approval_status='approved',
                           approved_at_utc=?,
                           approver_email=?,
                           approval_notes=?
                       WHERE id=?
                         AND approval_status!='rejected'""",
                    (
                        approved_ts,
                        "system@automation",
                        "Auto-approved by workflow (require_approver_decision=false)",
                        case_id,
                    ),
                )
                await db.add_event(
                    case_id,
                    None,
                    "case_auto_approved",
                    {"approved_at_utc": approved_ts},
                )

        await db.add_event(case_id, document_id, "docuseal_completion_processed", {"receipt_key": receipt_key})
        await db.mark_receipt_handled("docuseal", completion_receipt_key)
        await db.mark_receipt_handled("docuseal", receipt_key)
        logger.info("docuseal_completion_processed", extra={"correlation_id": case_id, "document_id": document_id})
        return {"ok": True, "handled": True}
    except Exception:
        await db.release_receipt_claim("docuseal", completion_receipt_key)
        await db.release_receipt_claim("docuseal", receipt_key)
        raise
