from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..core.hmac_auth import verify_hmac
from ..core.ids import new_case_id, new_document_id, normalize_grievance_id
from ..db.db import Db, utcnow
from ..services.doc_render import render_docx
from ..services.grievance_id_allocator import GrievanceIdAllocationError, GrievanceIdAllocator
from ..services.notification_service import NotificationService
from ..services.pdf_convert import docx_to_pdf
from ..services.signature_workflow import normalize_signers, send_document_for_signature
from .models import CaseStatusResponse, DocumentRequest, DocumentStatus, IntakeRequest, IntakeResponse

router = APIRouter()


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_FIELD_KEY_SAFE = re.compile(r"[^A-Za-z0-9]+")


def _safe_name(value: str) -> str:
    safe = _FILENAME_SAFE.sub("_", value.strip())
    return safe.strip("_") or "document"


def _resolve_template_path(cfg, doc_req: DocumentRequest) -> str:  # noqa: ANN001
    if doc_req.template_key and doc_req.template_key in cfg.doc_templates:
        return cfg.doc_templates[doc_req.template_key]
    if doc_req.doc_type in cfg.doc_templates:
        return cfg.doc_templates[doc_req.doc_type]
    return cfg.docx_template_path


def _normalize_field_key(key: str) -> str:
    normalized = _FIELD_KEY_SAFE.sub("_", key.strip()).strip("_").lower()
    return normalized


def _flatten_fields(value: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        composed = f"{prefix}_{key}" if prefix else key
        if isinstance(raw_val, dict):
            out.update(_flatten_fields(raw_val, composed))
        else:
            out[composed] = raw_val
    return out


def _coerce_context_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return json.dumps(value, ensure_ascii=False)


def _preferred_signer_email(payload: IntakeRequest) -> str | None:
    template_data = payload.template_data or {}
    if isinstance(template_data, dict):
        for key in ("personal_email", "personalEmail", "signer_email", "signerEmail"):
            val = template_data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    if payload.grievant_email and payload.grievant_email.strip():
        return payload.grievant_email.strip()
    return None


def _apply_statement_defaults(
    *,
    context: dict[str, object],
    payload: IntakeRequest,
    grievance_id: str,
    grievance_number: str | None,
) -> None:
    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    defaults: dict[str, object] = {
        "grievant_name": member_name,
        "work_address": payload.work_location or "",
        "home_address": "",
        "seniority_date": "",
        "ncs_date": "",
        "personal_cell": payload.grievant_phone or "",
        "personal_email": payload.grievant_email or "",
        "department": "",
        "title": "",
        "supervisor_name": payload.supervisor or "",
        "supervisor_phone": "",
        "supervisor_email": "",
        # Legacy placeholder in template includes a space.
        "grievants uid": grievance_id,
        "grievants_uid": grievance_id,
        "incident_date": payload.incident_date or "",
        "article": "",
        "statement_text": payload.narrative or "",
        "statement_continuation": "",
        "witness_1_name": "",
        "witness_1_title": "",
        "witness_1_phone": "",
        "witness_2_name": "",
        "witness_2_title": "",
        "witness_2_phone": "",
        "witness_3_name": "",
        "witness_3_title": "",
        "witness_3_phone": "",
        "grievance_number": grievance_number or "",
    }
    for key, value in defaults.items():
        context.setdefault(key, value)


def _validate_grievance_input_mode(grievance_mode: str, incoming_grievance_id: str) -> None:
    if grievance_mode == "auto" and incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id must be omitted when grievance_id.mode=auto",
        )
    if grievance_mode == "manual" and not incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id is required when grievance_id.mode=manual",
        )


def _build_template_context(
    *,
    payload: IntakeRequest,
    case_id: str,
    grievance_id: str,
    document_id: str,
    doc_type: str,
    grievance_number: str | None,
) -> dict[str, object]:
    payload_data = payload.model_dump(exclude={"documents", "template_data"})
    merged_fields = _flatten_fields(payload_data)
    merged_fields.update(_flatten_fields(payload.template_data))

    context: dict[str, object] = {
        "case_id": case_id,
        "grievance_id": grievance_id,
        "grievance_number": grievance_number or "",
        "document_id": document_id,
        "document_type": doc_type,
        "created_at_utc": utcnow(),
    }
    protected_keys = set(context.keys())

    for key, raw_val in merged_fields.items():
        value = _coerce_context_value(raw_val)
        if key not in protected_keys:
            context[key] = value

        normalized = _normalize_field_key(key)
        if normalized and normalized not in context:
            context[normalized] = value

    _apply_statement_defaults(
        context=context,
        payload=payload,
        grievance_id=grievance_id,
        grievance_number=grievance_number,
    )

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    if member_name:
        context.setdefault("grievant_name", member_name)
        context.setdefault("grievant_names", member_name)

    today = date.today().isoformat()
    context.setdefault("today_date", today)
    context.setdefault("request_date", today)

    return context


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
    statuses = [
        DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3])
        for d in docs
    ]

    return CaseStatusResponse(
        case_id=case_id,
        grievance_id=case_row[0],
        status=case_row[1],
        approval_status=case_row[2],
        grievance_number=case_row[3],
        documents=statuses,
    )


