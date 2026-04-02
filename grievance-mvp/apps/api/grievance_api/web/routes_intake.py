from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
import textwrap
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

import requests

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

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
from ..services.staged_signature_workflow import (
    create_or_send_stage,
    is_3g3a_staged,
    is_staged_document,
    normalize_staged_signers,
    resolve_staged_form_key,
    stage_count_for,
)
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
}

_BELL_SOUTH_TBD_KEYS = (
    "meeting_requested_date",
    "meeting_requested_time",
    "meeting_requested_place",
)
_MEETING_REQUEST_DOC_TYPES = {
    "bellsouth_meeting_request",
    "mobility_formal_grievance_meeting_request",
}
_CHECKED_MARK = "☒"
_UNCHECKED_MARK = "☐"
_3G3A_STAGE_INTERACTIVE_MARK_FIELDS = (
    "q8_is_accepted_mark",
    "q8_is_rejected_mark",
    "q8_is_appealed_mark",
    "q8_is_requested_mediation_mark",
    "q10_company_is_yes_mark",
    "q10_company_is_no_mark",
    "q10_union_is_yes_mark",
    "q10_union_is_no_mark",
)
_3G3A_STAGE2_DATE_MARKER = "{{Dte_es_:signer2:q5_l2_date}}"
_3G3A_WRAP_POLICIES: dict[str, dict[str, int]] = {
    # Render-time safety rails for long free-text blocks in fixed form sections.
    "q3_union_statement": {"max_chars": 1800, "wrap_width": 88},
    "q4_contract_basis": {"max_chars": 700, "wrap_width": 88},
    # Legacy/pre-fill compatibility if these are ever provided in payload.
    "q6_company_statement": {"max_chars": 1800, "wrap_width": 88},
    "q7_proposed_disposition_second_level": {"max_chars": 1200, "wrap_width": 88},
    "q8_union_disposition": {"max_chars": 1200, "wrap_width": 88},
}
_3G3A_CHOICE_GROUPS: tuple[dict[str, object], ...] = (
    {
        "source_keys": (
            "q1_choice",
            "q1_grievance_type",
            "contract",
            "contract_type",
            "contractType",
        ),
        "markers": {
            "bst": "q1_is_bst_mark",
            "bellsouth": "q1_is_bst_mark",
            "billing": "q1_is_billing_mark",
            "utility": "q1_is_utility_operations_mark",
            "utilities": "q1_is_utility_operations_mark",
            "utility operations": "q1_is_utility_operations_mark",
            "utility_ops": "q1_is_utility_operations_mark",
            "uo": "q1_is_utility_operations_mark",
            "other": "q1_is_other_mark",
        },
    },
    {
        "source_keys": ("q8_union_disposition_choice", "q8_union_disposition"),
        "markers": {
            "accepted": "q8_is_accepted_mark",
            "rejected": "q8_is_rejected_mark",
            "appealed": "q8_is_appealed_mark",
            "requested mediation": "q8_is_requested_mediation_mark",
            "requested_mediation": "q8_is_requested_mediation_mark",
        },
    },
    {
        "source_keys": (
            "q10_company_true_intent_choice",
            "q10_company_true_intent_exists",
            "q10_true_intent_choice",
            "q10_true_intent_exists",
        ),
        "markers": {
            "yes": "q10_company_is_yes_mark",
            "no": "q10_company_is_no_mark",
            "true": "q10_company_is_yes_mark",
            "false": "q10_company_is_no_mark",
            "1": "q10_company_is_yes_mark",
            "0": "q10_company_is_no_mark",
            "checked": "q10_company_is_yes_mark",
            "unchecked": "q10_company_is_no_mark",
        },
    },
    {
        "source_keys": (
            "q10_union_true_intent_choice",
            "q10_union_true_intent_exists",
            "q10_true_intent_choice",
            "q10_true_intent_exists",
        ),
        "markers": {
            "yes": "q10_union_is_yes_mark",
            "no": "q10_union_is_no_mark",
            "true": "q10_union_is_yes_mark",
            "false": "q10_union_is_no_mark",
            "1": "q10_union_is_yes_mark",
            "0": "q10_union_is_no_mark",
            "checked": "q10_union_is_yes_mark",
            "unchecked": "q10_union_is_no_mark",
        },
    },
)
_STAGE_SIGNATURE_PLACEHOLDER_RE = re.compile(
    r"{{\s*(?P<tag>(?P<prefix>Sig|Dte|Eml|Txt)_es_:signer(?P<signer>\d+):(?P<field>[A-Za-z0-9_]+))\s*}}",
    flags=re.IGNORECASE,
)

