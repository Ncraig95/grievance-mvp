from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import json
import os
import re
import socket
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from docx import Document
from PIL import Image

from ..db.db import Db, utcnow
from .graph_mail import MailAttachment
from .signature_workflow import resolve_docuseal_template_id


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_ANCHOR_PERIOD_START = date(2025, 9, 7)
_CURRENCY = Decimal("0.01")
_MILES = Decimal("0.01")
_METERS_PER_MILE = Decimal("1609.344")
_COMMISSION_HOUR_DIVISOR = Decimal("160")
_PAY_FORM_KEY = "pay_portal_packet"
_IRS_RATE_QUANT = Decimal("0.001")
_DEFAULT_IRS_RATE_SOURCE_URLS = (
    "https://www.irs.gov/tax-professionals/standard-mileage-rates",
    "https://www.irs.gov/newsroom/irs-sets-2026-business-standard-mileage-rate-at-725-cents-per-mile-up-25-cents",
)
_RECEIPT_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


@dataclass(frozen=True)
class PayActor:
    email: str
    display_name: str | None
    role: str
    can_view_all: bool
    can_edit_all: bool
    can_lock: bool
    is_guest: bool = False


@dataclass(frozen=True)
class DifferentialResult:
    wage_scale_id: int | None
    diff_rate: Decimal
    diff_amount: Decimal
    lost_wage_hourly_rate: Decimal


@dataclass(frozen=True)
class CommissionCompensationResult:
    base_wage_input_type: str
    base_wage_amount: Decimal
    base_hourly_rate: Decimal
    commission_month_1_amount: Decimal
    commission_month_2_amount: Decimal
    commission_month_3_amount: Decimal
    commission_average_monthly: Decimal
    commission_hourly_rate: Decimal
    calculated_hourly_rate: Decimal


@dataclass(frozen=True)
class IrsMileageRateCandidate:
    rate_year: str
    effective_date: str
    cents_per_mile: Decimal
    rate_per_mile: Decimal
    source_url: str
    source_title: str | None = None


def normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def safe_filename(value: object, *, fallback: str = "file") -> str:
    cleaned = _SAFE_FILENAME_RE.sub("", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def _money(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or "0").replace("$", "").replace(",", "").strip())
    except Exception:
        return Decimal("0")
    return parsed.quantize(_CURRENCY, rounding=ROUND_HALF_UP)


def _quantity(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or "0").replace(",", "").strip())
    except Exception:
        return Decimal("0")
    return parsed.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def normalize_wage_input(
    *,
    input_type: object,
    amount: object,
    weekly_basis_hours: object,
) -> tuple[str, Decimal, Decimal]:
    normalized_type = str(input_type or "hourly").strip().lower()
    if normalized_type not in {"hourly", "weekly"}:
        normalized_type = "hourly"
    wage_amount = _money(amount)
    basis = _quantity(weekly_basis_hours)
    if basis <= 0:
        basis = Decimal("40")
    if wage_amount <= 0:
        return normalized_type, Decimal("0.00"), Decimal("0.00")
    if normalized_type == "weekly":
        hourly = (wage_amount / basis).quantize(_CURRENCY, rounding=ROUND_HALF_UP)
        return normalized_type, wage_amount, hourly
    return normalized_type, wage_amount, wage_amount.quantize(_CURRENCY, rounding=ROUND_HALF_UP)


def calculate_commission_compensation(
    *,
    base_wage_input_type: object,
    base_wage_amount: object,
    weekly_basis_hours: object,
    commission_month_1_amount: object,
    commission_month_2_amount: object,
    commission_month_3_amount: object,
) -> CommissionCompensationResult:
    wage_type, wage_amount, base_hourly = normalize_wage_input(
        input_type=base_wage_input_type,
        amount=base_wage_amount,
        weekly_basis_hours=weekly_basis_hours,
    )
    month_1 = max(_money(commission_month_1_amount), Decimal("0.00"))
    month_2 = max(_money(commission_month_2_amount), Decimal("0.00"))
    month_3 = max(_money(commission_month_3_amount), Decimal("0.00"))
    average_monthly = ((month_1 + month_2 + month_3) / Decimal("3")).quantize(
        _CURRENCY,
        rounding=ROUND_HALF_UP,
    )
    commission_hourly = (average_monthly / _COMMISSION_HOUR_DIVISOR).quantize(
        _CURRENCY,
        rounding=ROUND_HALF_UP,
    )
    return CommissionCompensationResult(
        base_wage_input_type=wage_type,
        base_wage_amount=wage_amount,
        base_hourly_rate=base_hourly,
        commission_month_1_amount=month_1,
        commission_month_2_amount=month_2,
        commission_month_3_amount=month_3,
        commission_average_monthly=average_monthly,
        commission_hourly_rate=commission_hourly,
        calculated_hourly_rate=(base_hourly + commission_hourly).quantize(
            _CURRENCY,
            rounding=ROUND_HALF_UP,
        ),
    )


def _currency_text(value: object) -> str:
    amount = _money(value)
    return "" if amount == 0 else f"{amount:.2f}"


def _decimal_text(value: object, *, places: int = 2) -> str:
    try:
        amount = Decimal(str(value or "0"))
    except Exception:
        amount = Decimal("0")
    quant = Decimal(1).scaleb(-places)
    return f"{amount.quantize(quant, rounding=ROUND_HALF_UP):f}"


def _mileage_rate_text(value: object) -> str:
    try:
        amount = Decimal(str(value or "0"))
    except Exception:
        amount = Decimal("0")
    text = f"{amount.quantize(_IRS_RATE_QUANT, rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".")
    if "." not in text:
        return f"{text}.00"
    decimals = len(text.rsplit(".", 1)[1])
    if decimals == 1:
        return f"{text}0"
    return text


