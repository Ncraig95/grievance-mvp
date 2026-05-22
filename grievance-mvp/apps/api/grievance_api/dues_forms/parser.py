from __future__ import annotations

import re

LABEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("Contract", "contract"),
    ("First Name", "first_name"),
    ("Last Name", "last_name"),
    ("Work Location Address", "work_location_address"),
    ("Work Location State", "work_location_state"),
    ("Employee ID", "employee_id"),
    ("Local No", "local_no"),
    ("Home Address", "home_address"),
    ("City", "city"),
    ("State", "state"),
    ("Zip", "zip"),
    ("Personal Email Address", "personal_email"),
    ("Personal Cell Phone", "personal_cell_phone"),
    ("Timestamp", "timestamp"),
    ("IP Address", "ip_address"),
    ("Dues Deduction Authorization", "dues_deduction_authorization"),
    ("Electronic Signature", "electronic_signature"),
)

CRITICAL_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "employee_id",
    "local_no",
    "electronic_signature",
    "timestamp",
)

_FIELD_BY_LABEL = {re.sub(r"[^a-z0-9]+", "", label.lower()): field for label, field in LABEL_FIELDS}
_LABEL_EXPR = "|".join(
    re.escape(label).replace(r"\ ", r"\s+")
    for label, _field in sorted(LABEL_FIELDS, key=lambda item: len(item[0]), reverse=True)
)
_LABEL_RE = re.compile(rf"(?i)(?P<label>{_LABEL_EXPR})\s*[:：]")
_REPLACEMENT_CHARS = {"�", "\ufffd"}


def _canonical_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _normalize_text(text: object) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", normalized)


def _clean_value(value: object) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
    return " ".join(line for line in lines if line).strip()


def _normalize_phone(value: str) -> str:
    return re.sub(r"[\s\-()]", "", value or "").strip()


def expected_label_count(text: object) -> int:
    normalized = _normalize_text(text)
    return len({_canonical_label(match.group("label")) for match in _LABEL_RE.finditer(normalized)})


def text_needs_ocr(text: object) -> bool:
    normalized = _normalize_text(text).strip()
    if len(normalized) < 40:
        return True
    replacement_count = sum(normalized.count(ch) for ch in _REPLACEMENT_CHARS)
    if replacement_count > max(3, int(len(normalized) * 0.03)):
        return True
    return expected_label_count(normalized) < 5


def _label_values(text: object) -> dict[str, str]:
    normalized = _normalize_text(text)
    matches = list(_LABEL_RE.finditer(normalized))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        field = _FIELD_BY_LABEL.get(_canonical_label(match.group("label")))
        if not field:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        value = _clean_value(normalized[start:end])
        values[field] = value
    return values


def _employee_id_suspicious(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if any(ch in text for ch in ("$", "�", "\ufffd")):
        return True
    return bool(re.search(r"[^A-Za-z0-9._@/\-]", text))


def parse_dues_form_text(text: object) -> dict[str, str | None]:
    raw_text = _normalize_text(text)
    values = _label_values(raw_text)

    record: dict[str, str | None] = {
        "form_type": "dues_deduction_form",
        "contract": values.get("contract", ""),
        "first_name": values.get("first_name", ""),
        "last_name": values.get("last_name", ""),
        "work_location_address": values.get("work_location_address", ""),
        "work_location_state": values.get("work_location_state", "").upper(),
        "employee_id": values.get("employee_id", ""),
        "local_no": values.get("local_no", ""),
        "home_address": values.get("home_address", ""),
        "city": values.get("city", ""),
        "state": values.get("state", "").upper(),
        "zip": values.get("zip", ""),
        "personal_email": values.get("personal_email", ""),
        "personal_cell_phone": _normalize_phone(values.get("personal_cell_phone", "")),
        "timestamp": values.get("timestamp", ""),
        "ip_address": values.get("ip_address", ""),
        "dues_deduction_authorization": values.get("dues_deduction_authorization", ""),
        "electronic_signature": values.get("electronic_signature", ""),
        "raw_text": raw_text,
    }

    missing = [field for field in CRITICAL_FIELDS if not str(record.get(field) or "").strip()]
    review_reasons: list[str] = []
    if missing:
        review_reasons.append("Missing critical fields: " + ", ".join(missing))
    if _employee_id_suspicious(str(record.get("employee_id") or "")):
        review_reasons.append("Employee ID contains suspicious OCR characters")

    record["review_status"] = "needs_review" if review_reasons else "processed"
    record["error_message"] = "; ".join(review_reasons)
    return record
