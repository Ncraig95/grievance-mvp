from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import textwrap
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from fastapi import APIRouter, HTTPException, Request

from ..core.hmac_auth import verify_hmac
from ..core.ids import new_case_id, new_document_id, normalize_grievance_id
from ..core.intake_auth import verify_intake_request_auth
from ..db.db import Db, utcnow
from ..services.case_folder_naming import build_case_folder_member_name
from ..services.contract_timeline import parse_incident_date
from ..services.doc_render import render_docx
from ..services.grievance_id_allocator import GrievanceIdAllocationError, GrievanceIdAllocator
from ..services.notification_service import NotificationService
from ..services.pdf_convert import docx_to_pdf
from ..services.sharepoint_graph import CaseFolderAmbiguousError, CaseFolderNotFoundError
from ..services.signature_workflow import normalize_signers, send_document_for_signature
from .models import (
    CaseStatusResponse,
    ClientSuppliedFile,
    DocumentRequest,
    DocumentStatus,
    IntakeRequest,
    IntakeResponse,
)

router = APIRouter()


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DISPLAY_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9 _.-]+")
_FIELD_KEY_SAFE = re.compile(r"[^A-Za-z0-9]+")
_CLIENT_SUPPLIED_TOTAL_MAX_BYTES = 1_073_741_824
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_DEFAULT_STATEMENT_WRAP_WIDTH = 95
_DATE_FIELD_KEYS = {
    "incident_date",
    "incidentdate",
    "seniority_date",
    "senioritydate",
    "ncs_date",
    "ncsdate",
    "request_date",
    "requestdate",
    "today_date",
    "todaydate",
    "date_grievance_occurred",
    "informal_meeting_date",
    "meeting_requested_date",
}

_DOC_COMMAND_ALIASES: dict[str, str] = {
    "bellsouth_formal_grievance_meeting_request": "bellsouth_meeting_request",
    "mobility_meeting_request": "mobility_formal_grievance_meeting_request",
    "grievance_data_request": "grievance_data_request_form",
    "true_intent_brief": "true_intent_grievance_brief",
    "disciplinary_brief": "disciplinary_grievance_brief",
}
_DOC_TEMPLATE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "bellsouth_meeting_request": (
        "bellsouth_meeting_request",
        "bellsouth_formal_grievance_meeting_request",
    ),
    "mobility_formal_grievance_meeting_request": (
        "mobility_formal_grievance_meeting_request",
    ),
    "grievance_data_request_form": (
        "grievance_data_request_form",
    ),
    "true_intent_grievance_brief": (
        "true_intent_grievance_brief",
    ),
    "disciplinary_grievance_brief": (
        "disciplinary_grievance_brief",
    ),
}


def _safe_name(value: str) -> str:
    safe = _FILENAME_SAFE.sub("_", value.strip())
    return safe.strip("_") or "document"


