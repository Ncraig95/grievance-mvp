from __future__ import annotations

import json
import re


_INVALID_SHAREPOINT_NAME_CHARS = re.compile(r"[\"*:<>?/\\\\|]+")
_SPACE_RUN = re.compile(r"\s+")


def _sanitize_folder_component(value: str) -> str:
    cleaned = _INVALID_SHAREPOINT_NAME_CHARS.sub(" ", value.strip())
    return _SPACE_RUN.sub(" ", cleaned).strip(" .")


def build_case_folder_member_name(member_name: str, contract: str | None) -> str:
    base_name = _sanitize_folder_component(member_name) or "Member"
    contract_name = _sanitize_folder_component(contract or "")
    if contract_name:
        return f"{base_name} - {contract_name}"
    return base_name


def resolve_contract_label(intake_payload_json: str | None) -> str | None:
    if not intake_payload_json:
        return None
    try:
        payload = json.loads(intake_payload_json)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    for key in ("contract", "contract_type", "contractType"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
