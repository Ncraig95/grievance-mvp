from __future__ import annotations

import json

from ..db.db import Db
from .standalone_forms import build_wrapped_rows

MOTION_SHEET_FORM_KEY = "motion_sheet"
MOTION_SHEET_SETTINGS_KEY = "motion_sheet"
MOTION_TEXT_WRAP_WIDTH = 88
MOTION_SHEET_OFFICER_COUNT = 10
DEFAULT_MOTION_SHEET_OFFICERS = (
    "Josh Denmark",
    "Derek Williamson",
    "John Rice",
    "Chris Gaston",
    "",
    "",
    "",
    "",
    "",
    "",
)


def normalize_motion_sheet_officers(values: object) -> tuple[str, ...]:
    if isinstance(values, dict):
        raw_values = [values.get(f"officer_{idx}_name", "") for idx in range(1, MOTION_SHEET_OFFICER_COUNT + 1)]
    elif isinstance(values, (list, tuple)):
        raw_values = list(values)
    else:
        raw_values = []

    cleaned = [str(value or "").strip() for value in raw_values[:MOTION_SHEET_OFFICER_COUNT]]
    while len(cleaned) < MOTION_SHEET_OFFICER_COUNT:
        cleaned.append("")

    defaults = list(DEFAULT_MOTION_SHEET_OFFICERS)
    return tuple(cleaned[idx] or defaults[idx] for idx in range(MOTION_SHEET_OFFICER_COUNT))


async def load_motion_sheet_officers(db: Db) -> tuple[str, ...]:
    row = await db.app_setting(MOTION_SHEET_SETTINGS_KEY)
    if not row:
        return DEFAULT_MOTION_SHEET_OFFICERS
    try:
        parsed = json.loads(str(row[0] or "{}"))
    except Exception:
        return DEFAULT_MOTION_SHEET_OFFICERS
    if not isinstance(parsed, dict):
        return DEFAULT_MOTION_SHEET_OFFICERS
    return normalize_motion_sheet_officers(parsed.get("officers"))


async def save_motion_sheet_officers(
    db: Db,
    *,
    officers: object,
    updated_by: str | None,
) -> tuple[str, ...]:
    normalized = normalize_motion_sheet_officers(officers)
    await db.upsert_app_setting(
        setting_key=MOTION_SHEET_SETTINGS_KEY,
        setting={"officers": list(normalized)},
        updated_by=updated_by,
    )
    return normalized


def motion_sheet_context_defaults(officers: tuple[str, ...]) -> dict[str, object]:
    context: dict[str, object] = {
        "motion_made_by": "",
        "motion_text": "",
        "motion_text_rows": [{"text": "", "line_no": 1}],
        "motion_text_line_count": 1,
        "seconded_by": "",
        "result": "",
        "motion_date": "",
        "notes": "",
    }
    normalized_officers = normalize_motion_sheet_officers(officers)
    for idx, name in enumerate(normalized_officers, start=1):
        context[f"officer_{idx}_name"] = name
        for vote_key in ("present", "absent", "yes", "no", "abstained"):
            context.setdefault(f"officer_{idx}_{vote_key}", "")
    return context


def build_motion_sheet_context(
    *,
    base_context: dict[str, object],
    officers: tuple[str, ...],
) -> dict[str, object]:
    context = motion_sheet_context_defaults(officers)
    context["motion_made_by"] = str(base_context.get("motion_made_by", "") or "").strip()
    context["motion_text"] = str(base_context.get("motion_text", "") or "").strip()
    motion_rows = build_wrapped_rows(context["motion_text"], wrap_width=MOTION_TEXT_WRAP_WIDTH)
    context["motion_text_rows"] = motion_rows
    context["motion_text_line_count"] = len(motion_rows)
    return context