def _safe_display_name(value: str) -> str:
    cleaned = _DISPLAY_FILENAME_SAFE.sub("", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "unknown"


def _build_document_basename(*, doc_type: str, grievance_id: str, member_name: str) -> str:
    normalized_type = doc_type.strip().lower()
    member = _safe_display_name(member_name).lower()
    if normalized_type == "statement_of_occurrence":
        return f"{grievance_id} - {member} - statement"
    if normalized_type == "bellsouth_meeting_request":
        return f"{grievance_id} - {member} - bellsouth meeting request"
    if normalized_type == "mobility_formal_grievance_meeting_request":
        return f"{grievance_id} - {member} - mobility meeting request"
    if normalized_type == "grievance_data_request_form":
        return f"{grievance_id} - {member} - grievance data request"
    if normalized_type == "true_intent_grievance_brief":
        return f"{grievance_id} - {member} - true intent grievance brief"
    if normalized_type == "disciplinary_grievance_brief":
        return f"{grievance_id} - {member} - disciplinary grievance brief"
    return _safe_name(doc_type)


def _resolve_template_path(cfg, doc_req: DocumentRequest) -> str:  # noqa: ANN001
    if doc_req.template_key and doc_req.template_key in cfg.doc_templates:
        return cfg.doc_templates[doc_req.template_key]
    if doc_req.doc_type in cfg.doc_templates:
        return cfg.doc_templates[doc_req.doc_type]
    return cfg.docx_template_path


def _normalize_field_key(key: str) -> str:
    normalized = _FIELD_KEY_SAFE.sub("_", key.strip()).strip("_").lower()
    return normalized


def _flatten_fields(value: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        composed = f"{prefix}_{key}" if prefix else key
        if isinstance(raw_val, dict):
            out.update(_flatten_fields(raw_val, composed))
        else:
            out[composed] = raw_val
    return out


def _coerce_context_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return json.dumps(value, ensure_ascii=False)


def _is_date_field_key(key: str) -> bool:
    return _normalize_field_key(key) in _DATE_FIELD_KEYS


def _format_context_date_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return value

    text = str(value).strip()
    if not text:
        return ""
    parsed = parse_incident_date(text)
    if parsed is None:
        return text
    return parsed.isoformat()


def _coerce_positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return fallback
    return parsed if parsed > 0 else fallback


def _normalize_existing_statement_lines(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    rows: list[dict[str, object]] = []
    for idx, item in enumerate(value, start=1):
        if isinstance(item, dict):
            text = str(item.get("text", ""))
        else:
            text = str(item)
        rows.append({"text": text, "line_no": idx})
    return rows


def _build_statement_line_rows(full_text: str, wrap_width: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for paragraph in full_text.splitlines():
        normalized = paragraph.strip()
        if not normalized:
            rows.append({"text": ""})
            continue
        wrapped = textwrap.wrap(
            normalized,
            width=wrap_width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if not wrapped:
            rows.append({"text": normalized})
            continue
        for line in wrapped:
            rows.append({"text": line})

    if not rows:
        rows = [{"text": ""}]

    return [{"text": str(row.get("text", "")), "line_no": idx + 1} for idx, row in enumerate(rows)]


def _apply_dynamic_statement_context(context: dict[str, object]) -> None:
    statement_text = str(context.get("statement_text", "") or "")
    continuation = str(context.get("statement_continuation", "") or "")
    joined = statement_text
    if continuation.strip():
        joined = f"{statement_text}\n{continuation}" if statement_text else continuation

    existing = _normalize_existing_statement_lines(context.get("statement_lines"))
    if existing is not None:
        rows = existing if existing else [{"text": "", "line_no": 1}]
    else:
        wrap_width = _coerce_positive_int(context.get("statement_line_wrap_width"), _DEFAULT_STATEMENT_WRAP_WIDTH)
        rows = _build_statement_line_rows(joined, wrap_width)

    context["statement_full_text"] = joined
    context["statement_lines"] = rows
    context["statement_rows"] = rows
    context["statement_line_count"] = len(rows)
    context["statement_has_continuation"] = bool(continuation.strip())


def _preferred_signer_email(payload: IntakeRequest) -> str | None:
    template_data = payload.template_data or {}
    if isinstance(template_data, dict):
        for key in ("personal_email", "personalEmail", "signer_email", "signerEmail"):
            val = template_data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    if payload.grievant_email and payload.grievant_email.strip():
        return payload.grievant_email.strip()
    return None


def _preferred_signer_email_for_doc(
    *,
    payload: IntakeRequest,
    doc_type: str,
    template_key: str | None,
    cfg,  # noqa: ANN001
) -> tuple[str | None, str]:
    policy = _get_document_policy(cfg=cfg, doc_type=doc_type, template_key=template_key)
    if policy and policy.default_signer_field and isinstance(payload.template_data, dict):
        raw = payload.template_data.get(policy.default_signer_field)
        if isinstance(raw, str) and raw.strip():
            return raw.strip(), f"template_data.{policy.default_signer_field}"
    default_signer = _preferred_signer_email(payload)
    if default_signer:
        return default_signer, "default.grievant_email"
    return None, "missing"


def _apply_statement_defaults(
    *,
    context: dict[str, object],
    payload: IntakeRequest,
    grievance_id: str,
    grievance_number: str | None,
) -> None:
    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    grievants_uid_seed = context.get("grievants_uid")
    if grievants_uid_seed in (None, ""):
        grievants_uid_seed = context.get("grievants uid")
    if grievants_uid_seed in (None, ""):
        grievants_uid_seed = grievance_id
    defaults: dict[str, object] = {
        "grievant_name": member_name,
        "work_address": payload.work_location or "",
        "home_address": "",
        "seniority_date": "",
        "ncs_date": "",
        "personal_cell": payload.grievant_phone or "",
        "personal_email": payload.grievant_email or "",
        "department": "",
        "title": "",
        "supervisor_name": payload.supervisor or "",
        "supervisor_phone": "",
        "supervisor_email": "",
        # Legacy placeholder in template includes a space.
        "grievants uid": grievants_uid_seed,
        "grievants_uid": grievants_uid_seed,
        "incident_date": _format_context_date_value(payload.incident_date),
        "article": "",
        "statement_text": payload.narrative or "",
        "statement_continuation": "",
        "witness_1_name": "",
        "witness_1_title": "",
        "witness_1_phone": "",
        "witness_2_name": "",
        "witness_2_title": "",
        "witness_2_phone": "",
        "witness_3_name": "",
        "witness_3_title": "",
        "witness_3_phone": "",
        "grievance_number": grievance_number or "",
    }
    for key, value in defaults.items():
        context.setdefault(key, value)


def _apply_bellsouth_defaults(*, context: dict[str, object], payload: IntakeRequest) -> None:
    td = payload.template_data if isinstance(payload.template_data, dict) else {}
    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    today = date.today().isoformat()

    def _pick(*keys: str, fallback: str = "") -> str:
        for key in keys:
            val = td.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return fallback

    defaults = {
        "to": _pick("to", fallback=member_name),
        "request_date": _pick("request_date", fallback=today),
        "grievant_names": _pick("grievant_names", "grievant_name", fallback=member_name),
        "grievants_attending": _pick("grievants_attending", fallback=member_name),
        "grievants_in_attendance": _pick("grievants_in_attendance", fallback=member_name),
        "date_grievance_occurred": _pick("date_grievance_occurred", "incident_date", fallback=str(payload.incident_date or "")),
        "issue_contract_section": _pick("issue_contract_section", "article", fallback=""),
        "informal_meeting_date": _pick("informal_meeting_date", fallback=""),
        "meeting_requested_date": _pick("meeting_requested_date", fallback=today),
        "meeting_requested_time": _pick("meeting_requested_time", fallback=""),
        "meeting_requested_place": _pick("meeting_requested_place", fallback=""),
        "union_rep_attending": _pick("union_rep_attending", fallback=""),
        "union_reps_in_attendance": _pick("union_reps_in_attendance", fallback=""),
        "company_reps_in_attendance": _pick("company_reps_in_attendance", fallback=""),
        "additional_info": _pick("additional_info", fallback=str(payload.narrative or "")),
        "reply_to_name_1": _pick("reply_to_name_1", fallback=""),
        "reply_to_name_2": _pick("reply_to_name_2", fallback=""),
        "reply_to_address_1": _pick("reply_to_address_1", fallback=""),
        "reply_to_address_2": _pick("reply_to_address_2", fallback=""),
    }
    for key, value in defaults.items():
        context.setdefault(key, value)


def _clamp_with_ellipsis(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    body = value[: max_chars - 1].rstrip()
    if not body:
        body = value[: max_chars - 1]
    return body + "…"


def _apply_layout_policy_context(
    *,
    cfg,  # noqa: ANN001
    doc_type: str,
    context: dict[str, object],
) -> dict[str, object]:
    rendering_cfg = getattr(cfg, "rendering", None)
    if rendering_cfg is None:
        return {"policy_applied": False, "fallback_applied": False, "clamped_fields": []}

    policy = rendering_cfg.layout_policies.get(doc_type)
    if not policy or not policy.enabled:
        return {"policy_applied": False, "fallback_applied": False, "clamped_fields": []}

    fallback_applied = False
    if policy.grievance_number_fallback == "grievance_id":
        grievance_number = str(context.get("grievance_number", "") or "").strip()
        if not grievance_number:
            grievance_id = str(context.get("grievance_id", "") or "").strip()
            if grievance_id:
                context["grievance_number"] = grievance_id
                fallback_applied = True

    clamped_fields: list[str] = []
    if policy.single_line_ellipsis:
        for key, max_chars in policy.max_chars.items():
            normalized_policy_key = _normalize_field_key(key)
            target_keys: list[str] = []
            for candidate in context.keys():
                if _normalize_field_key(str(candidate)) == normalized_policy_key:
                    target_keys.append(str(candidate))
            if not target_keys and key in context:
                target_keys.append(key)
            if not target_keys:
                continue

            changed = False
            for target_key in target_keys:
                raw = context.get(target_key)
                if raw is None:
                    continue
                text_value = str(raw)
                clamped = _clamp_with_ellipsis(text_value, int(max_chars))
                if clamped != text_value:
                    context[target_key] = clamped
                    changed = True

            if changed:
                clamped_fields.append(key)

    return {
        "policy_applied": True,
        "fallback_applied": fallback_applied,
        "clamped_fields": clamped_fields,
    }


def _validate_grievance_input_mode(grievance_mode: str, incoming_grievance_id: str) -> None:
    if grievance_mode == "auto" and incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id must be omitted when grievance_id.mode=auto",
        )
    if grievance_mode == "manual" and not incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id is required when grievance_id.mode=manual",
        )


def _sanitize_intake_payload_for_storage(payload: IntakeRequest) -> str:
    body = payload.model_dump(exclude_none=True)
    sanitized_client_files: list[dict[str, object]] = []
    for item in payload.client_supplied_files:
        sanitized_client_files.append(
            {
                "file_name": item.file_name,
                "has_download_url": bool((item.download_url or "").strip()),
                "has_content_base64": bool((item.content_base64 or "").strip()),
            }
        )
    body["client_supplied_files"] = sanitized_client_files
    return json.dumps(body, ensure_ascii=False)


def _decode_base64_payload(value: str) -> bytes:
    raw = (value or "").strip()
    if not raw:
        return b""
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw, validate=True)


def _download_to_temp_file(*, download_url: str) -> tuple[Path, int]:
    parsed = urlparse(download_url)
    if parsed.scheme.lower() != "https":
        raise RuntimeError("client file download_url must use https")

    with tempfile.NamedTemporaryFile(prefix="client-file-", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    size_bytes = 0
    try:
        with requests.get(download_url, stream=True, timeout=(10, 300)) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    out.write(chunk)
                    size_bytes += len(chunk)
                    if size_bytes > _CLIENT_SUPPLIED_TOTAL_MAX_BYTES:
                        raise RuntimeError("client supplied file exceeds 1GB limit")
        return tmp_path, size_bytes
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _materialize_client_supplied_file(item: ClientSuppliedFile) -> tuple[Path, int]:
    if item.download_url and item.download_url.strip():
        return _download_to_temp_file(download_url=item.download_url.strip())

    if item.content_base64 and item.content_base64.strip():
        content = _decode_base64_payload(item.content_base64)
        if len(content) > _CLIENT_SUPPLIED_TOTAL_MAX_BYTES:
            raise RuntimeError("client supplied file exceeds 1GB limit")
        with tempfile.NamedTemporaryFile(prefix="client-file-", delete=False) as tmp:
            tmp.write(content)
            return Path(tmp.name), len(content)

    raise RuntimeError("client supplied file requires download_url or content_base64")


async def _upload_client_supplied_files(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    logger,  # noqa: ANN001
    graph,  # noqa: ANN001
    payload: IntakeRequest,
    case_id: str,
    grievance_id: str,
    member_name: str,
    correlation_id: str,
    sharepoint_case_folder: str | None,
    sharepoint_case_web_url: str | None,
) -> tuple[str | None, str | None]:
    files = payload.client_supplied_files or []
    if not files:
        return sharepoint_case_folder, sharepoint_case_web_url

    if not cfg.graph.site_hostname or not cfg.graph.site_path or not cfg.graph.document_library:
        raise HTTPException(status_code=503, detail="SharePoint is not configured for client supplied files")

    case_folder_name = sharepoint_case_folder
    case_folder_web_url = sharepoint_case_web_url
    if not case_folder_name:
        folder_ref = graph.ensure_case_folder(
            site_hostname=cfg.graph.site_hostname,
            site_path=cfg.graph.site_path,
            library=cfg.graph.document_library,
            case_parent_folder=cfg.graph.case_parent_folder,
            grievance_id=grievance_id,
            member_name=member_name,
        )
        case_folder_name = folder_ref.folder_name
        case_folder_web_url = folder_ref.web_url

    total_uploaded_bytes = 0
    uploaded_count = 0
    for item in files:
        safe_filename = _safe_name(item.file_name)
        local_path: Path | None = None
        try:
            local_path, size_bytes = _materialize_client_supplied_file(item)
            total_uploaded_bytes += size_bytes
            if total_uploaded_bytes > _CLIENT_SUPPLIED_TOTAL_MAX_BYTES:
                raise RuntimeError("combined client supplied files exceed 1GB limit")

            uploaded = graph.upload_local_file_to_case_subfolder(
                site_hostname=cfg.graph.site_hostname,
                site_path=cfg.graph.site_path,
                library=cfg.graph.document_library,
                case_folder_name=case_folder_name,
                case_parent_folder=cfg.graph.case_parent_folder,
                subfolder=cfg.graph.client_supplied_subfolder,
                filename=safe_filename,
                local_path=str(local_path),
            )
            uploaded_count += 1
            await db.add_event(
                case_id,
                None,
                "client_supplied_file_uploaded",
                {
                    "filename": safe_filename,
                    "size_bytes": size_bytes,
                    "sharepoint_path": uploaded.path,
                },
            )
        except Exception as exc:
            logger.exception(
                "client_supplied_file_upload_failed",
                extra={"correlation_id": correlation_id, "case_id": case_id, "client_filename": safe_filename},
            )
            await db.add_event(
                case_id,
                None,
                "client_supplied_file_upload_failed",
                {"filename": safe_filename, "error": str(exc)},
            )
            raise HTTPException(status_code=503, detail="unable to upload client supplied files") from exc
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)

    await db.add_event(
        case_id,
        None,
        "client_supplied_files_upload_completed",
        {"file_count": uploaded_count, "total_bytes": total_uploaded_bytes},
    )
    return case_folder_name, case_folder_web_url


def _resolve_document_command(cfg, document_command: str) -> DocumentRequest:  # noqa: ANN001
    raw = (document_command or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="document_command cannot be empty")

    candidates: list[str] = []
    for value in (raw, _normalize_field_key(raw)):
        normalized = value.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    for key in candidates:
        normalized_key = _DOC_COMMAND_ALIASES.get(key, key)
        template_candidates: list[str] = []
        for candidate in (normalized_key, key, *_DOC_TEMPLATE_FALLBACKS.get(normalized_key, ())):
            text = str(candidate).strip()
            if text and text not in template_candidates:
                template_candidates.append(text)

        template_lookup = next((candidate for candidate in template_candidates if candidate in cfg.doc_templates), None)
        if template_lookup:
            # Command mode is a convenience for single-doc workflows (Power Automate).
            policy = _get_document_policy(cfg=cfg, doc_type=normalized_key, template_key=template_lookup)
            requires_signature = bool(policy.default_requires_signature) if policy else True
            return DocumentRequest(
                doc_type=normalized_key,
                template_key=template_lookup,
                requires_signature=requires_signature,
            )

    raise HTTPException(status_code=400, detail=f"Unknown document_command '{raw}'")


def _get_document_policy(
    *,
    cfg,  # noqa: ANN001
    doc_type: str,
    template_key: str | None,
):
    if not hasattr(cfg, "document_policies"):
        return None
    candidates: list[str] = []
    for key in (doc_type, template_key):
        text = str(key or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    for candidate in candidates:
        policy = cfg.document_policies.get(candidate)
        if policy:
            return policy
    return None


def _doc_requires_existing_exact_folder(*, cfg, doc_req: DocumentRequest) -> bool:  # noqa: ANN001
    policy = _get_document_policy(
        cfg=cfg,
        doc_type=doc_req.doc_type,
        template_key=doc_req.template_key,
    )
    if not policy:
        return False
    return policy.folder_resolution == "existing_exact_grievance_id"


def _build_template_context(
    *,
    cfg,  # noqa: ANN001
    payload: IntakeRequest,
    case_id: str,
    grievance_id: str,
    document_id: str,
    doc_type: str,
    grievance_number: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    payload_data = payload.model_dump(
        exclude={"documents", "template_data", "document_command", "client_supplied_files"}
    )
    merged_fields = _flatten_fields(payload_data)
    merged_fields.update(_flatten_fields(payload.template_data))

    context: dict[str, object] = {
        "case_id": case_id,
        "grievance_id": grievance_id,
        "grievance_number": grievance_number or "",
        "document_id": document_id,
        "document_type": doc_type,
        "created_at_utc": utcnow(),
    }
    protected_keys = set(context.keys())

    for key, raw_val in merged_fields.items():
        value = _coerce_context_value(raw_val)
        normalized = _normalize_field_key(key)
        if _is_date_field_key(normalized):
            value = _format_context_date_value(value)
        if key not in protected_keys:
            context[key] = value

        if normalized and normalized not in context:
            context[normalized] = value

    _apply_statement_defaults(
        context=context,
        payload=payload,
        grievance_id=grievance_id,
        grievance_number=grievance_number,
    )
    if doc_type == "bellsouth_meeting_request":
        _apply_bellsouth_defaults(context=context, payload=payload)
    for ctx_key in tuple(context.keys()):
        if _is_date_field_key(str(ctx_key)):
            context[ctx_key] = _format_context_date_value(context.get(ctx_key))

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    if member_name:
        context.setdefault("grievant_name", member_name)
        context.setdefault("grievant_names", member_name)

    today = date.today().isoformat()
    context.setdefault("today_date", today)
    context.setdefault("request_date", today)
    _apply_dynamic_statement_context(context)
    layout_meta = _apply_layout_policy_context(
        cfg=cfg,
        doc_type=doc_type,
        context=context,
    )

    return context, layout_meta


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
    graph = request.app.state.graph
    notifications: NotificationService = request.app.state.notifications

    client_ip = request.client.host if request.client else ""
    try:
        await verify_intake_request_auth(request, cfg.intake_auth)
    except HTTPException as exc:
        logger.warning(
            "intake_auth_failed",
            extra={
                "status_code": exc.status_code,
                "client_ip": client_ip,
                "path": str(request.url.path),
            },
        )
        raise

    try:
        body = await verify_hmac(request, cfg.hmac_shared_secret)
    except HTTPException as exc:
        logger.warning(
            "intake_hmac_failed",
            extra={
                "status_code": exc.status_code,
                "client_ip": client_ip,
                "path": str(request.url.path),
            },
        )
        raise
    payload = IntakeRequest.model_validate_json(body)

    correlation_id = payload.request_id
    logger.info(
        "intake_received",
        extra={
            "correlation_id": correlation_id,
            "request_id": payload.request_id,
            "grievance_mode": cfg.grievance_id.mode,
            "has_document_command": bool((payload.document_command or "").strip()),
            "document_count": len(payload.documents or []),
            "client_supplied_file_count": len(payload.client_supplied_files or []),
        },
    )

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

    grievance_number = (payload.grievance_number or "").strip() or None
    case_id = new_case_id()
    cdir = Path(cfg.data_root) / case_id
    cdir.mkdir(parents=True, exist_ok=True)

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    folder_member_name = build_case_folder_member_name(member_name, payload.contract)
    incoming_grievance_id = (payload.grievance_id or "").strip()
    grievance_mode = cfg.grievance_id.mode

    _validate_grievance_input_mode(grievance_mode, incoming_grievance_id)

    sharepoint_case_folder: str | None = None
    sharepoint_case_web_url: str | None = None
    if grievance_mode == "manual":
        grievance_id = normalize_grievance_id(incoming_grievance_id) or incoming_grievance_id
    else:
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=db,
            graph=request.app.state.graph,
            logger=logger,
        )
        try:
            allocation = await allocator.allocate_and_reserve_folder(
                member_name=folder_member_name,
                correlation_id=correlation_id,
            )
        except GrievanceIdAllocationError as exc:
            logger.exception("grievance_id_allocation_failed", extra={"correlation_id": correlation_id})
            raise HTTPException(status_code=503, detail="unable to allocate grievance_id") from exc
        grievance_id = allocation.grievance_id
        sharepoint_case_folder = allocation.case_folder_name
        sharepoint_case_web_url = allocation.case_folder_web_url

    if payload.documents:
        doc_requests = payload.documents
    elif (payload.document_command or "").strip():
        doc_requests = [_resolve_document_command(cfg, payload.document_command or "")]
    else:
        doc_requests = [DocumentRequest(doc_type="grievance_form", requires_signature=True)]

    requires_existing_folder = any(
        _doc_requires_existing_exact_folder(cfg=cfg, doc_req=req) for req in doc_requests
    )
    if requires_existing_folder:
        try:
            folder_ref = graph.find_case_folder_by_grievance_id_exact(
                site_hostname=cfg.graph.site_hostname,
                site_path=cfg.graph.site_path,
                library=cfg.graph.document_library,
                case_parent_folder=cfg.graph.case_parent_folder,
                grievance_id=grievance_id,
            )
            sharepoint_case_folder = folder_ref.folder_name
            sharepoint_case_web_url = folder_ref.web_url
            logger.info(
                "existing_case_folder_resolved",
                extra={
                    "correlation_id": correlation_id,
                    "grievance_id": grievance_id,
                    "folder_name": folder_ref.folder_name,
                },
            )
        except CaseFolderNotFoundError as exc:
            logger.warning(
                "existing_case_folder_not_found",
                extra={"correlation_id": correlation_id, "grievance_id": grievance_id},
            )
            raise HTTPException(
                status_code=422,
                detail=f"No existing SharePoint case folder matches grievance_id '{grievance_id}'",
            ) from exc
        except CaseFolderAmbiguousError as exc:
            logger.warning(
                "existing_case_folder_ambiguous",
                extra={
                    "correlation_id": correlation_id,
                    "grievance_id": grievance_id,
                    "match_count": len(exc.candidates),
                },
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": f"Multiple SharePoint folders match grievance_id '{grievance_id}'",
                    "candidates": exc.candidates,
                },
            ) from exc
        except Exception as exc:
            logger.exception(
                "existing_case_folder_lookup_failed",
                extra={"correlation_id": correlation_id, "grievance_id": grievance_id},
            )
            raise HTTPException(
                status_code=503,
                detail="unable to resolve existing SharePoint case folder",
            ) from exc

    try:
        await db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status,
                 grievance_number, member_name, member_email, intake_request_id, intake_payload_json,
                 sharepoint_case_folder, sharepoint_case_web_url
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                grievance_id,
                utcnow(),
                "processing",
                "pending",
                grievance_number,
                member_name,
                payload.grievant_email,
                payload.request_id,
                _sanitize_intake_payload_for_storage(payload),
                sharepoint_case_folder,
                sharepoint_case_web_url,
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
            logger.info(
                "intake_deduped_race",
                extra={"correlation_id": correlation_id, "case_id": dupe_case_id},
            )
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
    await db.add_event(
        case_id,
        None,
        "case_created",
        {
            "request_id": payload.request_id,
            "grievance_id": grievance_id,
            "grievance_number": grievance_number,
            "grievance_id_mode": grievance_mode,
            "sharepoint_case_folder": sharepoint_case_folder,
        },
    )

    try:
        sharepoint_case_folder, sharepoint_case_web_url = await _upload_client_supplied_files(
            cfg=cfg,
            db=db,
            logger=logger,
            graph=graph,
            payload=payload,
            case_id=case_id,
            grievance_id=grievance_id,
            member_name=folder_member_name,
            correlation_id=correlation_id,
            sharepoint_case_folder=sharepoint_case_folder,
            sharepoint_case_web_url=sharepoint_case_web_url,
        )
        if sharepoint_case_folder or sharepoint_case_web_url:
            await db.exec(
                "UPDATE cases SET sharepoint_case_folder=?, sharepoint_case_web_url=? WHERE id=?",
                (sharepoint_case_folder, sharepoint_case_web_url, case_id),
            )
    except HTTPException:
        await db.exec("UPDATE cases SET status=? WHERE id=?", ("failed", case_id))
        raise

    doc_statuses: list[DocumentStatus] = []
    any_signature_requested = False
    any_signature_queued = False
    any_failed = False

    for doc_req in doc_requests:
        document_id = new_document_id()
        doc_type = doc_req.doc_type.strip() or "document"
        template_path = _resolve_template_path(cfg, doc_req)
        doc_name = _build_document_basename(
            doc_type=doc_type,
            grievance_id=grievance_id,
            member_name=member_name,
        )
        ddir = cdir / document_id
        ddir.mkdir(parents=True, exist_ok=True)

        docx_path = str(ddir / f"{doc_name}.docx")
        pdf_path = str(ddir / f"{doc_name}.pdf")

        context, layout_meta = _build_template_context(
            cfg=cfg,
            payload=payload,
            case_id=case_id,
            grievance_id=grievance_id,
            document_id=document_id,
            doc_type=doc_type,
            grievance_number=grievance_number,
        )
        if layout_meta.get("policy_applied"):
            await db.add_event(
                case_id,
                document_id,
                "layout_policy_applied",
                {
                    "doc_type": doc_type,
                    "policy_key": doc_type,
                    "grievance_number_fallback_applied": bool(layout_meta.get("fallback_applied")),
                    "clamped_field_count": len(layout_meta.get("clamped_fields", [])),
                    "clamped_fields": list(layout_meta.get("clamped_fields", [])),
                },
            )

        alignment_pdf_bytes: bytes | None = None
        try:
            if doc_req.requires_signature:
                anchor_docx_path = str(ddir / f"{doc_name}.anchor.docx")
                anchor_pdf_path: str | None = None
                try:
                    render_docx(
                        template_path,
                        context,
                        anchor_docx_path,
                        strip_signature_placeholders=False,
                        normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
                    )
                    anchor_pdf_path = docx_to_pdf(
                        anchor_docx_path,
                        str(ddir),
                        cfg.libreoffice_timeout_seconds,
                    )
                    alignment_pdf_bytes = Path(anchor_pdf_path).read_bytes()
                finally:
                    Path(anchor_docx_path).unlink(missing_ok=True)
                    if anchor_pdf_path:
                        Path(anchor_pdf_path).unlink(missing_ok=True)

                render_docx(
                    template_path,
                    context,
                    docx_path,
                    strip_signature_placeholders=True,
                    normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
                )
            else:
                render_docx(
                    template_path,
                    context,
                    docx_path,
                    normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
                )
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
        await db.add_event(
            case_id,
            document_id,
            "document_created",
            {"doc_type": doc_type, "template_path": template_path},
        )

        status = initial_status
        signing_link: str | None = None

        if doc_req.requires_signature:
            preferred_signer, signer_source = _preferred_signer_email_for_doc(
                payload=payload,
                doc_type=doc_type,
                template_key=doc_req.template_key,
                cfg=cfg,
            )
            signer_order = normalize_signers(doc_req.signers, preferred_signer)
            if not signer_order:
                any_failed = True
                await db.exec("UPDATE documents SET status=? WHERE id=?", ("failed", document_id))
                await db.add_event(
                    case_id,
                    document_id,
                    "signature_signer_resolution_failed",
                    {"doc_type": doc_type, "signer_source": signer_source},
                )
                doc_statuses.append(
                    DocumentStatus(document_id=document_id, doc_type=doc_type, status="failed", signing_link=None)
                )
                continue
            await db.add_event(
                case_id,
                document_id,
                "signature_signer_resolved",
                {"doc_type": doc_type, "signer_source": signer_source},
            )
            requires_grievance_number_gate = (
                cfg.wait_for_grievance_number_before_signature and not grievance_number
            )
            if requires_grievance_number_gate:
                status = "pending_grievance_number"
                any_signature_queued = True
                await db.exec(
                    "UPDATE documents SET status=?, signer_order_json=? WHERE id=?",
                    (status, json.dumps(signer_order, ensure_ascii=False), document_id),
                )
                await db.add_event(
                    case_id,
                    document_id,
                    "signature_queued_pending_grievance_number",
                    {"doc_type": doc_type},
                )
            else:
                outcome = await send_document_for_signature(
                    cfg=cfg,
                    db=db,
                    logger=logger,
                    docuseal=docuseal,
                    notifications=notifications,
                    case_id=case_id,
                    grievance_id=grievance_id,
                    document_id=document_id,
                    doc_type=doc_type,
                    template_key=doc_req.template_key,
                    pdf_bytes=pdf_bytes,
                    alignment_pdf_bytes=alignment_pdf_bytes,
                    signer_order=signer_order,
                    correlation_id=correlation_id,
                    idempotency_prefix=f"intake:{case_id}:{document_id}",
                )
                status = outcome.status
                signing_link = outcome.signing_link
                any_signature_requested = any_signature_requested or status == "sent_for_signature"
                any_failed = any_failed or status == "failed"
        else:
            if cfg.require_approver_decision:
                status = "pending_approval"
                event_type = "no_signature_required"
            else:
                status = "approved"
                event_type = "no_signature_auto_approved"
            await db.exec("UPDATE documents SET status=? WHERE id=?", (status, document_id))
            await db.add_event(case_id, document_id, event_type, {})

        doc_statuses.append(
            DocumentStatus(document_id=document_id, doc_type=doc_type, status=status, signing_link=signing_link)
        )

    if any_signature_requested:
        case_status = "awaiting_signatures"
    elif any_signature_queued:
        case_status = "pending_grievance_number"
    elif any_failed:
        case_status = "failed"
    else:
        case_status = "pending_approval" if cfg.require_approver_decision else "approved"

    if cfg.require_approver_decision:
        await db.exec("UPDATE cases SET status=? WHERE id=?", (case_status, case_id))
    else:
        await db.exec(
            "UPDATE cases SET status=?, approval_status='approved', approved_at_utc=?, approver_email=? WHERE id=?",
            (case_status, utcnow(), "system@automation", case_id),
        )
    await db.add_event(case_id, None, "intake_completed", {"status": case_status, "document_count": len(doc_statuses)})
    logger.info(
        "intake_completed",
        extra={
            "correlation_id": correlation_id,
            "case_id": case_id,
            "grievance_id": grievance_id,
            "status": case_status,
            "document_count": len(doc_statuses),
            "signature_requested": any_signature_requested,
            "signature_queued": any_signature_queued,
            "has_failures": any_failed,
        },
    )

    return IntakeResponse(case_id=case_id, grievance_id=grievance_id, status=case_status, documents=doc_statuses)
