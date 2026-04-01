from __future__ import annotations

import ipaddress
import json

from fastapi import HTTPException, Request


def require_local_access(request: Request) -> None:
    client_host = (request.client.host if request.client else "").strip()
    if client_host.lower() == "localhost":
        return
    try:
        ip = ipaddress.ip_address(client_host)
    except Exception as exc:
        raise HTTPException(status_code=403, detail="admin endpoints require local/private network access") from exc
    if not (ip.is_loopback or ip.is_private):
        raise HTTPException(status_code=403, detail="admin endpoints require local/private network access")


def parse_json_safely(raw: object) -> object:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text
