from __future__ import annotations

import hmac
from typing import Mapping

from fastapi import HTTPException, Request

from .config import IntakeAuthConfig


def _get_header_case_insensitive(headers: Mapping[str, str], wanted_name: str) -> str:
    wanted = wanted_name.lower()
    for name, value in headers.items():
        if str(name).lower() == wanted:
            return str(value)
    return ""


def validate_intake_auth_config(cfg: IntakeAuthConfig) -> None:
    has_cf_id = bool((cfg.cloudflare_access_client_id or "").strip())
    has_cf_secret = bool((cfg.cloudflare_access_client_secret or "").strip())
    if has_cf_id != has_cf_secret:
        raise RuntimeError(
            "intake_auth.cloudflare_access_client_id and intake_auth.cloudflare_access_client_secret must both be set"
        )

    if (cfg.shared_header_value or "").strip() and not (cfg.shared_header_name or "").strip():
        raise RuntimeError("intake_auth.shared_header_name must be set when intake_auth.shared_header_value is set")


def verify_intake_headers(headers: Mapping[str, str], cfg: IntakeAuthConfig) -> None:
    shared_value = (cfg.shared_header_value or "").strip()
    if shared_value:
        actual_shared = _get_header_case_insensitive(headers, cfg.shared_header_name).strip()
        if not actual_shared or not hmac.compare_digest(actual_shared, shared_value):
            raise HTTPException(status_code=401, detail="Unauthorized intake request")

    cf_id = (cfg.cloudflare_access_client_id or "").strip()
    cf_secret = (cfg.cloudflare_access_client_secret or "").strip()
    if cf_id and cf_secret:
        actual_id = _get_header_case_insensitive(headers, "CF-Access-Client-Id").strip()
        actual_secret = _get_header_case_insensitive(headers, "CF-Access-Client-Secret").strip()

        if not actual_id or not actual_secret:
            raise HTTPException(status_code=401, detail="Unauthorized intake request")
        if not hmac.compare_digest(actual_id, cf_id) or not hmac.compare_digest(actual_secret, cf_secret):
            raise HTTPException(status_code=401, detail="Unauthorized intake request")


async def verify_intake_request_auth(request: Request, cfg: IntakeAuthConfig) -> None:
    verify_intake_headers(request.headers, cfg)
