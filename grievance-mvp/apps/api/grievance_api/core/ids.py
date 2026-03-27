from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone


_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def normalize_grievance_id(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    safe = _ID_SAFE.sub("-", text)
    safe = safe.strip("-")
    return safe[:80]


def new_grievance_id() -> str:
    return f"G{_utc_ts()}_{secrets.token_hex(3)}"


def new_case_id() -> str:
    return f"C{_utc_ts()}_{secrets.token_hex(4)}"


def new_submission_id() -> str:
    return f"S{_utc_ts()}_{secrets.token_hex(4)}"


def new_document_id() -> str:
    return f"D{_utc_ts()}_{secrets.token_hex(4)}"