@router.get("/cases/{case_id}", response_model=CaseStatusResponse)
async def get_case(case_id: str, request: Request):
    db: Db = request.app.state.db
    return await _load_case_status(db, case_id)


@router.post("/intake", response_model=IntakeResponse)
async def intake(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger
    docuseal = request.app.state.docuseal
    notifications: NotificationService = request.app.state.notifications

    body = await verify_hmac(request, cfg.hmac_shared_secret)
    payload = IntakeRequest.model_validate_json(body)

    correlation_id = payload.request_id

    existing = await db.fetchone(
        "SELECT id, grievance_id, status FROM cases WHERE intake_request_id=?",
        (payload.request_id,),
    )
    if existing:
        case_id, grievance_id, status = existing
        logger.info("intake_deduped", extra={"correlation_id": correlation_id, "case_id": case_id})
        docs = await db.fetchall(
            "SELECT id, doc_type, status, docuseal_signing_link FROM documents WHERE case_id=? ORDER BY created_at_utc",
            (case_id,),
        )
        return IntakeResponse(
            case_id=case_id,
            grievance_id=grievance_id,
            status=status,
            documents=[
                DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3]) for d in docs
            ],
        )

    grievance_number = (payload.grievance_number or "").strip() or None
    case_id = new_case_id()
    cdir = Path(cfg.data_root) / case_id
    cdir.mkdir(parents=True, exist_ok=True)

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    incoming_grievance_id = (payload.grievance_id or "").strip()
    grievance_mode = cfg.grievance_id.mode

    _validate_grievance_input_mode(grievance_mode, incoming_grievance_id)

    sharepoint_case_folder: str | None = None
    sharepoint_case_web_url: str | None = None
    if grievance_mode == "manual":
        grievance_id = normalize_grievance_id(incoming_grievance_id) or incoming_grievance_id
    else:
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=db,
            graph=request.app.state.graph,
            logger=logger,
        )
        try:
            allocation = await allocator.allocate_and_reserve_folder(
                member_name=member_name,
                correlation_id=correlation_id,
            )
        except GrievanceIdAllocationError as exc:
            logger.exception("grievance_id_allocation_failed", extra={"correlation_id": correlation_id})
            raise HTTPException(status_code=503, detail="unable to allocate grievance_id") from exc
        grievance_id = allocation.grievance_id
        sharepoint_case_folder = allocation.case_folder_name
        sharepoint_case_web_url = allocation.case_folder_web_url

    try:
        await db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status,
                 grievance_number, member_name, member_email, intake_request_id, intake_payload_json,
                 sharepoint_case_folder, sharepoint_case_web_url
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                grievance_id,
                utcnow(),
                "processing",
                "pending",
                grievance_number,
                member_name,
                payload.grievant_email,
                payload.request_id,
                payload.model_dump_json(),
                sharepoint_case_folder,
                sharepoint_case_web_url,
            ),
        )
    except Exception as exc:
        if "UNIQUE constraint failed: cases.intake_request_id" in str(exc):
            dupe = await db.fetchone(
                "SELECT id, grievance_id, status FROM cases WHERE intake_request_id=?",
                (payload.request_id,),
            )
            if not dupe:
                raise
            dupe_case_id, dupe_grievance_id, dupe_status = dupe
            docs = await db.fetchall(
                "SELECT id, doc_type, status, docuseal_signing_link FROM documents WHERE case_id=? ORDER BY created_at_utc",
                (dupe_case_id,),
            )
            return IntakeResponse(
                case_id=dupe_case_id,
                grievance_id=dupe_grievance_id,
                status=dupe_status,
                documents=[
                    DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3]) for d in docs
                ],
            )
        raise
    await db.add_event(
        case_id,
        None,
        "case_created",
        {
            "request_id": payload.request_id,
            "grievance_id": grievance_id,
            "grievance_number": grievance_number,
            "grievance_id_mode": grievance_mode,
            "sharepoint_case_folder": sharepoint_case_folder,
        },
    )

    doc_requests = payload.documents or [DocumentRequest(doc_type="grievance_form", requires_signature=True)]
    doc_statuses: list[DocumentStatus] = []
    any_signature_requested = False
    any_signature_queued = False
    any_failed = False

    for doc_req in doc_requests:
        document_id = new_document_id()
        doc_type = doc_req.doc_type.strip() or "document"
        template_path = _resolve_template_path(cfg, doc_req)
        doc_name = _safe_name(doc_type)
        ddir = cdir / document_id
        ddir.mkdir(parents=True, exist_ok=True)

        docx_path = str(ddir / f"{doc_name}.docx")
        pdf_path = str(ddir / f"{doc_name}.pdf")

        context = _build_template_context(
            payload=payload,
            case_id=case_id,
            grievance_id=grievance_id,
            document_id=document_id,
            doc_type=doc_type,
            grievance_number=grievance_number,
        )

        try:
            render_docx(template_path, context, docx_path)
            pdf_path = docx_to_pdf(docx_path, str(ddir), cfg.libreoffice_timeout_seconds)
            pdf_bytes = Path(pdf_path).read_bytes()
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        except Exception as exc:
            any_failed = True
            await db.exec(
                """INSERT INTO documents(
                     id, case_id, created_at_utc, doc_type, template_key, status,
                     requires_signature, docx_path, pdf_path, pdf_sha256
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    case_id,
                    utcnow(),
                    doc_type,
                    doc_req.template_key,
                    "failed",
                    1 if doc_req.requires_signature else 0,
                    docx_path,
                    pdf_path,
                    "",
                ),
            )
            await db.add_event(
                case_id,
                document_id,
                "render_failed",
                {"error": str(exc), "doc_type": doc_type, "template_path": template_path},
            )
            logger.exception("render_failed", extra={"correlation_id": correlation_id, "doc_type": doc_type})
            doc_statuses.append(DocumentStatus(document_id=document_id, doc_type=doc_type, status="failed"))
            continue

        initial_status = "created"
        await db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, docx_path, pdf_path, pdf_sha256
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                case_id,
                utcnow(),
                doc_type,
                doc_req.template_key,
                initial_status,
                1 if doc_req.requires_signature else 0,
                docx_path,
                pdf_path,
                pdf_sha,
            ),
        )
        await db.add_event(
            case_id,
            document_id,
            "document_created",
            {"doc_type": doc_type, "template_path": template_path},
        )

        status = initial_status
        signing_link: str | None = None

        if doc_req.requires_signature:
            signer_order = normalize_signers(doc_req.signers, _preferred_signer_email(payload))
            if not grievance_number:
                status = "pending_grievance_number"
                any_signature_queued = True
                await db.exec(
                    "UPDATE documents SET status=?, signer_order_json=? WHERE id=?",
                    (status, json.dumps(signer_order, ensure_ascii=False), document_id),
                )
                await db.add_event(
                    case_id,
                    document_id,
                    "signature_queued_pending_grievance_number",
                    {"doc_type": doc_type},
                )
            else:
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
                    template_key=doc_req.template_key,
                    pdf_bytes=pdf_bytes,
                    signer_order=signer_order,
                    correlation_id=correlation_id,
                    idempotency_prefix=f"intake:{case_id}:{document_id}",
                )
                status = outcome.status
                signing_link = outcome.signing_link
                any_signature_requested = any_signature_requested or status == "sent_for_signature"
                any_failed = any_failed or status == "failed"
        else:
            status = "pending_approval"
            await db.exec("UPDATE documents SET status=? WHERE id=?", (status, document_id))
            await db.add_event(case_id, document_id, "no_signature_required", {})

        doc_statuses.append(
            DocumentStatus(document_id=document_id, doc_type=doc_type, status=status, signing_link=signing_link)
        )

    if any_signature_requested:
        case_status = "awaiting_signatures"
    elif any_signature_queued:
        case_status = "pending_grievance_number"
    elif any_failed:
        case_status = "failed"
    else:
        case_status = "pending_approval"

    await db.exec("UPDATE cases SET status=? WHERE id=?", (case_status, case_id))
    await db.add_event(case_id, None, "intake_completed", {"status": case_status, "document_count": len(doc_statuses)})

    return IntakeResponse(case_id=case_id, grievance_id=grievance_id, status=case_status, documents=doc_statuses)
