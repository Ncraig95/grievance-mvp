from __future__ import annotations

import hmac
import hashlib
import time
from fastapi import Request, HTTPException

MAX_SKEW_SECONDS = 300

def compute_signature(secret: str, ts: str, body: bytes) -> str:
    msg = ts.encode("utf-8") + b"." + body
    mac = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={mac}"

async def verify_hmac(request: Request, shared_secret: str) -> bytes:
    if not shared_secret or shared_secret.upper().startswith("REPLACE"):
        return await request.body()

    ts = request.headers.get("X-Timestamp")
    sig = request.headers.get("X-Signature")

    if not ts or not sig:
        raise HTTPException(status_code=401, detail="Missing HMAC headers")

    try:
        ts_i = int(ts)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp")

    now = int(time.time())
    if abs(now - ts_i) > MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="Timestamp out of range")

    body = await request.body()
    expected = compute_signature(shared_secret, ts, body)

    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=401, detail="Bad signature")

    return body
