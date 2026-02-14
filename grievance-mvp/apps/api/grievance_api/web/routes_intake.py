from __future__ import annotations

import hashlib
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException

from ..core.hmac_auth import verify_hmac
from ..core.ids import new_grievance_id
from ..db.db import Db, utcnow
from ..services.doc_render import render_docx
from ..services.pdf_convert import docx_to_pdf
from ..web.models import IntakeRequest, IntakeResponse

router = APIRouter()

def _year_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y")

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
        "SELECT id, status, docuseal_signing_link FROM grievances WHERE intake_request_id=?",
        (payload.request_id,),
    )
    if row:
        grievance_id, status, signing_link = row
        logger.info("intake_deduped", extra={"correlation_id": correlation_id})
        return IntakeResponse(grievance_id=grievance_id, status=status, signing_link=signing_link)

    grievance_id = new_grievance_id()
    gdir = Path(cfg.data_root) / grievance_id
    gdir.mkdir(parents=True, exist_ok=True)

    docx_path = str(gdir / "grievance.docx")
    context = {
        "grievance_id": grievance_id,
        "created_at_utc": utcnow(),
        **payload.model_dump(),
    }

    try:
        render_docx(cfg.docx_template_path, context, docx_path)
        pdf_path = docx_to_pdf(docx_path, str(gdir), cfg.libreoffice_timeout_seconds)
        pdf_bytes = Path(pdf_path).read_bytes()
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    except Exception as e:
        await db.add_event(grievance_id, "render_failed", {"error": str(e)})
        logger.exception("render_failed", extra={"correlation_id": correlation_id})
        raise HTTPException(status_code=500, detail="Document render/convert failed")

    # Persist intake + file paths
    await db.exec(
        """INSERT INTO grievances(
             id, created_at_utc, status, signer_email, signer_lastname,
             intake_request_id, intake_payload_json,
             docx_path, pdf_path, pdf_sha256
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            grievance_id,
            utcnow(),
            "created",
            payload.grievant_email,
            payload.grievant_lastname,
            payload.request_id,
            payload.model_dump_json(),
            docx_path,
            pdf_path,
            pdf_sha,
        ),
    )
    await db.add_event(grievance_id, "intake_received", {"request_id": payload.request_id})
    logger.info("intake_received", extra={"correlation_id": correlation_id})

    # Create DocuSeal submission (stub)
    signing_link = None
    submission_id = None
    try:
        docuseal = request.app.state.docuseal
        # result = docuseal.create_submission(
        #     pdf_bytes=pdf_bytes,
        #     signer_email=payload.grievant_email,
        #     signer_name=f"{payload.grievant_firstname} {payload.grievant_lastname}",
        #     title=f"Grievance {grievance_id}",
        # )
        # submission_id = result["submission_id"]
        # signing_link = result.get("signing_link")
        raise NotImplementedError("DocuSeal create_submission not wired yet")
    except NotImplementedError:
        await db.add_event(grievance_id, "docuseal_not_configured", {})
        logger.info("docuseal_not_configured", extra={"correlation_id": correlation_id})
    except Exception as e:
        await db.add_event(grievance_id, "docuseal_create_failed", {"error": str(e)})
        logger.exception("docuseal_create_failed", extra={"correlation_id": correlation_id})
        raise HTTPException(status_code=500, detail="Failed to create signing submission")

    if submission_id:
        await db.exec(
            "UPDATE grievances SET status=?, docuseal_submission_id=?, docuseal_signing_link=? WHERE id=?",
            ("sent_for_signature", submission_id, signing_link, grievance_id),
        )
        await db.add_event(grievance_id, "sent_for_signature", {"submission_id": submission_id})
        logger.info("sent_for_signature", extra={"correlation_id": correlation_id})

    return IntakeResponse(grievance_id=grievance_id, status="created", signing_link=signing_link)
