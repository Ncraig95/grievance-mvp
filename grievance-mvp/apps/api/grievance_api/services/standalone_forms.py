from __future__ import annotations

import json
import re
import textwrap
from datetime import date
from pathlib import Path

from ..core.config import StandaloneFormConfig
from ..db.db import utcnow
from ..web.models import StandaloneSubmissionRequest


_FIELD_KEY_SAFE = re.compile(r"[^A-Za-z0-9]+")
_DISPLAY_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9 _.-]+")
_SPACE_RUN = re.compile(r"\s+")
_DEFAULT_WRAP_WIDTHS = {
    "demand_text": 82,
    "reason_text": 82,
    "specific_examples_text": 82,
}


def normalize_field_key(key: str) -> str:
    return _FIELD_KEY_SAFE.sub("_", key.strip()).strip("_").lower()


def safe_display_name(value: str) -> str:
    cleaned = _DISPLAY_FILENAME_SAFE.sub("", (value or "").strip())
    cleaned = _SPACE_RUN.sub(" ", cleaned).strip()
    return cleaned or "document"


def flatten_fields(value: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        composed = f"{prefix}_{key}" if prefix else key
        if isinstance(raw_val, dict):
            out.update(flatten_fields(raw_val, composed))
        else:
            out[composed] = raw_val
    return out


def coerce_context_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return json.dumps(value, ensure_ascii=False)


def build_wrapped_rows(value: object, *, wrap_width: int) -> list[dict[str, object]]:
    text = str(value or "")
    rows: list[str] = []
    for paragraph in text.splitlines():
        normalized = paragraph.strip()
        if not normalized:
            rows.append("")
            continue
        wrapped = textwrap.wrap(
            normalized,
            width=max(20, int(wrap_width)),
            break_long_words=False,
            break_on_hyphens=False,
        )
        rows.extend(wrapped or [normalized])
    if not rows:
        rows = [""]
    return [{"text": row, "line_no": idx} for idx, row in enumerate(rows, start=1)]


def build_standalone_context(
    *,
    form_key: str,
    form_cfg: StandaloneFormConfig,
    payload: StandaloneSubmissionRequest,
    submission_id: str,
    document_id: str,
) -> dict[str, object]:
    today = date.today().isoformat()
    context: dict[str, object] = {
        "submission_id": submission_id,
        "document_id": document_id,
        "form_key": form_key,
        "form_title": form_cfg.form_label,
        "created_at_utc": utcnow(),
        "request_date": today,
        "today_date": today,
        "contract_name": "AT&T Mobility",
    }

    for key, raw_val in flatten_fields(payload.template_data).items():
        value = coerce_context_value(raw_val)
        normalized = normalize_field_key(key)
        context[key] = value
        if normalized and normalized not in context:
            context[normalized] = value

    for field_key, wrap_width in _DEFAULT_WRAP_WIDTHS.items():
        rows_key = field_key.replace("_text", "_rows")
        if rows_key in context and isinstance(context[rows_key], list):
            continue
        context[rows_key] = build_wrapped_rows(context.get(field_key, ""), wrap_width=wrap_width)

    context.setdefault("article_affected", "")
    context.setdefault("local_number", "")
    context.setdefault("demand_from_local", "")
    context.setdefault("submitting_member_title", "")
    context.setdefault("submitting_member_name", "")
    context.setdefault("demand_text", "")
    context.setdefault("reason_text", "")
    context.setdefault("specific_examples_text", "")
    context.setdefault("work_phone", "")
    context.setdefault("home_phone", "")
    context.setdefault("non_work_email", "")

    return context


def standalone_submission_dir(*, data_root: str, submission_id: str) -> Path:
    return Path(data_root) / "standalone" / submission_id


def standalone_document_dir(*, data_root: str, submission_id: str, document_id: str) -> Path:
    return standalone_submission_dir(data_root=data_root, submission_id=submission_id) / document_id


def standalone_document_basename(*, submission_id: str, form_cfg: StandaloneFormConfig) -> str:
    return f"{submission_id} - {safe_display_name(form_cfg.form_label).lower()}"


def standalone_sharepoint_root_folder(*, standalone_parent_folder: str, form_cfg: StandaloneFormConfig) -> str:
    configured_root = str(form_cfg.sharepoint_storage.root_folder or "").strip().strip("/")
    if configured_root:
        return configured_root
    return standalone_parent_folder.strip("/")


def standalone_sharepoint_folder_path(
    *,
    standalone_parent_folder: str,
    form_cfg: StandaloneFormConfig,
    submission_id: str,
) -> str:
    parts = [
        standalone_sharepoint_root_folder(
            standalone_parent_folder=standalone_parent_folder,
            form_cfg=form_cfg,
        ),
        form_cfg.sharepoint_folder_label.strip("/"),
        submission_id.strip("/"),
    ]
    return "/".join(part for part in parts if part)


def standalone_sequence_label(*, form_cfg: StandaloneFormConfig, sequence_no: int) -> str:
    prefix = str(form_cfg.sharepoint_storage.label_prefix or form_cfg.form_label).strip() or "Document"
    return f"{prefix} {max(1, int(sequence_no))}"


def standalone_numbered_sharepoint_folder_path(
    *,
    standalone_parent_folder: str,
    form_cfg: StandaloneFormConfig,
    filing_year: int,
    filing_label: str,
) -> str:
    parts = [
        standalone_sharepoint_root_folder(
            standalone_parent_folder=standalone_parent_folder,
            form_cfg=form_cfg,
        ),
    ]
    if form_cfg.sharepoint_storage.year_subfolders:
        parts.append(str(int(filing_year)))
    parts.append(filing_label.strip("/"))
    return "/".join(part for part in parts if part)


def standalone_signed_filename(*, filing_label: str) -> str:
    return f"{filing_label}.pdf"


def standalone_audit_filename(*, filing_label: str, extension: str) -> str:
    normalized = str(extension or "").strip()
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    return f"{filing_label} Audit{normalized or '.zip'}"