_DOC_COMMAND_ALIASES: dict[str, str] = {
    "bellsouth_formal_grievance_meeting_request": "bellsouth_meeting_request",
    "mobility_meeting_request": "mobility_formal_grievance_meeting_request",
    "grievance_data_request": "grievance_data_request_form",
    "true_intent_brief": "true_intent_grievance_brief",
    "disciplinary_brief": "disciplinary_grievance_brief",
    "settlement_form": "settlement_form_3106",
    "mobility_record_of_grievance": "mobility_record_of_grievance",
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
    "settlement_form_3106": (
        "settlement_form_3106",
        "settlement_form",
    ),
    "mobility_record_of_grievance": (
        "mobility_record_of_grievance",
    ),
}


def _safe_name(value: str) -> str:
    safe = _FILENAME_SAFE.sub("_", value.strip())
    return safe.strip("_") or "document"


def _safe_display_name(value: str) -> str:
    cleaned = _DISPLAY_FILENAME_SAFE.sub("", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "unknown"


def _stage_alignment_path(*, doc_dir: Path, stage_no: int) -> Path:
    return doc_dir / "stage_alignments" / f"stage{int(stage_no)}_alignment.pdf"


def _rewrite_signature_placeholders_for_stage(xml_text: str, *, stage_no: int) -> str:
    source_signer = int(stage_no)

    def _replace(match: re.Match[str]) -> str:
        signer_no = int(match.group("signer"))
        if signer_no != source_signer:
            return ""
        remapped = re.sub(r"signer\d+", "signer1", match.group("tag"), count=1, flags=re.IGNORECASE)
        return "{{" + remapped + "}}"

    return _STAGE_SIGNATURE_PLACEHOLDER_RE.sub(_replace, xml_text)


def _create_stage_alignment_pdf_from_anchor_docx(
    *,
    anchor_docx_path: str,
    doc_dir: Path,
    stage_no: int,
    libreoffice_timeout_seconds: int,
    docx_pdf_engine: str,
    graph,  # noqa: ANN001
    graph_site_hostname: str,
    graph_site_path: str,
    graph_library: str,
    graph_temp_folder_path: str,
) -> bytes:
    stage_anchor_docx_path = doc_dir / "stage_alignments" / f"stage{int(stage_no)}_anchor.docx"
    stage_anchor_docx_path.parent.mkdir(parents=True, exist_ok=True)
    stage_anchor_pdf_path: str | None = None
    try:
        with ZipFile(anchor_docx_path, "r") as zin, ZipFile(stage_anchor_docx_path, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                    patched = data.decode("utf-8", errors="ignore")
                    patched = _rewrite_signature_placeholders_for_stage(
                        patched,
                        stage_no=stage_no,
                    )
                    data = patched.encode("utf-8")
                zout.writestr(info, data)
        stage_anchor_pdf_path = docx_to_pdf(
            str(stage_anchor_docx_path),
            str(stage_anchor_docx_path.parent),
            libreoffice_timeout_seconds,
            engine=docx_pdf_engine,
            graph_uploader=graph,
            graph_site_hostname=graph_site_hostname,
            graph_site_path=graph_site_path,
            graph_library=graph_library,
            graph_temp_folder_path=graph_temp_folder_path,
        )
        stage_bytes = Path(stage_anchor_pdf_path).read_bytes()
        _stage_alignment_path(doc_dir=doc_dir, stage_no=stage_no).write_bytes(stage_bytes)
        return stage_bytes
    finally:
        stage_anchor_docx_path.unlink(missing_ok=True)
        if stage_anchor_pdf_path:
            Path(stage_anchor_pdf_path).unlink(missing_ok=True)


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
    if normalized_type == "mobility_record_of_grievance":
        return f"{grievance_id} - {member} - mobility record of grievance"
    if normalized_type in {"settlement_form", "settlement_form_3106"}:
        return f"{grievance_id} - {member} - settlement form 3106"
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


def _normalize_choice_value(value: str) -> str:
    return " ".join(_normalize_field_key(value).replace("_", " ").split())


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


def _normalize_existing_line_rows(value: object) -> list[dict[str, object]] | None:
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


def _normalize_existing_statement_lines(value: object) -> list[dict[str, object]] | None:
    # Legacy alias retained for existing statement tests/template logic.
    return _normalize_existing_line_rows(value)


def _build_wrapped_line_rows(full_text: str, wrap_width: int) -> list[dict[str, object]]:
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


def _build_statement_line_rows(full_text: str, wrap_width: int) -> list[dict[str, object]]:
    # Legacy alias retained for existing statement tests/template logic.
    return _build_wrapped_line_rows(full_text, wrap_width)


def _pick_first_non_empty_context_value(context: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        raw = context.get(key)
        if raw is None:
            continue
        text = str(raw)
        if text.strip():
            return text
    return ""


def _apply_dynamic_rows_context(
    *,
    context: dict[str, object],
    source_keys: tuple[str, ...],
    continuation_keys: tuple[str, ...],
    lines_key: str,
    rows_key: str,
    full_text_key: str,
    line_count_key: str,
    has_continuation_key: str | None,
    wrap_width_key: str,
) -> None:
    primary_text = _pick_first_non_empty_context_value(context, source_keys)
    continuation = _pick_first_non_empty_context_value(context, continuation_keys) if continuation_keys else ""
    joined = primary_text
    if continuation.strip():
        joined = f"{primary_text}\n{continuation}" if primary_text else continuation

    existing = _normalize_existing_line_rows(context.get(lines_key))
    if existing is None:
        existing = _normalize_existing_line_rows(context.get(rows_key))
    if existing is not None:
        rows = existing if existing else [{"text": "", "line_no": 1}]
    else:
        wrap_width = _coerce_positive_int(context.get(wrap_width_key), _DEFAULT_STATEMENT_WRAP_WIDTH)
        rows = _build_wrapped_line_rows(joined, wrap_width)

    context[full_text_key] = joined
    context[lines_key] = rows
    context[rows_key] = rows
    context[line_count_key] = len(rows)
    if has_continuation_key:
        context[has_continuation_key] = bool(continuation.strip())


def _apply_dynamic_statement_context(context: dict[str, object]) -> None:
    _apply_dynamic_rows_context(
        context=context,
        source_keys=("statement_text", "narrative"),
        continuation_keys=("statement_continuation",),
        lines_key="statement_lines",
        rows_key="statement_rows",
        full_text_key="statement_full_text",
        line_count_key="statement_line_count",
        has_continuation_key="statement_has_continuation",
        wrap_width_key="statement_line_wrap_width",
    )


def _is_settlement_doc_type(doc_type: str) -> bool:
    normalized = (doc_type or "").strip().lower()
    return normalized in {"settlement_form_3106", "settlement_form"}


def _apply_dynamic_settlement_context(context: dict[str, object]) -> None:
    issue_article = _pick_first_non_empty_context_value(context, ("issue_article", "article"))
    context.setdefault("issue_article", issue_article)

    _apply_dynamic_rows_context(
        context=context,
        source_keys=("issue_text", "issue_and_article_text", "issue_contract_section", "narrative"),
        continuation_keys=("issue_continuation",),
        lines_key="issue_lines",
        rows_key="issue_rows",
        full_text_key="issue_full_text",
        line_count_key="issue_line_count",
        has_continuation_key="issue_has_continuation",
        wrap_width_key="issue_line_wrap_width",
    )
    _apply_dynamic_rows_context(
        context=context,
        source_keys=(
            "settlement_text",
            "settlement_terms",
            "union_proposed_settlement",
            "company_proposed_settlement",
        ),
        continuation_keys=("settlement_continuation",),
        lines_key="settlement_lines",
        rows_key="settlement_rows",
        full_text_key="settlement_full_text",
        line_count_key="settlement_line_count",
        has_continuation_key="settlement_has_continuation",
        wrap_width_key="settlement_line_wrap_width",
    )


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
        "meeting_requested_date": _pick("meeting_requested_date", fallback=""),
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

    # Safety-net for Power Automate autofill: allow missing/empty/null and normalize to plain-text TBD.
    for key in _BELL_SOUTH_TBD_KEYS:
        raw = context.get(key)
        text = "" if raw is None else str(raw).strip()
        if not text or text.lower() == "null":
            context[key] = "TBD"


def _apply_3g3a_defaults(*, context: dict[str, object], grievance_id: str) -> None:
    def _pick(*keys: str, fallback: str = "") -> str:
        for key in keys:
            raw = context.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                return text
        return fallback

    # Explicit local grievance number on form; fallback to grievance_id.
    context.setdefault(
        "local_grievance_number",
        _pick("local_grievance_number", "local_grievance_id", fallback=grievance_id),
    )
    # This date must be captured by stage-2 manager in DocuSeal.
    # Keep the prefill slot as a DocuSeal date marker regardless of intake payload value.
    context["q5_second_level_meeting_date"] = _3G3A_STAGE2_DATE_MARKER
    baseline_date = _pick("q1_occurred_date", "incident_date", "date_grievance_occurred", fallback=date.today().isoformat())
    for key in ("q5_informal_meeting_date", "q5_3g3r_issued_date"):
        raw = context.get(key)
        if raw is None or not str(raw).strip():
            context[key] = baseline_date

    # Single-choice checkbox groups rendered as text marks.
    for group in _3G3A_CHOICE_GROUPS:
        markers = group.get("markers")
        if not isinstance(markers, dict) or not markers:
            continue
        for marker_field in markers.values():
            context.setdefault(str(marker_field), _UNCHECKED_MARK)

        selected = _pick(*[str(k) for k in group.get("source_keys", ())], fallback="")
        if not selected:
            continue
        selected_normalized = _normalize_choice_value(selected)
        chosen_marker = None
        normalized_markers: list[tuple[str, str]] = []
        for candidate, marker_field in markers.items():
            candidate_normalized = _normalize_choice_value(str(candidate))
            normalized_markers.append((candidate_normalized, str(marker_field)))
            if selected_normalized == candidate_normalized:
                chosen_marker = str(marker_field)
                break
        if not chosen_marker and selected_normalized:
            tokenized_selected = f" {selected_normalized} "
            for candidate_normalized, marker_field in normalized_markers:
                if not candidate_normalized:
                    continue
                if f" {candidate_normalized} " in tokenized_selected:
                    chosen_marker = marker_field
                    break
        if not chosen_marker:
            continue
        for marker_field in markers.values():
            marker_key = str(marker_field)
            context[marker_key] = _CHECKED_MARK if marker_key == chosen_marker else _UNCHECKED_MARK

    # Q1 "Other" free-text should only print when Other is selected.
    q1_selected = _pick("q1_choice", "q1_grievance_type", fallback="")
    q1_selected_normalized = _normalize_choice_value(q1_selected)
    q1_is_other = q1_selected_normalized == "other"
    if q1_is_other:
        q1_other_text = _pick("q1_other_text", "q1_other", "q1_grievance_type", fallback="")
        context["q1_grievance_type"] = q1_other_text if q1_other_text else ""
    else:
        context["q1_grievance_type"] = ""

    # Stable wrapping/clamping for fixed-layout narrative sections.
    for field_key, policy in _3G3A_WRAP_POLICIES.items():
        raw = context.get(field_key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        max_chars = int(policy.get("max_chars", 0))
        wrap_width = max(1, int(policy.get("wrap_width", 88)))
        if max_chars > 0 and len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        wrapped = textwrap.fill(
            text,
            width=wrap_width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        context[field_key] = wrapped


def _clear_3g3a_stage_interactive_marks(*, context: dict[str, object]) -> None:
    for marker_key in _3G3A_STAGE_INTERACTIVE_MARK_FIELDS:
        context[marker_key] = _UNCHECKED_MARK


def _apply_mobility_record_defaults(
    *,
    context: dict[str, object],
    grievance_id: str,
    grievance_number: str | None,
) -> None:
    def _pick(*keys: str, fallback: str = "") -> str:
        for key in keys:
            raw = context.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text:
                return text
        return fallback

    context.setdefault(
        "cw_grievance_number",
        _pick("grievance_number", fallback=str(grievance_number or "").strip() or grievance_id),
    )
    context.setdefault("district_grievance_number", _pick("district_grievance_number", fallback=""))
    context.setdefault("date_grievance_occurred", _pick("date_grievance_occurred", "incident_date", fallback=""))
    context.setdefault("department", _pick("department", fallback=""))
    context.setdefault("specific_location_state", _pick("specific_location_state", "work_location", fallback=""))
    context.setdefault("local_number", _pick("local_number", fallback=""))
    context.setdefault(
        "employee_work_group_name",
        _pick("employee_work_group_name", "grievant_name", fallback=""),
    )
    context.setdefault("job_title", _pick("job_title", "title", fallback=""))
    context.setdefault("ncs_date", _pick("ncs_date", fallback=""))
    context.setdefault("union_statement", _pick("union_statement", "narrative", fallback=""))
    context.setdefault("contract_articles", _pick("contract_articles", "article", "articles", fallback=""))
    context.setdefault("date_informal", _pick("date_informal", "informal_meeting_date", fallback=""))
    context.setdefault(
        "date_first_step_requested",
        _pick("date_first_step_requested", "first_level_request_sent_date", fallback=""),
    )
    context.setdefault(
        "date_first_step_held",
        _pick("date_first_step_held", "first_level_meeting_date", fallback=""),
    )


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


def _validate_existing_folder_mode(incoming_grievance_id: str) -> None:
    if not incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id is required for existing-folder document flows",
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


def _archive_failed_document_for_review(
    *,
    cfg,  # noqa: ANN001
    case_id: str,
    document_id: str,
    doc_type: str,
    grievance_id: str,
    reason: str,
    details: dict[str, object],
    working_dir: Path,
) -> Path:
    failed_root = Path(cfg.data_root) / "failed_processes" / case_id / document_id
    failed_root.mkdir(parents=True, exist_ok=True)

    for candidate in working_dir.glob("*"):
        if not candidate.is_file():
            continue
        target = failed_root / candidate.name
        try:
            shutil.copy2(candidate, target)
        except Exception:
            continue

    summary = {
        "case_id": case_id,
        "document_id": document_id,
        "doc_type": doc_type,
        "grievance_id": grievance_id,
        "reason": reason,
        "details": details,
        "source_working_dir": str(working_dir),
    }
    (failed_root / "failure_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return failed_root


async def _mirror_failed_archive_to_sharepoint(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    graph,  # noqa: ANN001
    case_id: str,
    document_id: str,
    archive_dir: Path,
) -> None:
    if not (cfg.graph.site_hostname and cfg.graph.site_path and cfg.graph.document_library):
        return
    base_folder = (cfg.graph.failed_processes_folder or "").strip().strip("/")
    if not base_folder:
        return

    target_folder = f"{base_folder}/{case_id}/{document_id}"
    uploaded = 0
    for item in sorted(archive_dir.glob("*")):
        if not item.is_file():
            continue
        uploaded_ref = graph.upload_local_file_to_folder_path(
            site_hostname=cfg.graph.site_hostname,
            site_path=cfg.graph.site_path,
            library=cfg.graph.document_library,
            folder_path=target_folder,
            filename=item.name,
            local_path=str(item),
        )
        uploaded += 1
        await db.add_event(
            case_id,
            document_id,
            "failed_archive_sharepoint_file_uploaded",
            {
                "filename": item.name,
                "path": uploaded_ref.path,
                "web_url": uploaded_ref.web_url,
            },
        )
    await db.add_event(
        case_id,
        document_id,
        "failed_archive_sharepoint_uploaded",
        {"target_folder": target_folder, "file_count": uploaded},
    )


def _normalize_name_fields(raw_payload: dict[str, object]) -> dict[str, object]:
    out = dict(raw_payload)
    first = str(out.get("grievant_firstname", "") or "").strip()
    last = str(out.get("grievant_lastname", "") or "").strip()
    if first and last:
        return out

    full_name_candidates = [
        out.get("grievant_name"),
        out.get("grievant_full_name"),
        out.get("member_name"),
    ]
    template_data = out.get("template_data")
    if isinstance(template_data, dict):
        full_name_candidates.extend(
            [
                template_data.get("grievant_name"),
                template_data.get("grievant_full_name"),
                template_data.get("member_name"),
            ]
        )

    full_name = ""
    for candidate in full_name_candidates:
        if isinstance(candidate, str) and candidate.strip():
            full_name = candidate.strip()
            break
    if not full_name:
        return out

    parts = full_name.split()
    if not first and parts:
        out["grievant_firstname"] = parts[0]
    if not last and len(parts) >= 2:
        out["grievant_lastname"] = " ".join(parts[1:])
    if not last and len(parts) == 1:
        out["grievant_lastname"] = "Unknown"
    return out


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


def _doc_uses_auto_grievance_id(doc_req: DocumentRequest) -> bool:
    doc_type = (doc_req.doc_type or "").strip().lower()
    template_key = (doc_req.template_key or "").strip().lower()
    statement_keys = {"statement_of_occurrence", "grievance_form"}
    return doc_type in statement_keys or template_key in statement_keys


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
    if not str(context.get("grievance_number", "") or "").strip():
        protected_keys.discard("grievance_number")

    for key, raw_val in merged_fields.items():
        value = _coerce_context_value(raw_val)
        normalized = _normalize_field_key(key)
        if _is_date_field_key(normalized):
            value = _format_context_date_value(value)
        if key not in protected_keys:
            context[key] = value

        if normalized and normalized not in context:
            context[normalized] = value

    if _is_settlement_doc_type(doc_type):
        grievance_display_number = _pick_first_non_empty_context_value(
            context,
            ("grievance_number", "grievance number"),
        )
        if not grievance_display_number:
            grievance_display_number = str(grievance_id or "").strip()
        context["grievance_number"] = grievance_display_number

    _apply_statement_defaults(
        context=context,
        payload=payload,
        grievance_id=grievance_id,
        grievance_number=grievance_number,
    )
    if doc_type in _MEETING_REQUEST_DOC_TYPES:
        _apply_bellsouth_defaults(context=context, payload=payload)
    if doc_type == "bst_grievance_form_3g3a":
        _apply_3g3a_defaults(context=context, grievance_id=grievance_id)
    if doc_type == "mobility_record_of_grievance":
        _apply_mobility_record_defaults(
            context=context,
            grievance_id=grievance_id,
            grievance_number=grievance_number,
        )
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
    _apply_dynamic_settlement_context(context)
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
    try:
        raw_payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    if not isinstance(raw_payload, dict):
        raise HTTPException(status_code=400, detail="intake payload must be a JSON object")

    normalized_payload = _normalize_name_fields(raw_payload)
    try:
        payload = IntakeRequest.model_validate(normalized_payload)
    except ValidationError as exc:
        details = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", []))
            msg = str(err.get("msg", "invalid field"))
            details.append({"field": loc, "message": msg})
        raise HTTPException(
            status_code=422,
            detail={
                "message": "intake validation failed",
                "errors": details,
            },
        ) from exc

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

    if payload.documents:
        doc_requests = payload.documents
    elif (payload.document_command or "").strip():
        doc_requests = [_resolve_document_command(cfg, payload.document_command or "")]
    else:
        doc_requests = [DocumentRequest(doc_type="grievance_form", requires_signature=True)]

    requires_existing_folder = any(
        _doc_requires_existing_exact_folder(cfg=cfg, doc_req=req) for req in doc_requests
    )
    uses_auto_id_flow = all(_doc_uses_auto_grievance_id(req) for req in doc_requests)

    if requires_existing_folder:
        _validate_existing_folder_mode(incoming_grievance_id)
    elif uses_auto_id_flow:
        _validate_grievance_input_mode(grievance_mode, incoming_grievance_id)
    elif not incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id is required for non-statement document flows",
        )

    sharepoint_case_folder: str | None = None
    sharepoint_case_web_url: str | None = None
    if requires_existing_folder:
        grievance_id = normalize_grievance_id(incoming_grievance_id) or incoming_grievance_id
    elif not uses_auto_id_flow:
        grievance_id = normalize_grievance_id(incoming_grievance_id) or incoming_grievance_id
    elif grievance_mode == "manual":
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
        staged_form_key = resolve_staged_form_key(cfg=cfg, doc_type=doc_type, template_key=doc_req.template_key)
        is_staged_document_flow = staged_form_key is not None
        render_context = context
        if doc_req.requires_signature and is_staged_3g3a_document(cfg=cfg, doc_type=doc_type, template_key=doc_req.template_key):
            render_context = dict(context)
            _clear_3g3a_stage_interactive_marks(context=render_context)
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
                        render_context,
                        anchor_docx_path,
                        strip_signature_placeholders=False,
                        normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
                    )
                    anchor_pdf_path = docx_to_pdf(
                        anchor_docx_path,
                        str(ddir),
                        cfg.libreoffice_timeout_seconds,
                        engine=cfg.docx_pdf_engine,
                        graph_uploader=graph,
                        graph_site_hostname=cfg.graph.site_hostname,
                        graph_site_path=cfg.graph.site_path,
                        graph_library=cfg.graph.document_library,
                        graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
                    )
                    alignment_pdf_bytes = Path(anchor_pdf_path).read_bytes()
                    if is_staged_document_flow:
                        # Build stage-specific alignment PDFs so each stage can activate only its own fields.
                        first_stage_bytes: bytes | None = None
                        for stage_no in range(1, stage_count_for(form_key=staged_form_key) + 1):
                            stage_bytes = _create_stage_alignment_pdf_from_anchor_docx(
                                anchor_docx_path=anchor_docx_path,
                                doc_dir=ddir,
                                stage_no=stage_no,
                                libreoffice_timeout_seconds=cfg.libreoffice_timeout_seconds,
                                docx_pdf_engine=cfg.docx_pdf_engine,
                                graph=graph,
                                graph_site_hostname=cfg.graph.site_hostname,
                                graph_site_path=cfg.graph.site_path,
                                graph_library=cfg.graph.document_library,
                                graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
                            )
                            if stage_no == 1:
                                first_stage_bytes = stage_bytes
                        alignment_pdf_bytes = first_stage_bytes
                finally:
                    Path(anchor_docx_path).unlink(missing_ok=True)
                    if anchor_pdf_path:
                        Path(anchor_pdf_path).unlink(missing_ok=True)

                render_docx(
                    template_path,
                    render_context,
                    docx_path,
                    strip_signature_placeholders=True,
                    normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
                )
            else:
                render_docx(
                    template_path,
                    render_context,
                    docx_path,
                    normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
                )
            pdf_path = docx_to_pdf(
                docx_path,
                str(ddir),
                cfg.libreoffice_timeout_seconds,
                engine=cfg.docx_pdf_engine,
                graph_uploader=graph,
                graph_site_hostname=cfg.graph.site_hostname,
                graph_site_path=cfg.graph.site_path,
                graph_library=cfg.graph.document_library,
                graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
            )
            pdf_bytes = Path(pdf_path).read_bytes()
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        except Exception as exc:
            any_failed = True
            failed_archive_path = _archive_failed_document_for_review(
                cfg=cfg,
                case_id=case_id,
                document_id=document_id,
                doc_type=doc_type,
                grievance_id=grievance_id,
                reason="render_failed",
                details={"error": str(exc), "template_path": template_path},
                working_dir=ddir,
            )
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
                {
                    "error": str(exc),
                    "doc_type": doc_type,
                    "template_path": template_path,
                    "failed_archive_path": str(failed_archive_path),
                },
            )
            try:
                await _mirror_failed_archive_to_sharepoint(
                    cfg=cfg,
                    db=db,
                    graph=graph,
                    case_id=case_id,
                    document_id=document_id,
                    archive_dir=failed_archive_path,
                )
            except Exception as mirror_exc:
                await db.add_event(
                    case_id,
                    document_id,
                    "failed_archive_sharepoint_upload_failed",
                    {"error": str(mirror_exc)},
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
            if is_staged_document(cfg=cfg, doc_type=doc_type, template_key=doc_req.template_key):
                staged_signers = normalize_staged_signers(doc_req.signers, form_key=staged_form_key)
                required_signer_count = stage_count_for(form_key=staged_form_key)
                if not staged_signers:
                    any_failed = True
                    await db.exec("UPDATE documents SET status=?, signer_order_json=? WHERE id=?", ("failed", "[]", document_id))
                    await db.add_event(
                        case_id,
                        document_id,
                        "staged_signature_signers_required",
                        {
                            "doc_type": doc_type,
                            "required_count": required_signer_count,
                            "provided_count": len(doc_req.signers or []),
                        },
                    )
                    doc_statuses.append(
                        DocumentStatus(document_id=document_id, doc_type=doc_type, status="failed", signing_link=None)
                    )
                    continue
                await db.exec(
                    "UPDATE documents SET signer_order_json=? WHERE id=?",
                    (json.dumps(staged_signers, ensure_ascii=False), document_id),
                )
                stage_outcome = await create_or_send_stage(
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
                    signer_email=staged_signers[0],
                    full_signer_chain=staged_signers,
                    stage_no=1,
                    correlation_id=correlation_id,
                    idempotency_prefix=f"intake-stage:{case_id}:{document_id}:1",
                )
                status = stage_outcome.status
                signing_link = stage_outcome.signing_link
                any_signature_requested = any_signature_requested or status.startswith("sent_for_signature")
                any_failed = any_failed or status == "failed"
                doc_statuses.append(
                    DocumentStatus(document_id=document_id, doc_type=doc_type, status=status, signing_link=signing_link)
                )
                continue

            preferred_signer, signer_source = _preferred_signer_email_for_doc(
                payload=payload,
                doc_type=doc_type,
                template_key=doc_req.template_key,
                cfg=cfg,
            )
            signer_order = normalize_signers(doc_req.signers, preferred_signer)
            if not signer_order:
                any_failed = True
                failed_archive_path = _archive_failed_document_for_review(
                    cfg=cfg,
                    case_id=case_id,
                    document_id=document_id,
                    doc_type=doc_type,
                    grievance_id=grievance_id,
                    reason="signature_signer_resolution_failed",
                    details={"signer_source": signer_source},
                    working_dir=ddir,
                )
                await db.exec("UPDATE documents SET status=? WHERE id=?", ("failed", document_id))
                await db.add_event(
                    case_id,
                    document_id,
                    "signature_signer_resolution_failed",
                    {
                        "doc_type": doc_type,
                        "signer_source": signer_source,
                        "failed_archive_path": str(failed_archive_path),
                    },
                )
                try:
                    await _mirror_failed_archive_to_sharepoint(
                        cfg=cfg,
                        db=db,
                        graph=graph,
                        case_id=case_id,
                        document_id=document_id,
                        archive_dir=failed_archive_path,
                    )
                except Exception as mirror_exc:
                    await db.add_event(
                        case_id,
                        document_id,
                        "failed_archive_sharepoint_upload_failed",
                        {"error": str(mirror_exc)},
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
                if status == "failed":
                    failed_archive_path = _archive_failed_document_for_review(
                        cfg=cfg,
                        case_id=case_id,
                        document_id=document_id,
                        doc_type=doc_type,
                        grievance_id=grievance_id,
                        reason="signature_submission_failed",
                        details={"template_key": doc_req.template_key or "", "signer_count": len(signer_order)},
                        working_dir=ddir,
                    )
                    await db.add_event(
                        case_id,
                        document_id,
                        "failed_document_archived",
                        {"failed_archive_path": str(failed_archive_path), "reason": "signature_submission_failed"},
                    )
                    try:
                        await _mirror_failed_archive_to_sharepoint(
                            cfg=cfg,
                            db=db,
                            graph=graph,
                            case_id=case_id,
                            document_id=document_id,
                            archive_dir=failed_archive_path,
                        )
                    except Exception as mirror_exc:
                        await db.add_event(
                            case_id,
                            document_id,
                            "failed_archive_sharepoint_upload_failed",
                            {"error": str(mirror_exc)},
                        )
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
