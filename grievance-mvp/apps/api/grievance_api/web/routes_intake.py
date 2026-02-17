from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..core.hmac_auth import verify_hmac
from ..core.ids import new_case_id, new_document_id, normalize_grievance_id
from ..db.db import Db, utcnow
from ..services.doc_render import render_docx
from ..services.notification_service import NotificationService
from ..services.pdf_convert import docx_to_pdf
from .models import CaseStatusResponse, DocumentRequest, DocumentStatus, IntakeRequest, IntakeResponse

router = APIRouter()


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    safe = _FILENAME_SAFE.sub("_", value.strip())
    return safe.strip("_") or "document"


def _resolve_template_path(cfg, doc_req: DocumentRequest) -> str:  # noqa: ANN001
    if doc_req.template_key and doc_req.template_key in cfg.doc_templates:
        return cfg.doc_templates[doc_req.template_key]
    if doc_req.doc_type in cfg.doc_templates:
        return cfg.doc_templates[doc_req.doc_type]
    return cfg.docx_template_path


def _resolve_docuseal_template_id(cfg, doc_req: DocumentRequest) -> int | None:  # noqa: ANN001
    if doc_req.template_key and doc_req.template_key in cfg.docuseal.template_ids:
        return cfg.docuseal.template_ids[doc_req.template_key]
    if doc_req.doc_type in cfg.docuseal.template_ids:
        return cfg.docuseal.template_ids[doc_req.doc_type]
    return cfg.docuseal.default_template_id


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

    grievance_id = normalize_grievance_id(payload.grievance_id) or payload.grievance_id
    case_id = new_case_id()
    cdir = Path(cfg.data_root) / case_id
    cdir.mkdir(parents=True, exist_ok=True)

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    try:
        await db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status,
                 member_name, member_email, intake_request_id, intake_payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                grievance_id,
                utcnow(),
                "processing",
                "pending",
                member_name,
                payload.grievant_email,
                payload.request_id,
                payload.model_dump_json(),
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
    await db.add_event(case_id, None, "case_created", {"request_id": payload.request_id, "grievance_id": grievance_id})

    doc_requests = payload.documents or [DocumentRequest(doc_type="grievance_form", requires_signature=True)]
    doc_statuses: list[DocumentStatus] = []
    any_signature_requested = False
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

        context = {
            "case_id": case_id,
            "grievance_id": grievance_id,
            "document_id": document_id,
            "document_type": doc_type,
            "created_at_utc": utcnow(),
            **payload.model_dump(),
        }

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
        await db.add_event(case_id, document_id, "document_created", {"doc_type": doc_type})

        status = initial_status
        signing_link: str | None = None

        if doc_req.requires_signature:
            signer_order = [
                s.strip()
                for s in (doc_req.signers or [payload.grievant_email])
                if s and s.strip()
            ]
            if not signer_order:
                signer_order = [payload.grievant_email]

            try:
                submission = docuseal.create_submission(
                    pdf_bytes=pdf_bytes,
                    signers=signer_order,
                    title=f"Grievance {grievance_id} - {doc_type}",
                    metadata={
                        "case_id": case_id,
                        "document_id": document_id,
                        "grievance_id": grievance_id,
                        "doc_type": doc_type,
                    },
                    template_id=_resolve_docuseal_template_id(cfg, doc_req),
                )
                signing_link = submission.signing_link
                status = "sent_for_signature"
                any_signature_requested = True
                await db.exec(
                    """UPDATE documents
                       SET status=?, signer_order_json=?, docuseal_submission_id=?, docuseal_signing_link=?
                       WHERE id=?""",
                    (
                        status,
                        json.dumps(signer_order, ensure_ascii=False),
                        submission.submission_id,
                        signing_link,
                        document_id,
                    ),
                )
                await db.add_event(
                    case_id,
                    document_id,
                    "sent_for_signature",
                    {"submission_id": submission.submission_id, "doc_type": doc_type},
                )

                if cfg.email.enabled and signing_link:
                    for signer in signer_order:
                        try:
                            await notifications.send_one(
                                case_id=case_id,
                                document_id=document_id,
                                recipient_email=signer,
                                template_key="signature_request",
                                context={
                                    "case_id": case_id,
                                    "grievance_id": grievance_id,
                                    "document_id": document_id,
                                    "document_type": doc_type,
                                    "docuseal_signing_url": signing_link,
                                    "signer_email": signer,
                                    "status": status,
                                },
                                idempotency_key=f"intake:{case_id}:{document_id}:signature_request:{signer.lower()}",
                            )
                        except Exception:
                            await db.add_event(
                                case_id,
                                document_id,
                                "signature_request_email_failed",
                                {"recipient": signer},
                            )
                            logger.exception(
                                "signature_request_email_failed",
                                extra={"correlation_id": case_id, "document_id": document_id},
                            )

            except Exception as exc:
                any_failed = True
                status = "failed"
                await db.exec(
                    "UPDATE documents SET status=?, signer_order_json=? WHERE id=?",
                    (status, json.dumps(signer_order, ensure_ascii=False), document_id),
                )
                await db.add_event(
                    case_id,
                    document_id,
                    "docuseal_create_failed",
                    {"error": str(exc), "doc_type": doc_type},
                )
                logger.exception("docuseal_create_failed", extra={"correlation_id": case_id, "document_id": document_id})
        else:
            status = "pending_approval"
            await db.exec("UPDATE documents SET status=? WHERE id=?", (status, document_id))
            await db.add_event(case_id, document_id, "no_signature_required", {})

        doc_statuses.append(
            DocumentStatus(document_id=document_id, doc_type=doc_type, status=status, signing_link=signing_link)
        )

    if any_failed and not any_signature_requested:
        case_status = "failed"
    elif any_signature_requested:
        case_status = "awaiting_signatures"
    else:
        case_status = "pending_approval"

    await db.exec("UPDATE cases SET status=? WHERE id=?", (case_status, case_id))
    await db.add_event(case_id, None, "intake_completed", {"status": case_status, "document_count": len(doc_statuses)})

    return IntakeResponse(case_id=case_id, grievance_id=grievance_id, status=case_status, documents=doc_statuses)
