from __future__ import annotations

import json
from fastapi import APIRouter, Request, HTTPException

from ..db.db import Db

router = APIRouter()

def verify_docuseal_webhook(raw_body: bytes, header_sig: str | None, secret: str) -> None:
    """
    TODO: Implement based on DocuSeal webhook signing scheme.
    This must be strict once you confirm DocuSeal docs.
    """
    raise NotImplementedError("Fill in DocuSeal webhook verification per official docs")

@router.post("/webhook/docuseal")
async def webhook_docuseal(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger

    raw = await request.body()

    # Verify webhook authenticity (stub)
    try:
        verify_docuseal_webhook(raw, request.headers.get("X-Signature"), cfg.docuseal.webhook_secret)
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Webhook verification not configured")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(raw.decode("utf-8"))

    # Derive idempotency key and grievance_id from payload (you define this once you confirm DocuSeal payload)
    receipt_key = payload.get("event_id") or payload.get("id") or "UNKNOWN"
    grievance_id = payload.get("metadata", {}).get("grievance_id")

    if not grievance_id:
        raise HTTPException(status_code=400, detail="Missing grievance_id in webhook payload")

    if await db.receipt_seen("docuseal", receipt_key):
        logger.info("webhook_deduped", extra={"correlation_id": grievance_id})
        return {"ok": True, "deduped": True}

    await db.store_receipt("docuseal", receipt_key, raw.decode("utf-8"))
    await db.add_event(grievance_id, "docuseal_webhook_received", {"receipt_key": receipt_key})
    logger.info("docuseal_webhook_received", extra={"correlation_id": grievance_id})

    # Completed handling is intentionally not implemented until DocuSeal API + payload are confirmed.
    raise HTTPException(status_code=501, detail="DocuSeal completion handling not configured yet")