def _plain_text_from_html(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_from_html(value: str) -> str | None:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", str(value or ""))
    if not match:
        return None
    title = _plain_text_from_html(match.group(1))
    return title or None


def parse_irs_mileage_rate_candidates(*, content: str, source_url: str) -> list[IrsMileageRateCandidate]:
    title = _title_from_html(content)
    text = _plain_text_from_html(content)
    patterns = (
        r"IRS\s+sets\s+(20\d{2})\s+business\s+standard\s+mileage\s+rate\s+at\s+(\d+(?:\.\d+)?)\s+cents",
        r"Beginning\s+Jan(?:uary)?\.?\s+1,\s*(20\d{2}).{0,800}?(\d+(?:\.\d+)?)\s+cents?\s+per\s+mile\s+driven\s+for\s+business\s+use",
        r"For\s+(20\d{2}).{0,240}?standard\s+mileage\s+rate\s+is\s+(\d+(?:\.\d+)?)\s+cents?\s+per\s+mile",
        r"(20\d{2})\s+mileage\s+rates?.{0,500}?Self-employed\s+and\s+business:\s+(\d+(?:\.\d+)?)\s+cents/?mile",
    )
    candidates: list[IrsMileageRateCandidate] = []
    seen: set[tuple[str, str]] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            year = str(match.group(1))
            cents = Decimal(str(match.group(2))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            key = (year, f"{cents:f}")
            if key in seen:
                continue
            seen.add(key)
            rate = (cents / Decimal("100")).quantize(_IRS_RATE_QUANT, rounding=ROUND_HALF_UP)
            candidates.append(
                IrsMileageRateCandidate(
                    rate_year=year,
                    effective_date=f"{year}-01-01",
                    cents_per_mile=cents,
                    rate_per_mile=rate,
                    source_url=source_url,
                    source_title=title,
                )
            )
    return candidates


def period_bounds_for(day: date) -> tuple[date, date]:
    delta_days = (day - _ANCHOR_PERIOD_START).days
    period_index = delta_days // 14
    start = _ANCHOR_PERIOD_START + timedelta(days=period_index * 14)
    return start, start + timedelta(days=13)


def current_period_bounds() -> tuple[date, date]:
    return period_bounds_for(date.today())


def period_id_for(start: date, end: date, revision: int = 1) -> str:
    return f"pay-{start.isoformat()}-{end.isoformat()}-r{max(1, int(revision))}"


def pay_period_folder_path(*, root_folder: str, period_start: str, period_end: str) -> str:
    year = str(period_start)[:4]
    label = f"{period_start}_to_{period_end}"
    return "/".join(part.strip("/") for part in (root_folder, year, label) if part.strip("/"))


async def add_pay_event(
    db: Db,
    *,
    period_id: str | None,
    event_type: str,
    actor: str | None = None,
    entry_id: str | None = None,
    packet_id: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    await db.exec(
        """INSERT INTO pay_events(period_id, entry_id, packet_id, ts_utc, event_type, actor, details_json)
           VALUES(?,?,?,?,?,?,?)""",
        (
            period_id,
            entry_id,
            packet_id,
            utcnow(),
            event_type,
            actor,
            json.dumps(details or {}, ensure_ascii=False),
        ),
    )


async def ensure_pay_period(db: Db, *, for_date: date | None = None) -> dict[str, object]:
    start, end = period_bounds_for(for_date or date.today())
    row = await db.fetchone(
        """SELECT id, period_start, period_end, status, revision, president_email,
                  sharepoint_folder_path, sharepoint_folder_web_url
           FROM pay_periods
           WHERE period_start=? AND period_end=?
           ORDER BY revision DESC
           LIMIT 1""",
        (start.isoformat(), end.isoformat()),
    )
    if row:
        return {
            "id": row[0],
            "period_start": row[1],
            "period_end": row[2],
            "status": row[3],
            "revision": int(row[4] or 1),
            "president_email": row[5],
            "sharepoint_folder_path": row[6],
            "sharepoint_folder_web_url": row[7],
        }

    now = utcnow()
    period_id = period_id_for(start, end)
    await db.exec(
        """INSERT INTO pay_periods(
             id, period_start, period_end, status, revision, created_at_utc, updated_at_utc
           ) VALUES(?,?,?,?,?,?,?)""",
        (period_id, start.isoformat(), end.isoformat(), "open", 1, now, now),
    )
    return {
        "id": period_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "status": "open",
        "revision": 1,
        "president_email": None,
        "sharepoint_folder_path": None,
        "sharepoint_folder_web_url": None,
    }


async def get_pay_period(db: Db, period_id: str) -> dict[str, object] | None:
    row = await db.fetchone(
        """SELECT id, period_start, period_end, status, revision, locked_by, locked_at_utc,
                  completed_at_utc, president_email, sharepoint_folder_path, sharepoint_folder_web_url
           FROM pay_periods
           WHERE id=?""",
        (period_id,),
    )
    if not row:
        return None
    return {
        "id": row[0],
        "period_start": row[1],
        "period_end": row[2],
        "status": row[3],
        "revision": int(row[4] or 1),
        "locked_by": row[5],
        "locked_at_utc": row[6],
        "completed_at_utc": row[7],
        "president_email": row[8],
        "sharepoint_folder_path": row[9],
        "sharepoint_folder_web_url": row[10],
    }


async def pay_settings(db: Db, *, pay_cfg: Any | None = None) -> dict[str, object]:
    defaults: dict[str, object] = {
        "treasurer_emails": list(getattr(pay_cfg, "treasurer_emails", ()) or ()),
        "president_email": str(getattr(pay_cfg, "president_email", "") or ""),
        "irs_rates": dict(getattr(pay_cfg, "irs_rates", {}) or {}),
        "common_places": list(getattr(pay_cfg, "common_places", ()) or ()),
    }
    row = await db.app_setting("pay_portal")
    if not row:
        return defaults
    try:
        parsed = json.loads(row[0])
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        return defaults
    merged = dict(defaults)
    for key, value in parsed.items():
        if key in {"irs_rates", "common_places"} and not value:
            continue
        merged[key] = value
    return merged


async def save_pay_settings(
    db: Db,
    *,
    setting: dict[str, object],
    updated_by: str | None,
    pay_cfg: Any | None = None,
) -> dict[str, object]:
    normalized = dict(await pay_settings(db, pay_cfg=pay_cfg))
    normalized.update(setting)
    await db.upsert_app_setting(setting_key="pay_portal", setting=normalized, updated_by=updated_by)
    return normalized


def _active_irs_rate_matches(settings: dict[str, object], *, year: str, rate_per_mile: Decimal) -> bool:
    rates = settings.get("irs_rates")
    if not isinstance(rates, dict):
        return False
    active = rates.get(str(year))
    if active is None:
        return False
    try:
        active_rate = Decimal(str(active))
    except Exception:
        return False
    return active_rate.quantize(_IRS_RATE_QUANT, rounding=ROUND_HALF_UP) == rate_per_mile


def _irs_rate_candidate_from_row(row: Any) -> dict[str, object]:
    return {
        "id": int(row[0]),
        "rate_year": row[1],
        "effective_date": row[2],
        "cents_per_mile": row[3],
        "rate_per_mile": _mileage_rate_text(row[4]),
        "source_url": row[5],
        "source_title": row[6],
        "detected_at_utc": row[7],
        "status": row[8],
        "approved_by": row[9],
        "approved_at_utc": row[10],
        "updated_at_utc": row[11],
    }


async def list_irs_rate_candidates(db: Db, *, status: str | None = None) -> list[dict[str, object]]:
    select_sql = """SELECT id, rate_year, effective_date, cents_per_mile, rate_per_mile,
                           source_url, source_title, detected_at_utc, status,
                           approved_by, approved_at_utc, updated_at_utc
                    FROM pay_irs_rate_candidates"""
    if status:
        rows = await db.fetchall(
            f"{select_sql} WHERE status=? ORDER BY effective_date DESC, id DESC",
            (status,),
        )
    else:
        rows = await db.fetchall(
            f"""{select_sql}
                ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                         effective_date DESC, id DESC"""
        )
    return [_irs_rate_candidate_from_row(row) for row in rows]


async def sync_irs_mileage_rate_candidates(
    db: Db,
    *,
    pay_cfg: Any | None = None,
    http_get: Any | None = None,
    source_urls: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    if pay_cfg is not None and not bool(getattr(pay_cfg, "irs_rate_sync_enabled", True)):
        return {"detected": [], "skipped_existing": 0, "skipped_duplicates": 0, "failures": []}

    configured_urls = tuple(source_urls or getattr(pay_cfg, "irs_rate_source_urls", ()) or _DEFAULT_IRS_RATE_SOURCE_URLS)
    get = http_get or requests.get
    settings = await pay_settings(db, pay_cfg=pay_cfg)
    detected: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    skipped_existing = 0
    skipped_duplicates = 0
    now = utcnow()

    for source_url in configured_urls:
        url = str(source_url or "").strip()
        if not url:
            continue
        try:
            def _fetch():  # noqa: ANN202
                try:
                    return get(url, timeout=15)
                except TypeError:
                    return get(url)

            response = await asyncio.to_thread(_fetch)
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code >= 400:
                raise RuntimeError(f"IRS source returned HTTP {status_code}")
            content = str(getattr(response, "text", "") or "")
            candidates = parse_irs_mileage_rate_candidates(content=content, source_url=url)
            if not candidates:
                raise RuntimeError("no IRS business mileage rate found")
        except Exception as exc:
            failures.append({"source_url": url, "error": str(exc)})
            continue

        for candidate in candidates:
            if _active_irs_rate_matches(settings, year=candidate.rate_year, rate_per_mile=candidate.rate_per_mile):
                skipped_existing += 1
                continue
            existing = await db.fetchone(
                """SELECT id
                   FROM pay_irs_rate_candidates
                   WHERE rate_year=? AND effective_date=? AND cents_per_mile=? AND source_url=?""",
                (
                    candidate.rate_year,
                    candidate.effective_date,
                    float(candidate.cents_per_mile),
                    candidate.source_url,
                ),
            )
            if existing:
                skipped_duplicates += 1
                continue
            candidate_id = await db.insert(
                """INSERT INTO pay_irs_rate_candidates(
                     rate_year, effective_date, cents_per_mile, rate_per_mile,
                     source_url, source_title, detected_at_utc, status, updated_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    candidate.rate_year,
                    candidate.effective_date,
                    float(candidate.cents_per_mile),
                    float(candidate.rate_per_mile),
                    candidate.source_url,
                    candidate.source_title,
                    now,
                    "pending",
                    now,
                ),
            )
            row = await db.fetchone(
                """SELECT id, rate_year, effective_date, cents_per_mile, rate_per_mile,
                          source_url, source_title, detected_at_utc, status,
                          approved_by, approved_at_utc, updated_at_utc
                   FROM pay_irs_rate_candidates
                   WHERE id=?""",
                (candidate_id,),
            )
            if row:
                detected.append(_irs_rate_candidate_from_row(row))

    if failures:
        await add_pay_event(
            db,
            period_id=None,
            event_type="irs_rate_sync_failed",
            details={"failures": failures},
        )
    if detected:
        await add_pay_event(
            db,
            period_id=None,
            event_type="irs_rate_candidates_detected",
            details={"candidate_ids": [row["id"] for row in detected]},
        )
    return {
        "detected": detected,
        "skipped_existing": skipped_existing,
        "skipped_duplicates": skipped_duplicates,
        "failures": failures,
    }


async def approve_irs_rate_candidate(
    db: Db,
    *,
    candidate_id: int,
    actor: str,
    pay_cfg: Any | None = None,
) -> dict[str, object]:
    row = await db.fetchone(
        """SELECT id, rate_year, effective_date, cents_per_mile, rate_per_mile,
                  source_url, source_title, detected_at_utc, status,
                  approved_by, approved_at_utc, updated_at_utc
           FROM pay_irs_rate_candidates
           WHERE id=?""",
        (int(candidate_id),),
    )
    if not row:
        raise ValueError("IRS rate candidate not found")
    current = _irs_rate_candidate_from_row(row)
    if current["status"] not in {"pending", "approved"}:
        raise ValueError("IRS rate candidate is not pending")

    rate_year = str(current["rate_year"])
    rate_text = _mileage_rate_text(current["rate_per_mile"])
    settings = await pay_settings(db, pay_cfg=pay_cfg)
    rates = settings.get("irs_rates")
    active_rates = dict(rates) if isinstance(rates, dict) else {}
    active_rates[rate_year] = rate_text
    await save_pay_settings(
        db,
        setting={"irs_rates": active_rates},
        updated_by=actor,
        pay_cfg=pay_cfg,
    )
    now = utcnow()
    await db.exec(
        """UPDATE pay_irs_rate_candidates
           SET status='approved', approved_by=?, approved_at_utc=?, updated_at_utc=?
           WHERE id=?""",
        (actor, now, now, int(candidate_id)),
    )
    await add_pay_event(
        db,
        period_id=None,
        event_type="irs_rate_candidate_approved",
        actor=actor,
        details={"candidate_id": int(candidate_id), "rate_year": rate_year, "rate_per_mile": rate_text},
    )
    approved = await db.fetchone(
        """SELECT id, rate_year, effective_date, cents_per_mile, rate_per_mile,
                  source_url, source_title, detected_at_utc, status,
                  approved_by, approved_at_utc, updated_at_utc
           FROM pay_irs_rate_candidates
           WHERE id=?""",
        (int(candidate_id),),
    )
    result = _irs_rate_candidate_from_row(approved)
    result["active_rate"] = rate_text
    return result


async def treasurer_recipients(db: Db, *, fallback: tuple[str, ...], pay_cfg: Any | None = None) -> list[str]:
    settings = await pay_settings(db, pay_cfg=pay_cfg)
    configured = settings.get("treasurer_emails")
    recipients: list[str] = []
    if isinstance(configured, list):
        recipients.extend(str(v).strip() for v in configured if str(v).strip())
    elif isinstance(configured, str):
        recipients.extend(v.strip() for v in configured.split(",") if v.strip())
    rows = await db.fetchall("SELECT email FROM pay_users WHERE status='active' AND role='treasurer'")
    recipients.extend(str(row[0] or "").strip() for row in rows if str(row[0] or "").strip())
    if not recipients:
        recipients.extend(fallback)
    out: list[str] = []
    seen: set[str] = set()
    for recipient in recipients:
        lowered = recipient.lower()
        if lowered and lowered not in seen:
            seen.add(lowered)
            out.append(recipient)
    return out


async def president_email(db: Db, *, explicit: str | None = None, pay_cfg: Any | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    settings = await pay_settings(db, pay_cfg=pay_cfg)
    return str(settings.get("president_email") or "").strip()


async def upsert_pay_user(
    db: Db,
    *,
    email: str,
    display_name: str | None,
    role: str,
    status: str,
    actor: str,
) -> dict[str, object]:
    normalized_email = normalize_email(email)
    if not normalized_email:
        raise ValueError("email is required")
    normalized_role = str(role or "guest").strip().lower()
    if normalized_role not in {"guest", "treasurer"}:
        normalized_role = "guest"
    normalized_status = str(status or "active").strip().lower()
    if normalized_status not in {"active", "disabled"}:
        normalized_status = "active"
    now = utcnow()
    await db.exec(
        """
        INSERT INTO pay_users(email, display_name, role, status, created_at_utc, updated_at_utc, invited_by)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
          display_name=excluded.display_name,
          role=excluded.role,
          status=excluded.status,
          updated_at_utc=excluded.updated_at_utc
        """,
        (normalized_email, display_name, normalized_role, normalized_status, now, now, actor),
    )
    return {
        "email": normalized_email,
        "display_name": display_name,
        "role": normalized_role,
        "status": normalized_status,
    }


async def pay_user_by_email(db: Db, email: str) -> dict[str, object] | None:
    row = await db.fetchone(
        "SELECT email, display_name, role, status FROM pay_users WHERE email=?",
        (normalize_email(email),),
    )
    if not row:
        return None
    return {"email": row[0], "display_name": row[1], "role": row[2], "status": row[3]}


async def list_pay_users(db: Db) -> list[dict[str, object]]:
    rows = await db.fetchall(
        "SELECT email, display_name, role, status, updated_at_utc FROM pay_users ORDER BY email"
    )
    return [
        {
            "email": row[0],
            "display_name": row[1],
            "role": row[2],
            "status": row[3],
            "updated_at_utc": row[4],
        }
        for row in rows
    ]


async def list_wage_scales(db: Db) -> list[dict[str, object]]:
    rows = await db.fetchall(
        """SELECT id, effective_date, weekly_basis_hours, target_scale, actual_scale,
                  target_weekly_amount, actual_weekly_amount, target_multiplier, notes,
                  updated_at_utc, updated_by
           FROM pay_wage_scales
           ORDER BY effective_date DESC, weekly_basis_hours"""
    )
    return [
        {
            "id": int(row[0]),
            "effective_date": row[1],
            "weekly_basis_hours": float(row[2]),
            "target_scale": row[3],
            "actual_scale": row[4],
            "target_weekly_amount": row[5],
            "actual_weekly_amount": row[6],
            "target_multiplier": row[7],
            "notes": row[8],
            "updated_at_utc": row[9],
            "updated_by": row[10],
        }
        for row in rows
    ]


def _compensation_stub_from_row(row: Any) -> dict[str, object]:
    return {
        "id": row[0],
        "user_email": row[1],
        "uploaded_by": row[2],
        "base_wage_input_type": row[3],
        "base_wage_amount": row[4],
        "weekly_basis_hours": row[5],
        "commission_month_1_amount": row[6],
        "commission_month_2_amount": row[7],
        "commission_month_3_amount": row[8],
        "commission_average_monthly": row[9],
        "commission_hourly_rate": row[10],
        "calculated_hourly_rate": row[11],
        "filename": row[12],
        "content_type": row[13],
        "size_bytes": row[14],
        "sha256": row[15],
        "scan_status": row[16],
        "sharepoint_url": row[17],
        "notes": row[18],
        "created_at_utc": row[19],
    }


async def list_compensation_stubs(db: Db, *, actor: PayActor) -> list[dict[str, object]]:
    select_sql = """SELECT id, user_email, uploaded_by, base_wage_input_type, base_wage_amount,
                           weekly_basis_hours, commission_month_1_amount, commission_month_2_amount,
                           commission_month_3_amount, commission_average_monthly,
                           commission_hourly_rate, calculated_hourly_rate, original_filename,
                           content_type, size_bytes, sha256, scan_status, sharepoint_url,
                           notes, created_at_utc
                    FROM pay_compensation_stubs"""
    if actor.can_view_all:
        rows = await db.fetchall(f"{select_sql} ORDER BY user_email, created_at_utc DESC")
    else:
        rows = await db.fetchall(
            f"{select_sql} WHERE user_email=? ORDER BY created_at_utc DESC",
            (actor.email,),
        )
    return [_compensation_stub_from_row(row) for row in rows]


async def latest_compensation_stub(db: Db, *, user_email: str) -> dict[str, object] | None:
    row = await db.fetchone(
        """SELECT id, user_email, uploaded_by, base_wage_input_type, base_wage_amount,
                  weekly_basis_hours, commission_month_1_amount, commission_month_2_amount,
                  commission_month_3_amount, commission_average_monthly,
                  commission_hourly_rate, calculated_hourly_rate, original_filename,
                  content_type, size_bytes, sha256, scan_status, sharepoint_url,
                  notes, created_at_utc
           FROM pay_compensation_stubs
           WHERE user_email=?
           ORDER BY created_at_utc DESC, id DESC
           LIMIT 1""",
        (normalize_email(user_email),),
    )
    return _compensation_stub_from_row(row) if row else None


async def upsert_wage_scale(
    db: Db,
    *,
    effective_date: str,
    weekly_basis_hours: float,
    target_weekly_amount: float,
    actual_weekly_amount: float | None,
    target_multiplier: float,
    target_scale: str,
    actual_scale: str,
    notes: str | None,
    updated_by: str,
) -> dict[str, object]:
    now = utcnow()
    await db.exec(
        """
        INSERT INTO pay_wage_scales(
          effective_date, weekly_basis_hours, target_scale, actual_scale,
          target_weekly_amount, actual_weekly_amount, target_multiplier,
          notes, created_at_utc, updated_at_utc, updated_by
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(effective_date, weekly_basis_hours, target_scale, actual_scale) DO UPDATE SET
          target_weekly_amount=excluded.target_weekly_amount,
          actual_weekly_amount=excluded.actual_weekly_amount,
          target_multiplier=excluded.target_multiplier,
          notes=excluded.notes,
          updated_at_utc=excluded.updated_at_utc,
          updated_by=excluded.updated_by
        """,
        (
            effective_date,
            float(weekly_basis_hours),
            str(target_scale or "36"),
            str(actual_scale or "base"),
            float(target_weekly_amount),
            None if actual_weekly_amount is None else float(actual_weekly_amount),
            float(target_multiplier),
            notes,
            now,
            now,
            updated_by,
        ),
    )
    row = await db.fetchone(
        """SELECT id FROM pay_wage_scales
           WHERE effective_date=? AND weekly_basis_hours=? AND target_scale=? AND actual_scale=?""",
        (effective_date, float(weekly_basis_hours), str(target_scale or "36"), str(actual_scale or "base")),
    )
    return {"id": int(row[0]) if row else None}


async def calculate_president_differential(
    db: Db,
    *,
    entry_date: str,
    weekly_basis_hours: float,
    president_diff_hours: object,
    target_scale: str,
    target_multiplier: float,
    lost_wage_input_type: object,
    lost_wage_amount: object,
) -> DifferentialResult:
    hours = _quantity(president_diff_hours)
    _, _, lost_wage_hourly = normalize_wage_input(
        input_type=lost_wage_input_type,
        amount=lost_wage_amount,
        weekly_basis_hours=weekly_basis_hours,
    )
    if hours <= 0:
        return DifferentialResult(
            wage_scale_id=None,
            diff_rate=Decimal("0.00"),
            diff_amount=Decimal("0.00"),
            lost_wage_hourly_rate=lost_wage_hourly,
        )
    if lost_wage_hourly <= 0:
        return DifferentialResult(
            wage_scale_id=None,
            diff_rate=Decimal("0.00"),
            diff_amount=Decimal("0.00"),
            lost_wage_hourly_rate=lost_wage_hourly,
        )

    row = await db.fetchone(
        """SELECT id, weekly_basis_hours, target_weekly_amount, target_multiplier
           FROM pay_wage_scales
           WHERE effective_date <= ?
             AND ABS(weekly_basis_hours - ?) < 0.0001
             AND target_scale=?
           ORDER BY effective_date DESC, id DESC
           LIMIT 1""",
        (entry_date, float(weekly_basis_hours), str(target_scale)),
    )
    if not row:
        return DifferentialResult(
            wage_scale_id=None,
            diff_rate=Decimal("0.00"),
            diff_amount=Decimal("0.00"),
            lost_wage_hourly_rate=lost_wage_hourly,
        )

    basis = Decimal(str(row[1]))
    if basis <= 0:
        return DifferentialResult(
            wage_scale_id=int(row[0]),
            diff_rate=Decimal("0.00"),
            diff_amount=Decimal("0.00"),
            lost_wage_hourly_rate=lost_wage_hourly,
        )
    # Wage scale rows store the scale 36 base; the presidential target is scale 36 plus 20%.
    multiplier = Decimal(str(row[3] if row[3] is not None else target_multiplier))
    target_hourly = (Decimal(str(row[2])) / basis) * multiplier
    diff_rate = max(target_hourly - lost_wage_hourly, Decimal("0")).quantize(_CURRENCY, rounding=ROUND_HALF_UP)
    diff_amount = (diff_rate * hours).quantize(_CURRENCY, rounding=ROUND_HALF_UP)
    return DifferentialResult(
        wage_scale_id=int(row[0]),
        diff_rate=diff_rate,
        diff_amount=diff_amount,
        lost_wage_hourly_rate=lost_wage_hourly,
    )


async def list_entries(db: Db, *, period_id: str, actor: PayActor) -> list[dict[str, object]]:
    if actor.can_view_all:
        rows = await db.fetchall(
            """SELECT id, user_email, display_name, entry_date, local_number, address, hourly_rate,
                      lost_wage_input_type, lost_wage_amount, lost_wage_hourly_rate,
                      compensation_stub_id, hours, mileage_miles, mileage_rate, mileage_amount,
                      rentals_amount, meals_amount, hotel_amount, miscellaneous_amount,
                      president_diff_hours, president_diff_rate, president_diff_amount,
                      wage_scale_id, notes, locked_at_utc, created_at_utc, updated_at_utc
               FROM pay_entries
               WHERE period_id=?
               ORDER BY entry_date, user_email""",
            (period_id,),
        )
    else:
        rows = await db.fetchall(
            """SELECT id, user_email, display_name, entry_date, local_number, address, hourly_rate,
                      lost_wage_input_type, lost_wage_amount, lost_wage_hourly_rate,
                      compensation_stub_id, hours, mileage_miles, mileage_rate, mileage_amount,
                      rentals_amount, meals_amount, hotel_amount, miscellaneous_amount,
                      president_diff_hours, president_diff_rate, president_diff_amount,
                      wage_scale_id, notes, locked_at_utc, created_at_utc, updated_at_utc
               FROM pay_entries
               WHERE period_id=? AND user_email=?
               ORDER BY entry_date, user_email""",
            (period_id, actor.email),
        )
    entries: list[dict[str, object]] = []
    for row in rows:
        entries.append(
            {
                "id": row[0],
                "user_email": row[1],
                "display_name": row[2],
                "entry_date": row[3],
                "local_number": row[4],
                "address": row[5],
                "hourly_rate": row[6],
                "lost_wage_input_type": row[7],
                "lost_wage_amount": row[8],
                "lost_wage_hourly_rate": row[9],
                "compensation_stub_id": row[10],
                "hours": row[11],
                "mileage_miles": row[12],
                "mileage_rate": row[13],
                "mileage_amount": row[14],
                "rentals_amount": row[15],
                "meals_amount": row[16],
                "hotel_amount": row[17],
                "miscellaneous_amount": row[18],
                "president_diff_hours": row[19],
                "president_diff_rate": row[20],
                "president_diff_amount": row[21],
                "wage_scale_id": row[22],
                "notes": row[23],
                "locked_at_utc": row[24],
                "created_at_utc": row[25],
                "updated_at_utc": row[26],
            }
        )
    return entries


async def upsert_entry(
    db: Db,
    *,
    period_id: str,
    actor: PayActor,
    data: dict[str, object],
    pay_cfg: Any,
) -> dict[str, object]:
    period = await get_pay_period(db, period_id)
    if not period:
        raise ValueError("pay period not found")
    if str(period["status"]) != "open":
        raise ValueError("pay period is locked")

    entry_date = str(data.get("entry_date") or "").strip()
    if not entry_date:
        raise ValueError("entry_date is required")
    period_start = date.fromisoformat(str(period["period_start"]))
    period_end = date.fromisoformat(str(period["period_end"]))
    parsed_entry_date = date.fromisoformat(entry_date)
    if parsed_entry_date < period_start or parsed_entry_date > period_end:
        raise ValueError("entry_date is outside the pay period")

    target_email = (normalize_email(data.get("user_email")) or actor.email) if actor.can_edit_all else actor.email
    if not target_email:
        raise ValueError("user_email is required")

    weekly_basis_hours = float(data.get("weekly_basis_hours") or 40.0)
    explicit_hourly_rate = _money(data.get("hourly_rate"))
    lost_wage_input_type = str(
        data.get("lost_wage_input_type") or data.get("employee_wage_input_type") or "hourly"
    ).strip().lower()
    lost_wage_amount_input = data.get("lost_wage_amount", data.get("employee_wage_amount"))
    compensation_stub_id: str | None = None
    if lost_wage_input_type in {"profile", "saved_profile", "commission_profile"}:
        stub = await latest_compensation_stub(db, user_email=target_email)
        if not stub:
            raise ValueError("no saved lost wage profile is available for this member")
        compensation_stub_id = str(stub["id"])
        lost_wage_input_type = "profile"
        lost_wage_amount_input = stub["calculated_hourly_rate"]
    elif _money(lost_wage_amount_input) <= 0 and explicit_hourly_rate > 0:
        lost_wage_input_type = "hourly"
        lost_wage_amount_input = explicit_hourly_rate
    diff_wage_type = "hourly" if lost_wage_input_type == "profile" else lost_wage_input_type
    normalized_wage_type, lost_wage_amount, lost_wage_hourly_rate = normalize_wage_input(
        input_type=lost_wage_input_type,
        amount=lost_wage_amount_input,
        weekly_basis_hours=weekly_basis_hours,
    )
    if lost_wage_input_type == "profile":
        normalized_wage_type = "profile"
    diff = await calculate_president_differential(
        db,
        entry_date=entry_date,
        weekly_basis_hours=weekly_basis_hours,
        president_diff_hours=data.get("president_diff_hours"),
        target_scale=pay_cfg.president_target_scale,
        target_multiplier=pay_cfg.president_target_multiplier,
        lost_wage_input_type=diff_wage_type,
        lost_wage_amount=lost_wage_amount,
    )
    hourly_rate = explicit_hourly_rate if explicit_hourly_rate > 0 else lost_wage_hourly_rate

    row = await db.fetchone(
        "SELECT id, locked_at_utc FROM pay_entries WHERE period_id=? AND user_email=? AND entry_date=?",
        (period_id, target_email, entry_date),
    )
    if row and row[1]:
        raise ValueError("entry is locked")
    entry_id = str(row[0]) if row else f"pay-entry-{uuid4().hex}"
    now = utcnow()
    values = {
        "display_name": str(data.get("display_name") or actor.display_name or target_email).strip(),
        "local_number": str(data.get("local_number") or "").strip(),
        "address": str(data.get("address") or "").strip(),
        "hourly_rate": float(hourly_rate),
        "lost_wage_input_type": normalized_wage_type,
        "lost_wage_amount": float(lost_wage_amount),
        "lost_wage_hourly_rate": float(diff.lost_wage_hourly_rate),
        "compensation_stub_id": compensation_stub_id,
        "hours": float(_quantity(data.get("hours"))),
        "mileage_miles": float(_quantity(data.get("mileage_miles"))),
        "mileage_rate": float(_money(data.get("mileage_rate"))),
        "mileage_amount": float(_money(data.get("mileage_amount"))),
        "rentals_amount": float(_money(data.get("rentals_amount"))),
        "meals_amount": float(_money(data.get("meals_amount"))),
        "hotel_amount": float(_money(data.get("hotel_amount"))),
        "miscellaneous_amount": float(_money(data.get("miscellaneous_amount"))),
        "president_diff_hours": float(_quantity(data.get("president_diff_hours"))),
        "president_diff_rate": float(diff.diff_rate),
        "president_diff_amount": float(diff.diff_amount),
        "wage_scale_id": diff.wage_scale_id,
        "notes": str(data.get("notes") or "").strip(),
    }
    await db.exec(
        """
        INSERT INTO pay_entries(
          id, period_id, user_email, display_name, entry_date, local_number, address,
          hourly_rate, lost_wage_input_type, lost_wage_amount, lost_wage_hourly_rate,
          compensation_stub_id, hours, mileage_miles, mileage_rate, mileage_amount,
          rentals_amount, meals_amount, hotel_amount, miscellaneous_amount,
          president_diff_hours, president_diff_rate, president_diff_amount, wage_scale_id,
          notes, created_at_utc, updated_at_utc
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(period_id, user_email, entry_date) DO UPDATE SET
          display_name=excluded.display_name,
          local_number=excluded.local_number,
          address=excluded.address,
          hourly_rate=excluded.hourly_rate,
          lost_wage_input_type=excluded.lost_wage_input_type,
          lost_wage_amount=excluded.lost_wage_amount,
          lost_wage_hourly_rate=excluded.lost_wage_hourly_rate,
          compensation_stub_id=excluded.compensation_stub_id,
          hours=excluded.hours,
          mileage_miles=excluded.mileage_miles,
          mileage_rate=excluded.mileage_rate,
          mileage_amount=excluded.mileage_amount,
          rentals_amount=excluded.rentals_amount,
          meals_amount=excluded.meals_amount,
          hotel_amount=excluded.hotel_amount,
          miscellaneous_amount=excluded.miscellaneous_amount,
          president_diff_hours=excluded.president_diff_hours,
          president_diff_rate=excluded.president_diff_rate,
          president_diff_amount=excluded.president_diff_amount,
          wage_scale_id=excluded.wage_scale_id,
          notes=excluded.notes,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            entry_id,
            period_id,
            target_email,
            values["display_name"],
            entry_date,
            values["local_number"],
            values["address"],
            values["hourly_rate"],
            values["lost_wage_input_type"],
            values["lost_wage_amount"],
            values["lost_wage_hourly_rate"],
            values["compensation_stub_id"],
            values["hours"],
            values["mileage_miles"],
            values["mileage_rate"],
            values["mileage_amount"],
            values["rentals_amount"],
            values["meals_amount"],
            values["hotel_amount"],
            values["miscellaneous_amount"],
            values["president_diff_hours"],
            values["president_diff_rate"],
            values["president_diff_amount"],
            values["wage_scale_id"],
            values["notes"],
            now,
            now,
        ),
    )
    await add_pay_event(
        db,
        period_id=period_id,
        entry_id=entry_id,
        event_type="entry_upserted",
        actor=actor.email,
        details={"entry_date": entry_date, "user_email": target_email},
    )
    entries = await list_entries(db, period_id=period_id, actor=PayActor(target_email, None, "guest", True, True, False))
    return next(row for row in entries if row["id"] == entry_id)


def decode_content_base64(value: str) -> bytes:
    raw = str(value or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw, validate=True)


def detect_content_type(filename: str, declared: str | None, content: bytes) -> str:
    declared_type = str(declared or "").split(";", 1)[0].strip().lower()
    if declared_type in _RECEIPT_CONTENT_TYPES:
        return declared_type
    lowered = filename.lower()
    if content.startswith(b"%PDF") or lowered.endswith(".pdf"):
        return "application/pdf"
    if content.startswith(b"\xff\xd8\xff") or lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n") or lowered.endswith(".png"):
        return "image/png"
    return declared_type or "application/octet-stream"


def validate_receipt_file(*, filename: str, content_type: str, content: bytes, max_file_bytes: int) -> None:
    if not content:
        raise ValueError("file is empty")
    if len(content) > max_file_bytes:
        raise ValueError("file exceeds maximum size")
    if content_type not in _RECEIPT_CONTENT_TYPES:
        raise ValueError("only PDF, JPEG, and PNG files are allowed")
    if content_type == "application/pdf" and not content.startswith(b"%PDF"):
        raise ValueError("PDF content is invalid")
    if content_type == "image/jpeg" and not content.startswith(b"\xff\xd8\xff"):
        raise ValueError("JPEG content is invalid")
    if content_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG content is invalid")


def scan_with_clamav(*, content: bytes, host: str, port: int, timeout_seconds: int) -> str:
    if not host:
        raise RuntimeError("ClamAV host is not configured")
    with socket.create_connection((host, int(port)), timeout=float(timeout_seconds)) as sock:
        sock.settimeout(float(timeout_seconds))
        sock.sendall(b"zINSTREAM\0")
        for start in range(0, len(content), 1024 * 1024):
            chunk = content[start : start + 1024 * 1024]
            sock.sendall(len(chunk).to_bytes(4, "big"))
            sock.sendall(chunk)
        sock.sendall((0).to_bytes(4, "big"))
        response = sock.recv(4096).decode("utf-8", errors="replace").strip()
    if "FOUND" in response:
        raise ValueError(f"virus scan failed: {response}")
    if "OK" not in response:
        raise RuntimeError(f"virus scan did not complete cleanly: {response}")
    return response


async def store_attachment(
    db: Db,
    *,
    cfg: Any,
    period_id: str,
    entry_id: str,
    actor: PayActor,
    attachment_type: str,
    filename: str,
    content_type: str | None,
    content: bytes,
    scan: bool = True,
) -> dict[str, object]:
    entry = await db.fetchone(
        "SELECT user_email, locked_at_utc FROM pay_entries WHERE id=? AND period_id=?",
        (entry_id, period_id),
    )
    if not entry:
        raise ValueError("entry not found")
    if entry[1]:
        raise ValueError("entry is locked")
    if not actor.can_edit_all and normalize_email(entry[0]) != actor.email:
        raise PermissionError("cannot attach files to another user's entry")

    safe_name = safe_filename(filename, fallback="receipt")
    detected_type = detect_content_type(safe_name, content_type, content)
    validate_receipt_file(
        filename=safe_name,
        content_type=detected_type,
        content=content,
        max_file_bytes=int(cfg.pay_portal.receipt_max_file_bytes),
    )

    scan_result = "scan-skipped"
    if scan:
        scan_result = scan_with_clamav(
            content=content,
            host=cfg.pay_portal.clamav_host,
            port=cfg.pay_portal.clamav_port,
            timeout_seconds=cfg.pay_portal.clamav_timeout_seconds,
        )

    attachment_id = f"pay-att-{uuid4().hex}"
    ext = _RECEIPT_CONTENT_TYPES[detected_type]
    stored_filename = f"{attachment_id}{ext}"
    period_dir = Path(cfg.data_root) / "pay" / period_id / "attachments"
    period_dir.mkdir(parents=True, exist_ok=True)
    local_path = period_dir / stored_filename
    local_path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    await db.exec(
        """INSERT INTO pay_attachments(
             id, period_id, entry_id, uploaded_by, attachment_type, original_filename,
             stored_filename, local_path, content_type, size_bytes, sha256, scan_status,
             scan_result, created_at_utc
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            attachment_id,
            period_id,
            entry_id,
            actor.email,
            attachment_type,
            safe_name,
            stored_filename,
            str(local_path),
            detected_type,
            len(content),
            sha,
            "clean",
            scan_result,
            utcnow(),
        ),
    )
    await add_pay_event(
        db,
        period_id=period_id,
        entry_id=entry_id,
        event_type="attachment_uploaded",
        actor=actor.email,
        details={"filename": safe_name, "attachment_type": attachment_type, "sha256": sha},
    )
    return {
        "id": attachment_id,
        "attachment_type": attachment_type,
        "filename": safe_name,
        "content_type": detected_type,
        "size_bytes": len(content),
        "sha256": sha,
        "scan_status": "clean",
    }


async def store_compensation_stub(
    db: Db,
    *,
    cfg: Any,
    actor: PayActor,
    user_email: str | None,
    base_wage_input_type: object,
    base_wage_amount: object,
    weekly_basis_hours: object,
    commission_month_1_amount: object,
    commission_month_2_amount: object,
    commission_month_3_amount: object,
    filename: str,
    content_type: str | None,
    content: bytes,
    notes: str | None = None,
    scan: bool = True,
) -> dict[str, object]:
    target_email = normalize_email(user_email) or actor.email
    if not target_email:
        raise ValueError("user_email is required")
    if not actor.can_edit_all and target_email != actor.email:
        raise PermissionError("cannot upload lost wage proof for another member")

    safe_name = safe_filename(filename, fallback="pay-stub")
    detected_type = detect_content_type(safe_name, content_type, content)
    validate_receipt_file(
        filename=safe_name,
        content_type=detected_type,
        content=content,
        max_file_bytes=int(cfg.pay_portal.receipt_max_file_bytes),
    )
    scan_result = "scan-skipped"
    if scan:
        scan_result = scan_with_clamav(
            content=content,
            host=cfg.pay_portal.clamav_host,
            port=cfg.pay_portal.clamav_port,
            timeout_seconds=cfg.pay_portal.clamav_timeout_seconds,
        )

    basis = _quantity(weekly_basis_hours)
    if basis <= 0:
        basis = Decimal("40")
    calc = calculate_commission_compensation(
        base_wage_input_type=base_wage_input_type,
        base_wage_amount=base_wage_amount,
        weekly_basis_hours=basis,
        commission_month_1_amount=commission_month_1_amount,
        commission_month_2_amount=commission_month_2_amount,
        commission_month_3_amount=commission_month_3_amount,
    )
    stub_id = f"pay-comp-{uuid4().hex}"
    ext = _RECEIPT_CONTENT_TYPES[detected_type]
    stored_filename = f"{stub_id}{ext}"
    stub_dir = Path(cfg.data_root) / "pay" / "compensation-stubs" / safe_filename(target_email, fallback="member")
    stub_dir.mkdir(parents=True, exist_ok=True)
    local_path = stub_dir / stored_filename
    local_path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    now = utcnow()
    await db.exec(
        """INSERT INTO pay_compensation_stubs(
             id, user_email, uploaded_by, base_wage_input_type, base_wage_amount,
             weekly_basis_hours, commission_month_1_amount, commission_month_2_amount,
             commission_month_3_amount, commission_average_monthly, commission_hourly_rate,
             calculated_hourly_rate, original_filename, stored_filename, local_path,
             content_type, size_bytes, sha256, scan_status, scan_result, notes, created_at_utc
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            stub_id,
            target_email,
            actor.email,
            calc.base_wage_input_type,
            float(calc.base_wage_amount),
            float(basis),
            float(calc.commission_month_1_amount),
            float(calc.commission_month_2_amount),
            float(calc.commission_month_3_amount),
            float(calc.commission_average_monthly),
            float(calc.commission_hourly_rate),
            float(calc.calculated_hourly_rate),
            safe_name,
            stored_filename,
            str(local_path),
            detected_type,
            len(content),
            sha,
            "clean",
            scan_result,
            str(notes or "").strip(),
            now,
        ),
    )
    await add_pay_event(
        db,
        period_id=None,
        event_type="compensation_stub_uploaded",
        actor=actor.email,
        details={
            "user_email": target_email,
            "filename": safe_name,
            "sha256": sha,
            "calculated_hourly_rate": float(calc.calculated_hourly_rate),
        },
    )
    saved = await latest_compensation_stub(db, user_email=target_email)
    return saved or {
        "id": stub_id,
        "user_email": target_email,
        "calculated_hourly_rate": float(calc.calculated_hourly_rate),
    }


async def list_attachments(db: Db, *, period_id: str, actor: PayActor) -> list[dict[str, object]]:
    if actor.can_view_all:
        rows = await db.fetchall(
            """SELECT a.id, a.entry_id, a.attachment_type, a.original_filename, a.content_type,
                      a.size_bytes, a.sha256, a.scan_status, a.sharepoint_url, a.created_at_utc,
                      e.user_email, e.entry_date
               FROM pay_attachments a
               JOIN pay_entries e ON e.id = a.entry_id
               WHERE a.period_id=?
               ORDER BY e.entry_date, a.created_at_utc""",
            (period_id,),
        )
    else:
        rows = await db.fetchall(
            """SELECT a.id, a.entry_id, a.attachment_type, a.original_filename, a.content_type,
                      a.size_bytes, a.sha256, a.scan_status, a.sharepoint_url, a.created_at_utc,
                      e.user_email, e.entry_date
               FROM pay_attachments a
               JOIN pay_entries e ON e.id = a.entry_id
               WHERE a.period_id=? AND e.user_email=?
               ORDER BY e.entry_date, a.created_at_utc""",
            (period_id, actor.email),
        )
    return [
        {
            "id": row[0],
            "entry_id": row[1],
            "attachment_type": row[2],
            "filename": row[3],
            "content_type": row[4],
            "size_bytes": row[5],
            "sha256": row[6],
            "scan_status": row[7],
            "sharepoint_url": row[8],
            "created_at_utc": row[9],
            "user_email": row[10],
            "entry_date": row[11],
        }
        for row in rows
    ]


def _cell_set_text(cell: Any, text: object) -> None:
    cell.text = str(text or "")


def _set_paragraph_if_prefix(doc: Document, prefix: str, value: str) -> None:
    for paragraph in doc.paragraphs:
        if paragraph.text.strip().startswith(prefix):
            paragraph.text = value
            return


def _entry_amounts(row: dict[str, object]) -> dict[str, Decimal]:
    hours_amount = _money(row.get("hours")) * _money(row.get("hourly_rate"))
    return {
        "hours": hours_amount.quantize(_CURRENCY, rounding=ROUND_HALF_UP),
        "mileage": _money(row.get("mileage_amount")),
        "rentals": _money(row.get("rentals_amount")),
        "meals": _money(row.get("meals_amount")),
        "hotel": _money(row.get("hotel_amount")),
        "misc": _money(row.get("miscellaneous_amount")),
        "president_diff": _money(row.get("president_diff_amount")),
    }


def fill_pay_voucher_docx(
    *,
    template_path: str,
    output_path: str,
    period_start: str,
    period_end: str,
    entries: list[dict[str, object]],
    include_signature_placeholders: bool = False,
    filer_signer_index: int = 1,
    paid_by_signer_index: int = 2,
) -> None:
    doc = Document(template_path)
    first = entries[0] if entries else {}
    local_number = str(first.get("local_number") or "3106").strip()
    display_name = str(first.get("display_name") or first.get("user_email") or "").strip()
    address = str(first.get("address") or "").strip()
    hourly_rate = _currency_text(first.get("lost_wage_hourly_rate") or first.get("hourly_rate"))

    _set_paragraph_if_prefix(doc, "Local #", f"Local # {local_number}    Date: {period_end}")
    _set_paragraph_if_prefix(doc, "Name", f"Name {display_name}")
    _set_paragraph_if_prefix(doc, "Address", f"Address {address}")
    _set_paragraph_if_prefix(doc, "Hourly Rate", f"Hourly Rate {hourly_rate}")

    period_start_date = date.fromisoformat(period_start)
    table_pairs = [(0, 0), (1, 7)]
    row_names = {
        "hours": 1,
        "mileage": 2,
        "rentals": 3,
        "meals": 4,
        "hotel": 5,
        "misc": 6,
        "president_diff": 7,
    }
    totals_by_category = {key: Decimal("0.00") for key in row_names}
    explanation_lines: list[str] = []

    for table_index, day_offset in table_pairs:
        if table_index >= len(doc.tables):
            continue
        table = doc.tables[table_index]
        for col in range(1, min(8, len(table.rows[0].cells))):
            current = period_start_date + timedelta(days=day_offset + col - 1)
            _cell_set_text(table.rows[0].cells[col], current.strftime("%a\n%m/%d"))
        day_totals = [Decimal("0.00") for _ in range(7)]
        for entry in entries:
            try:
                entry_day = date.fromisoformat(str(entry.get("entry_date")))
            except Exception:
                continue
            index = (entry_day - period_start_date).days - day_offset
            if index < 0 or index > 6:
                continue
            col = index + 1
            amounts = _entry_amounts(entry)
            for key, row_no in row_names.items():
                value = amounts[key]
                totals_by_category[key] += value
                day_totals[index] += value
                _cell_set_text(table.rows[row_no].cells[col], _currency_text(value))
            notes = str(entry.get("notes") or "").strip()
            if notes:
                explanation_lines.append(f"{entry_day.isoformat()}: {notes}")
        for key, row_no in row_names.items():
            row_total = Decimal("0.00")
            for col in range(1, 8):
                row_total += _money(table.rows[row_no].cells[col].text)
            _cell_set_text(table.rows[row_no].cells[8], _currency_text(row_total))
        for index, day_total in enumerate(day_totals):
            _cell_set_text(table.rows[8].cells[index + 1], _currency_text(day_total))
        _cell_set_text(table.rows[8].cells[8], _currency_text(sum(day_totals, Decimal("0.00"))))

    if len(doc.tables) >= 3 and explanation_lines:
        _cell_set_text(
            doc.tables[2].rows[0].cells[0],
            "Attach necessary receipts - Explain reason for expense: "
            + " | ".join(explanation_lines)[:900],
        )

    if len(doc.tables) >= 4:
        totals_table = doc.tables[3]
        if len(totals_table.rows) >= 2:
            row = totals_table.rows[1]
            values = [
                totals_by_category["hours"],
                totals_by_category["mileage"],
                totals_by_category["rentals"],
                totals_by_category["meals"],
                totals_by_category["hotel"],
                totals_by_category["misc"],
                sum(totals_by_category.values(), Decimal("0.00")),
            ]
            for idx, value in enumerate(values, start=1):
                if idx < len(row.cells):
                    _cell_set_text(row.cells[idx], f"$ {_currency_text(value)}")

    if include_signature_placeholders:
        filer_index = max(1, int(filer_signer_index or 1))
        paid_by_index = max(1, int(paid_by_signer_index or 2))
        for paragraph in doc.paragraphs:
            if paragraph.text.strip().startswith("Signature"):
                paragraph.text = (
                    f"Signature\t{{{{Sig_es_:signer{filer_index}:signer{filer_index}_signature}}}}    "
                    f"{{{{Dte_es_:signer{filer_index}:signer{filer_index}_date}}}}\tPaid by "
                    f"{{{{Sig_es_:signer{paid_by_index}:signer{paid_by_index}_signature}}}}    "
                    f"{{{{Dte_es_:signer{paid_by_index}:signer{paid_by_index}_date}}}}"
                )
                break

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)


def merge_pdfs(input_paths: list[str], output_path: str) -> None:
    paths = [str(path) for path in input_paths if path and Path(path).exists()]
    if not paths:
        raise RuntimeError("no PDF files available to merge")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore

        writer = PdfWriter()
        for path in paths:
            reader = PdfReader(path)
            for page in reader.pages:
                writer.add_page(page)
        with open(output_path, "wb") as out:
            writer.write(out)
        return
    except ImportError:
        pass
    result = subprocess.run(
        ["pdfunite", *paths, output_path],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdfunite failed: {result.stderr[:400]}")


def image_to_pdf(image_path: str, output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as img:
        converted = img.convert("RGB")
        converted.save(output_path, "PDF", resolution=100.0)


def _extract_first_pdf(zip_bytes: bytes) -> bytes | None:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = sorted(zf.namelist())
            for name in names:
                if name.lower().endswith(".pdf"):
                    data = zf.read(name)
                    if data:
                        return data
    except Exception:
        return None
    return None


async def _attachments_for_packet(db: Db, period_id: str) -> list[dict[str, object]]:
    rows = await db.fetchall(
        """SELECT a.id, a.entry_id, a.attachment_type, a.original_filename, a.local_path,
                  a.content_type, a.sha256, e.user_email, e.entry_date
           FROM pay_attachments a
           JOIN pay_entries e ON e.id = a.entry_id
           WHERE a.period_id=?
           ORDER BY e.user_email, e.entry_date, a.created_at_utc""",
        (period_id,),
    )
    return [
        {
            "id": row[0],
            "entry_id": row[1],
            "attachment_type": row[2],
            "original_filename": row[3],
            "local_path": row[4],
            "content_type": row[5],
            "sha256": row[6],
            "user_email": row[7],
            "entry_date": row[8],
        }
        for row in rows
    ]


async def _compensation_stubs_for_packet(db: Db, period_id: str) -> list[dict[str, object]]:
    rows = await db.fetchall(
        """SELECT DISTINCT s.id, s.user_email, s.original_filename, s.local_path,
                  s.content_type, s.sha256
           FROM pay_entries e
           JOIN pay_compensation_stubs s ON s.id = e.compensation_stub_id
           WHERE e.period_id=?
           ORDER BY s.user_email, s.created_at_utc""",
        (period_id,),
    )
    return [
        {
            "id": row[0],
            "user_email": row[1],
            "original_filename": row[2],
            "local_path": row[3],
            "content_type": row[4],
            "sha256": row[5],
        }
        for row in rows
    ]


async def _upload_if_configured(
    *,
    cfg: Any,
    graph: Any,
    folder_path: str,
    filename: str,
    local_path: str,
) -> tuple[str | None, str | None]:
    if not (cfg.graph.site_hostname and cfg.graph.site_path and cfg.graph.document_library):
        return None, None
    uploaded = graph.upload_local_file_to_folder_path(
        site_hostname=cfg.graph.site_hostname,
        site_path=cfg.graph.site_path,
        library=cfg.graph.document_library,
        folder_path=folder_path,
        filename=filename,
        local_path=local_path,
    )
    return uploaded.web_url, uploaded.path


def pay_packet_signer_order(*, grouped_entry_emails: list[str], president_signer_email: str) -> tuple[list[str], int]:
    signers: list[str] = []
    seen: set[str] = set()
    president = normalize_email(president_signer_email)
    if not president:
        raise ValueError("president signer email is required")
    for email in grouped_entry_emails:
        normalized = normalize_email(email)
        if normalized == president:
            continue
        if normalized and normalized not in seen:
            seen.add(normalized)
            signers.append(normalized)
    signers.append(president)
    president_index = len(signers)
    return signers, president_index


async def lock_period_and_send_packet(
    *,
    db: Db,
    cfg: Any,
    graph: Any,
    docuseal: Any,
    period_id: str,
    actor: PayActor,
    president_signer_email: str | None,
    docx_to_pdf_func: Any,
) -> dict[str, object]:
    if not actor.can_lock:
        raise PermissionError("treasurer access required")
    period = await get_pay_period(db, period_id)
    if not period:
        raise ValueError("pay period not found")
    if str(period["status"]) not in {"open", "locked"}:
        raise ValueError("pay period is already sent or completed")
    entries = await list_entries(
        db,
        period_id=period_id,
        actor=PayActor(actor.email, actor.display_name, actor.role, True, True, True),
    )
    if not entries:
        raise ValueError("cannot lock an empty pay period")
    president_signer = await president_email(db, explicit=president_signer_email, pay_cfg=cfg.pay_portal)
    if not president_signer:
        raise ValueError("president signer email is required")

    packet_id = f"pay-packet-{uuid4().hex}"
    packet_dir = Path(cfg.data_root) / "pay" / period_id / "packet" / packet_id
    voucher_dir = packet_dir / "vouchers"
    support_dir = packet_dir / "support"
    voucher_dir.mkdir(parents=True, exist_ok=True)
    support_dir.mkdir(parents=True, exist_ok=True)
    period_start = str(period["period_start"])
    period_end = str(period["period_end"])

    grouped: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        grouped.setdefault(str(entry["user_email"]), []).append(entry)
    ordered_groups = sorted(grouped.items())
    signer_order, president_signer_index = pay_packet_signer_order(
        grouped_entry_emails=[email for email, _ in ordered_groups],
        president_signer_email=president_signer,
    )

    voucher_paths: list[str] = []
    voucher_pdf_paths: list[str] = []
    anchor_pdf_paths: list[str] = []
    for index, (email, rows) in enumerate(ordered_groups, start=1):
        label = safe_filename(rows[0].get("display_name") or email, fallback=f"voucher-{index}")
        docx_path = voucher_dir / f"{index:02d}-{label}.docx"
        anchor_docx_path = voucher_dir / f"{index:02d}-{label}.anchor.docx"
        filer_signer_index = signer_order.index(normalize_email(email)) + 1
        fill_pay_voucher_docx(
            template_path=cfg.pay_portal.voucher_template_path,
            output_path=str(docx_path),
            period_start=period_start,
            period_end=period_end,
            entries=rows,
            include_signature_placeholders=False,
        )
        fill_pay_voucher_docx(
            template_path=cfg.pay_portal.voucher_template_path,
            output_path=str(anchor_docx_path),
            period_start=period_start,
            period_end=period_end,
            entries=rows,
            include_signature_placeholders=True,
            filer_signer_index=filer_signer_index,
            paid_by_signer_index=president_signer_index,
        )
        voucher_pdf = docx_to_pdf_func(
            str(docx_path),
            str(voucher_dir),
            cfg.libreoffice_timeout_seconds,
            engine=cfg.docx_pdf_engine,
            graph_uploader=graph,
            graph_site_hostname=cfg.graph.site_hostname,
            graph_site_path=cfg.graph.site_path,
            graph_library=cfg.graph.document_library,
            graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
        )
        anchor_pdf = docx_to_pdf_func(
            str(anchor_docx_path),
            str(voucher_dir),
            cfg.libreoffice_timeout_seconds,
            engine=cfg.docx_pdf_engine,
            graph_uploader=graph,
            graph_site_hostname=cfg.graph.site_hostname,
            graph_site_path=cfg.graph.site_path,
            graph_library=cfg.graph.document_library,
            graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
        )
        voucher_paths.append(str(docx_path))
        voucher_pdf_paths.append(voucher_pdf)
        anchor_pdf_paths.append(anchor_pdf)

    support_pdf_paths: list[str] = []
    for attachment in await _attachments_for_packet(db, period_id):
        source = Path(str(attachment["local_path"]))
        if not source.exists():
            continue
        if str(attachment["content_type"]) == "application/pdf":
            support_pdf_paths.append(str(source))
            continue
        target = support_dir / f"{attachment['id']}.pdf"
        image_to_pdf(str(source), str(target))
        support_pdf_paths.append(str(target))

    unsigned_packet_path = str(packet_dir / f"{period_start}_to_{period_end}_packet.pdf")
    alignment_packet_path = str(packet_dir / f"{period_start}_to_{period_end}_alignment.pdf")
    merge_pdfs([*voucher_pdf_paths, *support_pdf_paths], unsigned_packet_path)
    merge_pdfs([*anchor_pdf_paths, *support_pdf_paths], alignment_packet_path)
    packet_bytes = Path(unsigned_packet_path).read_bytes()
    alignment_bytes = Path(alignment_packet_path).read_bytes()
    sha = hashlib.sha256(packet_bytes).hexdigest()

    folder_path = pay_period_folder_path(
        root_folder=cfg.pay_portal.sharepoint_root_folder,
        period_start=period_start,
        period_end=period_end,
    )
    sharepoint_unsigned_url, _ = await _upload_if_configured(
        cfg=cfg,
        graph=graph,
        folder_path=folder_path,
        filename=Path(unsigned_packet_path).name,
        local_path=unsigned_packet_path,
    )
    for path in voucher_paths:
        await _upload_if_configured(
            cfg=cfg,
            graph=graph,
            folder_path="/".join((folder_path, "Generated")),
            filename=Path(path).name,
            local_path=path,
        )
    for attachment in await _attachments_for_packet(db, period_id):
        web_url, sp_path = await _upload_if_configured(
            cfg=cfg,
            graph=graph,
            folder_path="/".join((folder_path, "Receipts and Mileage")),
            filename=safe_filename(attachment["original_filename"], fallback=str(attachment["id"])),
            local_path=str(attachment["local_path"]),
        )
        if web_url or sp_path:
            await db.exec(
                "UPDATE pay_attachments SET sharepoint_url=?, sharepoint_path=? WHERE id=?",
                (web_url, sp_path, attachment["id"]),
            )

    for stub in await _compensation_stubs_for_packet(db, period_id):
        web_url, sp_path = await _upload_if_configured(
            cfg=cfg,
            graph=graph,
            folder_path="/".join((folder_path, "Lost Wage Proof")),
            filename=safe_filename(stub["original_filename"], fallback=str(stub["id"])),
            local_path=str(stub["local_path"]),
        )
        if web_url or sp_path:
            await db.exec(
                "UPDATE pay_compensation_stubs SET sharepoint_url=?, sharepoint_path=? WHERE id=?",
                (web_url, sp_path, stub["id"]),
            )

    submission = docuseal.create_submission(
        pdf_bytes=packet_bytes,
        alignment_pdf_bytes=alignment_bytes,
        signers=signer_order,
        title=f"Local 3106 Pay Packet {period_start} to {period_end}",
        metadata={
            "pay_period_id": period_id,
            "pay_packet_id": packet_id,
            "form_key": _PAY_FORM_KEY,
            "president_signer_email": president_signer,
            "signer_order": signer_order,
        },
        template_id=resolve_docuseal_template_id(cfg, template_key=_PAY_FORM_KEY, doc_type=_PAY_FORM_KEY),
        form_key=_PAY_FORM_KEY,
    )
    signer_links_by_email: dict[str, str] = {}
    try:
        signer_links_by_email = docuseal.extract_signing_links_by_email(submission.raw)
    except Exception:
        signer_links_by_email = {}
    if not signer_links_by_email:
        try:
            signer_links_by_email = docuseal.fetch_signing_links_by_email(submission_id=submission.submission_id)
        except Exception:
            signer_links_by_email = {}
    first_signer = signer_order[0]
    signing_link = signer_links_by_email.get(first_signer.lower()) or submission.signing_link

    now = utcnow()
    await db.exec(
        """INSERT INTO pay_packets(
             id, period_id, revision, status, voucher_paths_json, voucher_pdf_paths_json,
             unsigned_packet_path, unsigned_packet_sha256, docuseal_submission_id,
             docuseal_signing_link, sharepoint_unsigned_url, created_at_utc, updated_at_utc
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            packet_id,
            period_id,
            int(period["revision"]),
            "awaiting_signature",
            json.dumps(voucher_paths),
            json.dumps(voucher_pdf_paths),
            unsigned_packet_path,
            sha,
            submission.submission_id,
            signing_link,
            sharepoint_unsigned_url,
            now,
            now,
        ),
    )
    await db.exec(
        """UPDATE pay_periods
           SET status='awaiting_signature', locked_by=?, locked_at_utc=?, president_email=?,
               sharepoint_folder_path=?, updated_at_utc=?
           WHERE id=?""",
        (actor.email, now, president_signer, folder_path, now, period_id),
    )
    await db.exec(
        "UPDATE pay_entries SET locked_at_utc=? WHERE period_id=? AND locked_at_utc IS NULL",
        (now, period_id),
    )
    await add_pay_event(
        db,
        period_id=period_id,
        packet_id=packet_id,
        event_type="period_locked_packet_sent",
        actor=actor.email,
        details={
            "docuseal_submission_id": submission.submission_id,
            "signing_link": signing_link,
            "president_signer_email": president_signer,
            "signer_order": signer_order,
        },
    )
    return {
        "packet_id": packet_id,
        "period_id": period_id,
        "status": "awaiting_signature",
        "docuseal_submission_id": submission.submission_id,
        "signing_link": signing_link,
        "signer_order": signer_order,
        "sharepoint_unsigned_url": sharepoint_unsigned_url,
    }


async def create_revision(db: Db, *, period_id: str, actor: PayActor) -> dict[str, object]:
    if not actor.can_lock:
        raise PermissionError("treasurer access required")
    period = await get_pay_period(db, period_id)
    if not period:
        raise ValueError("pay period not found")
    start = str(period["period_start"])
    end = str(period["period_end"])
    next_revision = int(period["revision"]) + 1
    new_id = period_id_for(date.fromisoformat(start), date.fromisoformat(end), next_revision)
    now = utcnow()
    await db.exec(
        """INSERT INTO pay_periods(id, period_start, period_end, status, revision, created_at_utc, updated_at_utc)
           VALUES(?,?,?,?,?,?,?)""",
        (new_id, start, end, "open", next_revision, now, now),
    )
    await add_pay_event(
        db,
        period_id=new_id,
        event_type="revision_created",
        actor=actor.email,
        details={"source_period_id": period_id, "revision": next_revision},
    )
    return await get_pay_period(db, new_id) or {"id": new_id}


async def handle_pay_docuseal_completion(
    *,
    db: Db,
    cfg: Any,
    graph: Any,
    mailer: Any,
    docuseal: Any,
    submission_id: str,
    payload: dict[str, object],
) -> dict[str, object] | None:
    _ = payload
    row = await db.fetchone(
        """SELECT p.id, p.period_id, p.status, pp.period_start, pp.period_end, pp.sharepoint_folder_path
           FROM pay_packets p
           JOIN pay_periods pp ON pp.id = p.period_id
           WHERE p.docuseal_submission_id=?""",
        (submission_id,),
    )
    if not row:
        return None
    packet_id, period_id, status, period_start, period_end, folder_path = row
    if str(status) == "completed":
        return {"ok": True, "deduped": True, "pay_packet_id": packet_id}

    packet_dir = Path(cfg.data_root) / "pay" / str(period_id) / "packet" / str(packet_id)
    packet_dir.mkdir(parents=True, exist_ok=True)
    signed_path: str | None = None
    audit_path: str | None = None
    signed_bytes: bytes | None = None
    artifacts = docuseal.download_completed_artifacts(submission_id=submission_id)
    zip_bytes = artifacts.get("completed_zip_bytes")
    if isinstance(zip_bytes, (bytes, bytearray)) and zip_bytes:
        audit_path = str(packet_dir / "docuseal_completed.zip")
        Path(audit_path).write_bytes(bytes(zip_bytes))
        signed_bytes = _extract_first_pdf(bytes(zip_bytes))
    if signed_bytes:
        signed_path = str(packet_dir / f"{period_start}_to_{period_end}_signed.pdf")
        Path(signed_path).write_bytes(signed_bytes)
    else:
        unsigned_row = await db.fetchone("SELECT unsigned_packet_path FROM pay_packets WHERE id=?", (packet_id,))
        if unsigned_row and unsigned_row[0] and Path(str(unsigned_row[0])).exists():
            signed_path = str(unsigned_row[0])
            signed_bytes = Path(signed_path).read_bytes()

    target_folder = str(folder_path or "").strip() or pay_period_folder_path(
        root_folder=cfg.pay_portal.sharepoint_root_folder,
        period_start=str(period_start),
        period_end=str(period_end),
    )
    signed_url: str | None = None
    audit_url: str | None = None
    if signed_path:
        signed_url, _ = await _upload_if_configured(
            cfg=cfg,
            graph=graph,
            folder_path=target_folder,
            filename=Path(signed_path).name,
            local_path=signed_path,
        )
    if audit_path:
        audit_url, _ = await _upload_if_configured(
            cfg=cfg,
            graph=graph,
            folder_path="/".join((target_folder, "Audit")),
            filename=Path(audit_path).name,
            local_path=audit_path,
        )

    now = utcnow()
    await db.exec(
        """UPDATE pay_packets
           SET status='completed', signed_packet_path=?, audit_zip_path=?,
               sharepoint_signed_url=?, sharepoint_audit_url=?, completed_at_utc=?, updated_at_utc=?
           WHERE id=?""",
        (signed_path, audit_path, signed_url, audit_url, now, now, packet_id),
    )
    await db.exec(
        """UPDATE pay_periods
           SET status='completed', completed_at_utc=?, sharepoint_folder_path=?, updated_at_utc=?
           WHERE id=?""",
        (now, target_folder, now, period_id),
    )
    await add_pay_event(
        db,
        period_id=period_id,
        packet_id=packet_id,
        event_type="docuseal_completion_processed",
        actor="docuseal",
        details={"docuseal_submission_id": submission_id, "signed_url": signed_url, "audit_url": audit_url},
    )

    if mailer is not None and cfg.email.enabled:
        recipients = await treasurer_recipients(
            db,
            fallback=cfg.email.internal_recipients,
            pay_cfg=cfg.pay_portal,
        )
        attachments = None
        if signed_bytes and len(signed_bytes) <= cfg.email.max_attachment_bytes:
            attachments = [
                MailAttachment(
                    filename=Path(signed_path or "pay_packet_signed.pdf").name,
                    content_type="application/pdf",
                    content_bytes=signed_bytes,
                )
            ]
        if recipients:
            mailer.send_mail(
                to_recipients=recipients,
                subject=f"Pay packet signed: {period_start} to {period_end}",
                text_body=(
                    f"The pay packet for {period_start} to {period_end} has been signed.\n\n"
                    f"SharePoint folder: {target_folder}\n"
                    f"Signed packet: {signed_url or signed_path or 'unavailable'}\n"
                ),
                attachments=attachments,
            )

    return {"ok": True, "handled": True, "pay_packet_id": packet_id, "period_id": period_id}


def _parse_decimal_input(text: object) -> tuple[Decimal, int]:
    cleaned = str(text or "").replace("$", "").replace(",", "").strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", cleaned):
        raise ValueError("value must be a number")
    value = Decimal(cleaned)
    places = len(cleaned.split(".", 1)[1]) if "." in cleaned else 0
    return value, places


def _format_decimal(value: Decimal, places: int) -> str:
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}"


def parse_rate_user_input(rate_text: object) -> tuple[Decimal, str]:
    value, places = _parse_decimal_input(rate_text)
    if value <= 0:
        raise ValueError("rate must be greater than 0")
    if abs(value) >= Decimal("10"):
        rate = value / Decimal("100")
        return rate, _format_decimal(rate, places + 2)
    return value, _format_decimal(value, places or 2)


def mileage_rate_from_settings(settings: dict[str, object], year: int) -> tuple[Decimal, str]:
    rates = settings.get("irs_rates")
    raw = None
    if isinstance(rates, dict):
        raw = rates.get(str(year))
    if raw is None:
        raw = "0.67"
    return parse_rate_user_input(raw)


def _google_leg(*, api_key: str, origin: str, destination: str) -> dict[str, object]:
    directions_url = "https://maps.googleapis.com/maps/api/directions/json"
    resp = requests.get(
        directions_url,
        params={"origin": origin, "destination": destination, "mode": "driving", "key": api_key},
        timeout=30,
    )
    data = resp.json()
    if data.get("status") != "OK" or not data.get("routes"):
        raise RuntimeError(f"Directions failed between '{origin}' and '{destination}': {data.get('status')}")
    route = data["routes"][0]
    leg = route["legs"][0]
    distance_meters = int((leg.get("distance") or {}).get("value", 0) or 0)
    distance_miles = Decimal(distance_meters) / _METERS_PER_MILE if distance_meters else Decimal("0")
    steps = []
    for step in leg.get("steps", []):
        text = html.unescape(re.sub("<.*?>", "", str(step.get("html_instructions") or ""))).strip()
        if text:
            steps.append(text)
    map_bytes = None
    polyline = (route.get("overview_polyline") or {}).get("points")
    if polyline:
        static_url = "https://maps.googleapis.com/maps/api/staticmap"
        static_params = [
            ("size", "600x400"),
            ("path", f"enc:{polyline}"),
            ("markers", f"color:blue|label:S|{leg.get('start_address', origin)}"),
            ("markers", f"color:red|label:E|{leg.get('end_address', destination)}"),
            ("key", api_key),
        ]
        try:
            map_resp = requests.get(static_url, params=static_params, timeout=30)
            if map_resp.status_code == 200 and map_resp.content:
                map_bytes = map_resp.content
        except requests.RequestException:
            map_bytes = None
    return {
        "origin": leg.get("start_address", origin),
        "destination": leg.get("end_address", destination),
        "distance_text": (leg.get("distance") or {}).get("text") or "",
        "distance_miles": distance_miles,
        "turn_by_turn": steps,
        "map_bytes": map_bytes,
    }


def build_mileage_pdf(
    *,
    name: str,
    local_number: str,
    date_str: str,
    description: str,
    rate: Decimal,
    rate_display: str,
    locations: list[str],
    google_maps_api_key: str,
) -> tuple[bytes, Decimal, Decimal]:
    try:
        from reportlab.lib.units import inch
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Image as ReportLabImage
        from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:  # pragma: no cover - runtime packaging guard
        raise RuntimeError("reportlab is required for mileage PDF generation") from exc

    if not google_maps_api_key:
        raise RuntimeError("Google Maps API key is not configured")
    if len(locations) < 2:
        raise ValueError("at least two locations are required")

    legs = []
    total_miles = Decimal("0")
    for idx in range(len(locations) - 1):
        leg = _google_leg(
            api_key=google_maps_api_key,
            origin=locations[idx],
            destination=locations[idx + 1],
        )
        total_miles += leg["distance_miles"]  # type: ignore[operator]
        legs.append(leg)

    reimbursement = (total_miles * rate).quantize(_CURRENCY, rounding=ROUND_HALF_UP)
    total_miles_display = total_miles.quantize(_MILES, rounding=ROUND_HALF_UP)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Mileage Report", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"Name: {name}", styles["Normal"]),
    ]
    if local_number:
        story.append(Paragraph(f"Local: {local_number}", styles["Normal"]))
    story.extend(
        [
            Paragraph(f"Date: {date_str}", styles["Normal"]),
            Paragraph(f"Description: {description}", styles["Normal"]),
            Spacer(1, 12),
            Paragraph(f"IRS Standard Mileage Rate: ${rate_display} per mile", styles["Normal"]),
            Paragraph(f"Total Distance: {_decimal_text(total_miles_display)} miles", styles["Normal"]),
            Paragraph(f"Total Reimbursement: ${_decimal_text(reimbursement)}", styles["Normal"]),
            Spacer(1, 18),
        ]
    )
    for index, leg in enumerate(legs, start=1):
        story.extend(
            [
                Paragraph(f"Route {index}:", styles["Heading3"]),
                Paragraph(f"From: {leg['origin']}", styles["Normal"]),
                Paragraph(f"To: {leg['destination']}", styles["Normal"]),
                Paragraph(
                    f"Distance: {leg.get('distance_text') or _decimal_text(leg['distance_miles']) + ' mi'}",
                    styles["Normal"],
                ),
                Spacer(1, 8),
            ]
        )
    story.extend(
        [
            Spacer(1, 12),
            Paragraph("Distances computed by Google Directions API on the server side.", styles["Normal"]),
        ]
    )
    story.append(PageBreak())
    for index, leg in enumerate(legs, start=1):
        story.extend(
            [
                Paragraph(f"Route {index} Detail", styles["Heading2"]),
                Spacer(1, 6),
                Paragraph(f"From: {leg['origin']}", styles["Normal"]),
                Paragraph(f"To: {leg['destination']}", styles["Normal"]),
                Paragraph(
                    f"Distance: {leg.get('distance_text') or _decimal_text(leg['distance_miles']) + ' mi'}",
                    styles["Normal"],
                ),
                Spacer(1, 12),
            ]
        )
        if leg.get("map_bytes"):
            map_image = ReportLabImage(io.BytesIO(leg["map_bytes"]))  # type: ignore[arg-type]
            map_image.drawWidth = 6 * inch
            map_image.drawHeight = 4 * inch
            story.extend([map_image, Spacer(1, 12)])
        story.append(Paragraph("Turn-by-Turn Directions:", styles["Heading3"]))
        for step in leg.get("turn_by_turn", []):
            story.append(Paragraph(str(step), styles["Normal"]))
        story.append(PageBreak())
    doc.build(story)
    return buf.getvalue(), reimbursement, total_miles_display


async def create_mileage_attachment(
    *,
    db: Db,
    cfg: Any,
    period_id: str,
    entry_id: str,
    actor: PayActor,
    name: str,
    local_number: str,
    date_str: str,
    description: str,
    locations: list[str],
    rate_text: str | None,
) -> dict[str, object]:
    settings = await pay_settings(db, pay_cfg=cfg.pay_portal)
    year = datetime.strptime(date_str, "%Y-%m-%d").year
    rate, rate_display = parse_rate_user_input(rate_text) if rate_text else mileage_rate_from_settings(settings, year)
    pdf_bytes, reimbursement, total_miles = build_mileage_pdf(
        name=name,
        local_number=local_number,
        date_str=date_str,
        description=description,
        rate=rate,
        rate_display=rate_display,
        locations=locations,
        google_maps_api_key=cfg.pay_portal.google_maps_api_key,
    )
    yyyymmdd = date_str.replace("-", "")
    filename = f"{yyyymmdd} {name}.pdf"
    attachment = await store_attachment(
        db,
        cfg=cfg,
        period_id=period_id,
        entry_id=entry_id,
        actor=actor,
        attachment_type="mileage_pdf",
        filename=filename,
        content_type="application/pdf",
        content=pdf_bytes,
        scan=False,
    )
    row = await db.fetchone(
        "SELECT mileage_amount FROM pay_entries WHERE id=? AND period_id=?",
        (entry_id, period_id),
    )
    current_mileage_amount = _money(row[0] if row else 0)
    await db.exec(
        """UPDATE pay_entries
           SET mileage_miles=?, mileage_rate=?, mileage_amount=?, updated_at_utc=?
           WHERE id=? AND period_id=?""",
        (
            float(total_miles),
            float(rate),
            float(current_mileage_amount + reimbursement),
            utcnow(),
            entry_id,
            period_id,
        ),
    )
    return {**attachment, "mileage_miles": float(total_miles), "reimbursement": float(reimbursement)}
