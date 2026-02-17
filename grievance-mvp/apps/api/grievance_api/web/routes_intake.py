
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException

from ..core.hmac_auth import verify_hmac
from ..core.ids import new_case_id, new_document_id
from ..db.db import Db, utcnow
from ..services.doc_render import render_docx
from ..services.pdf_convert import docx_to_pdf
from ..web.models import IntakeRequest, IntakeResponse, DocumentStatus

router = APIRouter()

@router.post("/intake", response_model=IntakeResponse)
async def intake(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger

    body = await verify_hmac(request, cfg.hmac_shared_secret)
    payload = IntakeRequest.model_validate_json(body)

    correlation_id = payload.request_id

    # Replay protection (idempotency)
    row = await db.fetchone(
        "SELECT id, status FROM cases WHERE intake_request_id=?",
        (payload.request_id,),
    )
    if row:
        case_id, status = row
        logger.info("intake_deduped", extra={"correlation_id": correlation_id})
        # Return the current status of the case and its documents
        docs = await db.fetchall(
            "SELECT doc_type, status, docuseal_signing_link FROM documents WHERE case_id=?",
            (case_id,)
        )
        doc_statuses = [DocumentStatus(doc_type=d[0], status=d[1], signing_link=d[2]) for d in docs]
        return IntakeResponse(case_id=case_id, status=status, documents=doc_statuses)

    case_id = new_case_id()
    cdir = Path(cfg.data_root) / case_id
    cdir.mkdir(parents=True, exist_ok=True)

    await db.exec(
        "INSERT INTO cases (id, created_at_utc, status, member_name, intake_request_id, intake_payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            case_id,
            utcnow(),
            "created",
            f"{payload.grievant_firstname} {payload.grievant_lastname}",
            payload.request_id,
            payload.model_dump_json(),
        ),
    )
    await db.add_event(case_id, None, "case_created", {"request_id": payload.request_id})

    doc_statuses = []
    for doc_req in payload.documents:
        document_id = new_document_id()
        doc_status = "created"
        signing_link = None

        context = {
            "case_id": case_id,
            "document_id": document_id,
            "created_at_utc": utcnow(),
            **payload.model_dump(),
        }

        docx_path = str(cdir / f"{doc_req.doc_type}.docx")
        pdf_path = str(cdir / f"{doc_req.doc_type}.pdf")

        try:
            render_docx(cfg.docx_template_path, context, docx_path)
            pdf_path = docx_to_pdf(docx_path, str(cdir), cfg.libreoffice_timeout_seconds)
            pdf_bytes = Path(pdf_path).read_bytes()
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        except Exception as e:
            await db.add_event(case_id, document_id, "render_failed", {"error": str(e)})
            logger.exception("render_failed", extra={"correlation_id": correlation_id, "doc_type": doc_req.doc_type})
            raise HTTPException(status_code=500, detail=f"Document render/convert failed for {doc_req.doc_type}")

        await db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, status, requires_signature, 
                 docx_path, pdf_path, pdf_sa256
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                case_id,
                utcnow(),
                doc_req.doc_type,
                doc_status,
                doc_req.requires_signature,
                docx_path,
                pdf_path,
                pdf_sha,
            ),
        )

        if doc_req.requires_signature:
            try:
                docuseal = request.app.state.docuseal
                # TODO: Make signer order configurable
                signer_order = [payload.grievant_email]
                result = docuseal.create_submission(
                    pdf_bytes=pdf_bytes,
                    signers=signer_order,
                    title=f"Grievance {case_id} - {doc_req.doc_type}",
                )
                submission_id = result["submission_id"]
                signing_link = result.get("signing_link")

                await db.exec(
                    "UPDATE documents SET status=?, docuseal_submission_id=?, docuseal_signing_link=? WHERE id=?",
                    ("sent_for_signature", submission_id, signing_link, document_id),
                )
                await db.add_event(case_id, document_id, "sent_for_signature", {"submission_id": submission_id})
                logger.info("sent_for_signature", extra={"correlation_id": correlation_id, "doc_type": doc_req.doc_type})
                doc_status = "sent_for_signature"

            except Exception as e:
                await db.add_event(case_id, document_id, "docuseal_create_failed", {"error": str(e)})
                logger.exception("docuseal_create_failed", extra={"correlation_id": correlation_id, "doc_type": doc_req.doc_type})
                # Continue to next document

        doc_statuses.append(DocumentStatus(doc_type=doc_req.doc_type, status=doc_status, signing_link=signing_link))

    return IntakeResponse(case_id=case_id, status="processing", documents=doc_statuses)

