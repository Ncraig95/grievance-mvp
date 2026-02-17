
from __future__ import annotations

import hashlib
import hmac
import json
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException

from ..db.db import Db
from ..services.docuseal_client import DocuSealClient

def verify_docuseal_webhook(raw_body: bytes, header_sig: str | None, secret: str) -> None:
    if not header_sig:
        raise ValueError("Missing X-DocuSeal-Signature header")

    expected_sig = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, header_sig):
        raise ValueError("Invalid signature")


router = APIRouter()


@router.post("/webhook/docuseal")
async def webhook_docuseal(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger
    docuseal: DocuSealClient = request.app.state.docuseal

    raw = await request.body()

    try:
        verify_docuseal_webhook(raw, request.headers.get("X-DocuSeal-Signature"), cfg.docuseal.webhook_secret)
    except ValueError as e:
        logger.warning(f"docuseal_webhook_invalid_sig: {e}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(raw.decode("utf-8"))
    
    event_type = payload.get("event")
    submission_id = payload.get("id")

    if not event_type or not submission_id:
        return {"ok": True, "msg": "Ignoring event without type or submission id"}

    # Use submission_id for idempotency key
    receipt_key = submission_id

    if await db.receipt_seen("docuseal", receipt_key):
        logger.info("webhook_deduped", extra={"submission_id": submission_id})
        return {"ok": True, "deduped": True}

    await db.store_receipt("docuseal", receipt_key, raw.decode("utf-8"))

    doc_row = await db.fetchone("SELECT id, case_id FROM documents WHERE docuseal_submission_id = ?", (submission_id,))
    if not doc_row:
        logger.warning("docuseal_webhook_unknown_submission", extra={"submission_id": submission_id})
        # Ack to prevent retries
        return {"ok": True, "msg": "Unknown submission"}

    document_id, case_id = doc_row

    await db.add_event(case_id, document_id, "docuseal_webhook_received", {"event_type": event_type})
    logger.info("docuseal_webhook_received", extra={"case_id": case_id, "document_id": document_id, "event_type": event_type})

    if event_type == 'submission.completed':
        await db.exec("UPDATE documents SET status = 'signed' WHERE id = ?", (document_id,))
        await db.add_event(case_id, document_id, "document_signed", {})

        # Download artifacts
        try:
            artifacts = docuseal.download_completed_artifacts(submission_id=submission_id)
            zip_bytes = artifacts["completed_zip_bytes"]
            cdir = Path(cfg.data_root) / case_id
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                z.extractall(cdir)
            await db.add_event(case_id, document_id, "artifacts_downloaded", {})
        except Exception as e:
            logger.exception("artifact_download_failed", extra={"case_id": case_id, "document_id": document_id})
            await db.add_event(case_id, document_id, "artifact_download_failed", {"error": str(e)})
            # Don't halt processing

        # TODO: Business logic for approvals. For now, auto-approve.
        await db.exec("UPDATE documents SET status = 'approved' WHERE id = ?", (document_id,))
        await db.add_event(case_id, document_id, "document_approved", {"auto": True})
        logger.info("document_auto_approved", extra={"case_id": case_id, "document_id": document_id})


    return {"ok": True}
