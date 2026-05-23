from __future__ import annotations

import re

FILENAME_IGNORE_MARKERS: tuple[tuple[str, str], ...] = (
    ("coj", "filename_contains_coj"),
    ("cityofjacksonville", "filename_contains_cityofjacksonville"),
    ("city_of_jacksonville", "filename_contains_city_of_jacksonville"),
    ("perc_card", "filename_contains_perc_card"),
)

TEXT_IGNORE_MARKERS: tuple[tuple[str, str], ...] = (
    ("employee no", "employee_no"),
    ("employment category", "employment_category"),
    ("pay grade", "pay_grade"),
    ("job code", "job_code"),
    ("job title", "job_title"),
    ("flsa status", "flsa_status"),
    ("department", "department"),
    ("division", "division"),
    ("public record exempt", "public_record_exempt"),
    ("work phone", "work_phone"),
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def should_ignore_filename(filename: str) -> tuple[bool, str]:
    lowered = str(filename or "").lower()
    for marker, reason in FILENAME_IGNORE_MARKERS:
        if marker in lowered:
            return True, reason
    return False, ""


def should_ignore_text(text: str) -> tuple[bool, str]:
    normalized = _normalize_text(text)
    if not normalized:
        return False, ""
    if "city of jacksonville" in normalized:
        return True, "text_contains_city_of_jacksonville"

    matched_reasons = [reason for marker, reason in TEXT_IGNORE_MARKERS if marker in normalized]
    if matched_reasons:
        return True, f"text_contains_{'_'.join(matched_reasons[:2])}"
    return False, ""
