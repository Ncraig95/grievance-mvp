from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..db.db import Db
from ..services.pay_portal import (
    PayActor,
    add_pay_event,
    approve_irs_rate_candidate,
    create_pay_demo_feedback,
    add_pay_fund_ledger_entry,
    attachment_for_actor,
    create_pay_entry_correction,
    delete_pay_entry,
    delete_pay_profile,
    create_mileage_attachment,
    create_revision,
    current_period_bounds,
    decode_content_base64,
    ensure_pay_period,
    generate_pay_demo_artifacts,
    generate_pay_fund_packet,
    fund_fica_rate_from_settings,
    list_attachments,
    list_compensation_stubs,
    list_entries,
    list_pay_demo_artifacts,
    list_pay_fund_allocations,
    list_pay_fund_attachment_links,
    list_pay_fund_packets,
    list_pay_funds,
    list_pay_demo_feedback,
    list_irs_rate_candidates,
    list_pay_profiles,
    list_pay_profile_change_requests,
    list_pay_users,
    list_wage_scales,
    load_common_places_cache,
    load_sharepoint_common_places,
    lock_period_and_send_packet,
    merge_common_places,
    normalize_email,
    pay_demo_settings,
    pay_demo_artifact_path,
    pay_profile_by_email,
    pay_fund_packet_by_id,
    pay_profile_wage_fields_changed,
    pay_settings,
    remove_mileage_attachment,
    request_pay_profile_change,
    review_pay_entry,
    review_pay_profile_change_request,
    save_pay_demo_settings,
    save_pay_settings,
    store_attachment,
    store_compensation_stub,
    link_pay_attachment_to_fund,
    sync_irs_mileage_rate_candidates,
    treasurer_recipients,
    validate_mileage_locations,
    update_pay_demo_feedback_status,
    upsert_entry,
    upsert_pay_profile,
    upsert_pay_fund,
    save_pay_fund_allocations_for_entry,
    upsert_pay_user,
    upsert_wage_scale,
    write_common_places_cache,
)
from ..services.pdf_convert import docx_to_pdf
from ..services.internal_roles import (
    active_internal_roles_for_user,
    delete_internal_role_assignment,
    internal_role_assignment_by_id,
    list_internal_role_assignments,
    normalize_internal_role,
    upsert_internal_role_assignment,
)
from .models import DirectoryUserSearchResponse
from .officer_auth import (
    current_external_steward_user,
    current_officer_user,
)
from .routes_officers import search_directory_users_for_request


router = APIRouter()
_PAY_ROUTE_LOGGER = logging.getLogger("grievance_api.pay_portal.routes")
_DEMO_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_DEMO_JOBS_LOCK = threading.Lock()
_DEMO_JOBS: dict[str, dict[str, Any]] = {}


async def _record_pay_lock_failure(
    request: Request,
    *,
    period_id: str,
    actor: PayActor,
    exc: Exception,
    status_code: int,
) -> None:
    _PAY_ROUTE_LOGGER.error(
        "pay_lock_send_failed period_id=%s actor=%s status_code=%s error_type=%s error=%s",
        period_id,
        actor.email,
        status_code,
        type(exc).__name__,
        str(exc),
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    try:
        await add_pay_event(
            request.app.state.db,
            period_id=period_id,
            event_type="period_lock_send_failed",
            actor=actor.email,
            details={
                "status_code": status_code,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "route": f"/pay/api/periods/{period_id}/lock",
            },
        )
    except Exception as audit_exc:
        _PAY_ROUTE_LOGGER.error(
            "pay_lock_send_failure_audit_failed period_id=%s actor=%s error_type=%s error=%s",
            period_id,
            actor.email,
            type(audit_exc).__name__,
            str(audit_exc),
            exc_info=(type(audit_exc), audit_exc, audit_exc.__traceback__),
        )


class PayEntryUpsertRequest(BaseModel):
    period_id: str | None = None
    user_email: str | None = None
    display_name: str | None = None
    entry_date: str
    weekly_basis_hours: float = 40.0
    local_number: str | None = "3106"
    address: str | None = None
    hourly_rate: float = 0
    lost_wage_input_type: str = "hourly"
    lost_wage_amount: float = 0
    hours: float = 0
    mileage_miles: float = 0
    mileage_rate: float = 0
    mileage_amount: float = 0
    rentals_amount: float = 0
    meals_amount: float = 0
    hotel_amount: float = 0
    miscellaneous_amount: float = 0
    president_diff_hours: float = 0
    submitter_certified: bool = False
    submitter_certification_text: str | None = None
    notes: str | None = None


class PayAttachmentUploadRequest(BaseModel):
    period_id: str
    filename: str
    content_type: str | None = None
    content_base64: str
    attachment_type: str = "receipt"


class PayFundUpsertRequest(BaseModel):
    id: str | None = None
    fund_type: str = "sif"
    name: str
    status: str = "active"
    local_number: str | None = "3106"
    description: str | None = None


class PayFundLedgerEntryRequest(BaseModel):
    ledger_type: str = "advance"
    amount: float
    effective_date: str
    reference: str | None = None
    notes: str | None = None


class PayFundAllocationRow(BaseModel):
    fund_id: str
    hours: float = 0
    mileage_miles: float = 0
    mileage_amount: float = 0
    rentals_amount: float = 0
    meals_amount: float = 0
    hotel_amount: float = 0
    miscellaneous_amount: float = 0
    notes: str | None = None


class PayFundAllocationsRequest(BaseModel):
    allocations: list[PayFundAllocationRow] = Field(default_factory=list)


class PayFundAttachmentLinkRequest(BaseModel):
    fund_id: str
    allocation_id: str | None = None
    notes: str | None = None


class PayFundPacketRequest(BaseModel):
    period_start: str
    period_end: str


class PayCompensationStubRequest(BaseModel):
    user_email: str | None = None
    base_wage_input_type: str = "hourly"
    base_wage_amount: float = 0
    weekly_basis_hours: float = 40.0
    commission_month_1_amount: float = 0
    commission_month_2_amount: float = 0
    commission_month_3_amount: float = 0
    payroll_month: str | None = None
    filename: str
    content_type: str | None = None
    content_base64: str
    notes: str | None = None


class PayMileageRequest(BaseModel):
    period_id: str
    name: str | None = None
    local_number: str | None = "3106"
    date: str
    description: str
    locations: list[str] = Field(default_factory=list)
    rate: str | None = None


class PayMileageAddressCheckRequest(BaseModel):
    locations: list[str] = Field(default_factory=list)


class PaySettingsUpdateRequest(BaseModel):
    president_email: str | None = None
    treasurer_emails: list[str] | None = None
    irs_rates: dict[str, str] | None = None
    common_places: list[dict[str, str]] | None = None


class PayDemoFeedbackRequest(BaseModel):
    screen: str | None = "demo"
    category: str | None = "suggestion"
    demo_step: int | None = 0
    demo_cycle_title: str | None = None
    comment: str


class PayDemoFeedbackStatusRequest(BaseModel):
    status: str


class PayDemoArtifactRequest(BaseModel):
    demo_step: int | None = 0
    demo_cycle_title: str | None = None


class PayDemoSettingsUpdateRequest(BaseModel):
    demo_mode_enabled: bool | None = None
    demo_cycle_title: str | None = None
    demo_cycle_notes: str | None = None


class PayUserUpsertRequest(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "guest"
    status: str = "active"


class PayEntryReviewRequest(BaseModel):
    review_status: str
    review_note: str | None = None


class PayEntryCorrectionRequest(BaseModel):
    period_id: str
    user_email: str
    display_name: str | None = None
    entry_date: str
    local_number: str | None = "3106"
    address: str | None = None
    hours: float = 0
    mileage_miles: float = 0
    mileage_rate: float = 0
    mileage_amount: float = 0
    rentals_amount: float = 0
    meals_amount: float = 0
    hotel_amount: float = 0
    miscellaneous_amount: float = 0
    notes: str | None = None


class InternalRoleAssignmentRequest(BaseModel):
    principal_id: str | None = None
    principal_email: str
    principal_display_name: str | None = None
    role: str
    status: str = "active"


class PayInternalUserImportRequest(BaseModel):
    limit: int = Field(default=999, ge=1, le=999)
    confirm: bool = False


class PayProfileChangeReviewRequest(BaseModel):
    approved: bool
    review_note: str | None = None


class PayProfileUpsertRequest(BaseModel):
    principal_id: str | None = None
    principal_email: str
    principal_display_name: str | None = None
    pay_basis: str = "expense_only"
    base_wage_input_type: str = "hourly"
    base_wage_amount: float = 0
    weekly_basis_hours: float = 40.0
    commission_month_1_amount: float = 0
    commission_month_2_amount: float = 0
    commission_month_3_amount: float = 0
    status: str = "active"
    notes: str | None = None
    default_address: str | None = None


class PayWageScaleUpsertRequest(BaseModel):
    effective_date: str
    weekly_basis_hours: float = 40.0
    target_scale: str = "36"
    actual_scale: str = "base"
    target_weekly_amount: float
    actual_weekly_amount: float | None = None
    target_multiplier: float = 1.20
    notes: str | None = None


class PayLockRequest(BaseModel):
    president_email: str | None = None


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail=message)


def _update_demo_job(job_id: str, **updates: object) -> None:
    with _DEMO_JOBS_LOCK:
        job = _DEMO_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _run_demo_artifact_job(
    *,
    job_id: str,
    cfg: Any,
    graph: Any,
    actor: PayActor,
    pay_settings_payload: dict[str, object],
    demo_step: int,
    demo_cycle_title: str,
    period_start: str,
    period_end: str,
) -> None:
    try:
        _update_demo_job(
            job_id,
            status="running",
            progress={"stage": "start", "current": 0, "total": 1, "message": "Starting demo packet"},
            message="Starting demo packet",
        )

        def progress(payload: dict[str, object]) -> None:
            _update_demo_job(job_id, progress=payload, message=payload.get("message") or "")

        rows = generate_pay_demo_artifacts(
            cfg=cfg,
            settings=pay_settings_payload,
            actor=actor,
            demo_step=demo_step,
            demo_cycle_title=demo_cycle_title,
            period_start=period_start,
            period_end=period_end,
            docx_to_pdf_func=docx_to_pdf,
            graph=graph,
            progress_callback=progress,
        )
        _update_demo_job(
            job_id,
            status="completed",
            rows=rows,
            progress={"stage": "complete", "current": 1, "total": 1, "message": "Demo packet ready"},
            message="Demo packet ready",
        )
    except Exception as exc:  # noqa: BLE001
        _update_demo_job(
            job_id,
            status="failed",
            error=str(exc),
            progress={"stage": "failed", "current": 0, "total": 1, "message": str(exc)},
            message=str(exc),
        )


def _demo_job_snapshot(job_id: str, *, actor: PayActor, request: Request) -> dict[str, object]:
    with _DEMO_JOBS_LOCK:
        job = dict(_DEMO_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="demo job not found")
    if str(job.get("actor_email") or "") != actor.email and not actor.can_view_all:
        raise _forbidden("demo job not found")
    rows = _demo_artifact_rows_for_response(request, actor) if job.get("status") == "completed" else job.get("rows") or []
    return {**job, "job_id": job_id, "rows": rows}


def _actor_from_officer(
    user: Any,
    *,
    treasurer: bool,
    president: bool = False,
    internal_roles: tuple[str, ...] = (),
) -> PayActor:
    role = str(getattr(user, "role", "") or "").lower()
    is_admin = role == "admin"
    role_set = {str(value or "").strip().lower() for value in internal_roles}
    is_treasurer = bool(treasurer or is_admin or "treasurer" in role_set)
    is_president = bool(president or "president" in role_set)
    is_pay_viewer = bool("pay_viewer" in role_set)
    actor_role = (
        "admin"
        if is_admin
        else "treasurer"
        if is_treasurer
        else "president"
        if is_president
        else "pay_viewer"
        if is_pay_viewer
        else "officer"
    )
    return PayActor(
        email=normalize_email(getattr(user, "email", "")),
        display_name=getattr(user, "display_name", None),
        role=actor_role,
        can_view_all=bool(is_treasurer or is_pay_viewer),
        can_edit_all=is_treasurer,
        can_lock=is_treasurer,
        is_guest=False,
        is_president=is_president,
    )


async def _current_pay_actor(request: Request) -> PayActor | None:
    cfg = request.app.state.cfg
    if not cfg.pay_portal.enabled:
        raise HTTPException(status_code=503, detail="pay portal is disabled")

    db: Db = request.app.state.db
    settings = await pay_settings(db, pay_cfg=cfg.pay_portal)
    treasurer_emails = {
        normalize_email(value)
        for value in settings.get("treasurer_emails", [])
        if normalize_email(value)
    }
    for recipient in await treasurer_recipients(db, fallback=(), pay_cfg=cfg.pay_portal):
        treasurer_emails.add(normalize_email(recipient))

    officer = await current_officer_user(request)
    if officer:
        officer_email = normalize_email(officer.email)
        president_address = normalize_email(settings.get("president_email"))
        internal_roles = await active_internal_roles_for_user(
            db,
            user_id=getattr(officer, "user_id", None),
            email=officer_email,
        )
        return _actor_from_officer(
            officer,
            treasurer=officer_email in treasurer_emails,
            president=bool(president_address and officer_email == president_address),
            internal_roles=internal_roles,
        )

    external = await current_external_steward_user(request)
    if external:
        user_row = await db.fetchone(
            "SELECT email, display_name, role, status FROM pay_users WHERE email=?",
            (normalize_email(external.email),),
        )
        if not user_row:
            external_email = normalize_email(external.email)
            for configured in cfg.pay_portal.pay_users:
                if normalize_email(configured.get("email")) == external_email:
                    user_row = (
                        configured.get("email"),
                        configured.get("display_name"),
                        configured.get("role") or "guest",
                        configured.get("status") or "active",
                    )
                    break
        if not user_row or str(user_row[3]).lower() != "active":
            raise _forbidden("external pay user is not allowlisted")
        role = str(user_row[2] or "guest").lower()
        is_treasurer = role == "treasurer"
        return PayActor(
            email=normalize_email(user_row[0]),
            display_name=str(user_row[1] or external.display_name or external.email),
            role=role,
            can_view_all=is_treasurer,
            can_edit_all=is_treasurer,
            can_lock=is_treasurer,
            is_guest=True,
            is_president=False,
        )

    return None


async def _require_pay_actor(request: Request) -> PayActor:
    actor = await _current_pay_actor(request)
    if actor:
        return actor
    raise HTTPException(status_code=401, detail="login required")


async def _require_treasurer(request: Request) -> PayActor:
    actor = await _require_pay_actor(request)
    if not actor.can_lock:
        raise _forbidden("treasurer access required")
    return actor


async def _pay_settings_for_request(request: Request) -> dict[str, object]:
    cfg = request.app.state.cfg
    settings = await pay_settings(request.app.state.db, pay_cfg=cfg.pay_portal)
    cached_places = load_common_places_cache(data_root=cfg.data_root)
    try:
        sharepoint_places = load_sharepoint_common_places(
            graph=getattr(request.app.state, "graph", None),
            graph_cfg=cfg.graph,
            pay_cfg=cfg.pay_portal,
        )
    except Exception as exc:
        if cached_places:
            settings["common_places"] = merge_common_places(cached_places, settings.get("common_places"))
            settings["common_places_source"] = "app_file"
        else:
            settings["common_places_source"] = "local"
        settings["common_places_warning"] = f"SharePoint mileage config unavailable: {exc}"
        return settings

    if sharepoint_places:
        synced_places = merge_common_places(sharepoint_places)
        try:
            write_common_places_cache(data_root=cfg.data_root, places=synced_places)
            settings["common_places_cache_updated"] = True
        except Exception as exc:
            settings["common_places_cache_updated"] = False
            settings["common_places_warning"] = f"SharePoint places loaded but app file was not updated: {exc}"
        settings["common_places"] = merge_common_places(synced_places, settings.get("common_places"))
        settings["common_places_source"] = "sharepoint"
    elif cached_places:
        settings["common_places"] = merge_common_places(cached_places, settings.get("common_places"))
        settings["common_places_source"] = "app_file"
    else:
        settings["common_places_source"] = "local"
    return settings


def _demo_artifact_rows_for_response(request: Request, actor: PayActor) -> list[dict[str, object]]:
    rows = list_pay_demo_artifacts(data_root=request.app.state.cfg.data_root, actor=actor)
    for row in rows:
        row["download_url"] = f"/pay/api/demo/artifacts/{quote(str(row['filename']))}"
    return rows


def _render_pay_page() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lost Wage Portal</title>
  <style>
    :root { color-scheme: light; font-family: Arial, sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #1f2933; }
    header { background: #ffffff; border-bottom: 1px solid #d9dee7; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; }
    main { max-width: 1200px; margin: 0 auto; padding: 20px; display: grid; gap: 16px; }
    section, dialog { background: #fff; border: 1px solid #d9dee7; border-radius: 6px; padding: 16px; }
    h1 { font-size: 24px; margin: 0; }
    h2 { font-size: 18px; margin: 0 0 12px; }
    label { display: grid; gap: 4px; font-size: 13px; font-weight: 600; }
    input, textarea, select, button { font: inherit; }
    input, textarea, select { border: 1px solid #b9c1cf; border-radius: 4px; padding: 8px; min-width: 0; }
    textarea { min-height: 72px; }
    button { border: 1px solid #1b5e7a; border-radius: 4px; background: #1b5e7a; color: white; padding: 9px 12px; cursor: pointer; }
    button.secondary { background: #fff; color: #1b5e7a; }
    button.danger { background: #8a1f1f; border-color: #8a1f1f; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px; border-bottom: 1px solid #e4e8ef; text-align: left; font-size: 13px; vertical-align: top; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .muted { color: #596779; font-size: 13px; }
    .hidden { display: none; }
    @media (max-width: 760px) { .grid { grid-template-columns: 1fr; } header { align-items: flex-start; flex-direction: column; gap: 8px; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Lost Wage Portal</h1>
      <div id="periodLabel" class="muted"></div>
    </div>
    <div class="toolbar">
      <a href="/auth/login?next=/pay">Officer login</a>
      <a href="/auth/steward/login?next=/pay">External login</a>
    </div>
  </header>
  <main>
    <section>
      <h2>Lost Wage Entry</h2>
      <form id="entryForm">
        <div class="grid">
          <label>Date<input id="entryDate" name="entry_date" type="date" required></label>
          <input id="displayName" name="display_name" type="hidden">
          <label>Hours<input name="hours" type="number" step="0.25"></label>
          <label>Rentals ($)<input name="rentals_amount" type="number" step="0.01"></label>
          <label>Meals ($)<input name="meals_amount" type="number" step="0.01"></label>
          <label>Hotel ($)<input name="hotel_amount" type="number" step="0.01"></label>
          <label>Miscellaneous ($)<input name="miscellaneous_amount" type="number" step="0.01"></label>
          <label>Local<input name="local_number" value="3106"></label>
        </div>
        <label>Address<input name="address"></label>
        <label>Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit">Save Entry</button><span id="entryStatus" class="muted"></span></div>
      </form>
    </section>
    <section>
      <h2>Pay Proof Attachments</h2>
      <form id="stubForm">
        <div class="grid">
          <label>Member Email<input name="user_email"></label>
          <label>Base Wage Type<select name="base_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
          <label>Base Wage Amount ($)<input name="base_wage_amount" type="number" step="0.01"></label>
          <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label>Commission Month 1 ($)<input name="commission_month_1_amount" type="number" step="0.01"></label>
          <label>Commission Month 2 ($)<input name="commission_month_2_amount" type="number" step="0.01"></label>
          <label>Commission Month 3 ($)<input name="commission_month_3_amount" type="number" step="0.01"></label>
          <label>Work Pay Stub<input id="stubFile" type="file" accept=".pdf,image/png,image/jpeg"></label>
        </div>
        <label>Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit" class="secondary">Save Work Pay Stub</button><span id="stubStatus" class="muted"></span></div>
      </form>
      <table><thead><tr><th>Member</th><th>Base</th><th>Commission Avg</th><th>Commission Hr</th><th>Total Hr</th><th>Stub</th></tr></thead><tbody id="stubsBody"></tbody></table>
    </section>
    <section>
      <h2>Entries</h2>
      <div class="toolbar">
        <input id="receiptFile" type="file" accept=".pdf,image/png,image/jpeg">
        <button id="uploadReceiptBtn" type="button" class="secondary">Attach Receipt To Selected</button>
      </div>
      <table><thead><tr><th></th><th>Date</th><th>Name</th><th>Hours</th><th>Mileage</th><th>Other</th><th>President Diff</th><th>Sign-Off</th><th>Notes</th></tr></thead><tbody id="entriesBody"></tbody></table>
    </section>
    <section id="treasurerPanel" class="hidden">
      <h2>Treasurer</h2>
      <div class="toolbar">
        <button id="lockBtn" type="button">Lock And Send</button>
        <button id="revisionBtn" type="button" class="secondary">Create Revision</button>
        <span id="treasurerStatus" class="muted"></span>
      </div>
    </section>
    <section id="settingsPanel" class="hidden">
      <h2>Settings</h2>
      <form id="settingsForm">
        <div class="grid">
          <label>President Email<input name="president_email"></label>
          <label>Treasurer Emails<input name="treasurer_emails"></label>
          <label>Effective Date<input name="effective_date" type="date"></label>
          <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label>Scale 36 Weekly Base ($)<input name="target_weekly_amount" type="number" step="0.01"></label>
          <label>President Target<input value="Scale 36 + 20%" disabled></label>
        </div>
        <div class="toolbar"><button type="submit" class="secondary">Save Settings</button><span id="settingsStatus" class="muted"></span></div>
      </form>
      <div class="toolbar" style="margin-top:16px;">
        <button id="irsSyncBtn" type="button" class="secondary">Check IRS Rates</button>
        <span id="irsStatus" class="muted"></span>
      </div>
      <table style="margin-top:10px;">
        <thead><tr><th>Year</th><th>Rate</th><th>Source</th><th>Status</th><th></th></tr></thead>
        <tbody id="irsCandidatesBody"></tbody>
      </table>
    </section>
  </main>
<script>
let context = null;
let selectedEntryId = null;
function money(value) { return Number(value || 0).toFixed(2); }
function currency(value) { return `$${money(value)}`; }
function rateCurrency(value, digits = 3) { return `$${Number(value || 0).toFixed(digits)}`; }
function addCell(row, value) { const cell = document.createElement('td'); cell.textContent = value || ''; row.appendChild(cell); }
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return await res.json();
}
async function loadContext() {
  context = await api('/pay/api/context');
  document.getElementById('periodLabel').textContent = `${context.period.period_start} to ${context.period.period_end} - ${context.period.status}`;
  document.getElementById('treasurerPanel').classList.toggle('hidden', !context.actor.can_lock);
  document.getElementById('settingsPanel').classList.toggle('hidden', !context.actor.can_lock);
  const body = document.getElementById('entriesBody');
  body.innerHTML = '';
  for (const row of context.entries) {
    const tr = document.createElement('tr');
    const other = Number(row.rentals_amount || 0) + Number(row.meals_amount || 0) + Number(row.hotel_amount || 0) + Number(row.miscellaneous_amount || 0);
    tr.innerHTML = `<td><input type="radio" name="entryPick" value="${row.id}"></td><td>${row.entry_date}</td><td>${row.display_name || row.user_email}</td><td>${money(row.hours)}</td><td>${currency(row.mileage_amount)}</td><td>${currency(other)}</td><td>${currency(row.president_diff_amount)}</td><td></td>`;
    tr.lastChild.textContent = row.notes || '';
    body.appendChild(tr);
  }
  body.querySelectorAll('input[name="entryPick"]').forEach(input => input.addEventListener('change', () => selectedEntryId = input.value));
  const stubsBody = document.getElementById('stubsBody');
  stubsBody.innerHTML = '';
  for (const row of context.compensation_stubs || []) {
    const tr = document.createElement('tr');
    addCell(tr, row.user_email);
    addCell(tr, row.payroll_month);
    addCell(tr, `${row.base_wage_input_type} ${currency(row.base_wage_amount)}`);
    addCell(tr, currency(row.commission_average_monthly));
    addCell(tr, currency(row.commission_hourly_rate));
    addCell(tr, currency(row.calculated_hourly_rate));
    addCell(tr, row.filename);
    stubsBody.appendChild(tr);
  }
  renderIrsCandidates(context.irs_rate_candidates || []);
}
function renderIrsCandidates(rows) {
  const body = document.getElementById('irsCandidatesBody');
  if (!body) return;
  body.innerHTML = '';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8;
    td.textContent = 'No staged IRS rates.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.rate_year);
    addCell(tr, rateCurrency(row.rate_per_mile, 3));
    const sourceCell = document.createElement('td');
    const link = document.createElement('a');
    link.href = row.source_url;
    link.textContent = row.source_title || row.source_url;
    link.rel = 'noreferrer';
    sourceCell.appendChild(link);
    tr.appendChild(sourceCell);
    addCell(tr, row.status);
    const actionCell = document.createElement('td');
    if (row.status === 'pending') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'secondary';
      btn.textContent = 'Approve';
      btn.addEventListener('click', () => approveIrsRate(row.id));
      actionCell.appendChild(btn);
    }
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
async function approveIrsRate(candidateId) {
  try {
    const result = await api(`/pay/api/irs-rates/${candidateId}/approve`, { method: 'POST', body: JSON.stringify({}) });
    document.getElementById('irsStatus').textContent = `Approved ${result.rate_year}: ${rateCurrency(result.active_rate, 3)}`;
    await loadContext();
  } catch (err) {
    document.getElementById('irsStatus').textContent = err.message;
  }
}
document.getElementById('entryForm').addEventListener('submit', async event => {
  event.preventDefault();
  balancePresidentDailyHours();
  const data = Object.fromEntries(new FormData(event.target).entries());
  data.period_id = context.period.id;
  data.display_name = data.display_name || (context.actor && (context.actor.display_name || context.actor.email)) || '';
  for (const key of ['hourly_rate','lost_wage_amount','hours','mileage_miles','mileage_rate','mileage_amount','rentals_amount','meals_amount','hotel_amount','miscellaneous_amount','president_diff_hours','weekly_basis_hours']) data[key] = Number(data[key] || 0);
  try { await api('/pay/api/entries', { method: 'POST', body: JSON.stringify(data) }); document.getElementById('entryStatus').textContent = 'Saved'; await loadContext(); }
  catch (err) { document.getElementById('entryStatus').textContent = err.message; }
});
document.getElementById('stubForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target).entries());
  const file = document.getElementById('stubFile').files[0];
  if (!file) { document.getElementById('stubStatus').textContent = 'Pay stub is required'; return; }
  const bytes = await file.arrayBuffer();
  const base64 = btoa(String.fromCharCode(...new Uint8Array(bytes)));
  const body = {
    user_email: form.user_email,
    base_wage_input_type: form.base_wage_input_type,
    base_wage_amount: Number(form.base_wage_amount || 0),
    weekly_basis_hours: Number(form.weekly_basis_hours || 40),
    commission_month_1_amount: Number(form.commission_month_1_amount || 0),
    commission_month_2_amount: Number(form.commission_month_2_amount || 0),
    commission_month_3_amount: Number(form.commission_month_3_amount || 0),
    filename: file.name,
    content_type: file.type,
    content_base64: base64,
    notes: form.notes,
  };
  try { await api('/pay/api/compensation-stubs', { method: 'POST', body: JSON.stringify(body) }); document.getElementById('stubStatus').textContent = 'Saved'; event.target.reset(); await loadContext(); }
  catch (err) { document.getElementById('stubStatus').textContent = err.message; }
});
document.getElementById('uploadReceiptBtn').addEventListener('click', async () => {
  const file = document.getElementById('receiptFile').files[0];
  if (!selectedEntryId || !file) return;
  const bytes = await file.arrayBuffer();
  const base64 = btoa(String.fromCharCode(...new Uint8Array(bytes)));
  try { await api(`/pay/api/entries/${selectedEntryId}/attachments`, { method: 'POST', body: JSON.stringify({ period_id: context.period.id, filename: file.name, content_type: file.type, content_base64: base64 }) }); await loadContext(); }
  catch (err) { alert(err.message); }
});
document.getElementById('lockBtn').addEventListener('click', async () => {
  try { const result = await api(`/pay/api/periods/${context.period.id}/lock`, { method: 'POST', body: JSON.stringify({}) }); document.getElementById('treasurerStatus').textContent = result.signing_link || 'Sent'; await loadContext(); }
  catch (err) { document.getElementById('treasurerStatus').textContent = err.message; }
});
document.getElementById('revisionBtn').addEventListener('click', async () => {
  try { await api(`/pay/api/periods/${context.period.id}/revision`, { method: 'POST', body: JSON.stringify({}) }); await loadContext(); }
  catch (err) { document.getElementById('treasurerStatus').textContent = err.message; }
});
document.getElementById('settingsForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target).entries());
  const settings = { president_email: form.president_email, treasurer_emails: String(form.treasurer_emails || '').split(',').map(v => v.trim()).filter(Boolean) };
  try {
    await api('/pay/api/settings', { method: 'PUT', body: JSON.stringify(settings) });
    if (form.effective_date && form.target_weekly_amount) {
      await api('/pay/api/wage-scales', { method: 'POST', body: JSON.stringify({ effective_date: form.effective_date, weekly_basis_hours: Number(form.weekly_basis_hours || 40), target_weekly_amount: Number(form.target_weekly_amount), target_multiplier: 1.20 }) });
    }
    document.getElementById('settingsStatus').textContent = 'Saved';
    await loadContext();
  } catch (err) { document.getElementById('settingsStatus').textContent = err.message; }
});
document.getElementById('irsSyncBtn').addEventListener('click', async () => {
  try {
    const result = await api('/pay/api/irs-rates/sync', { method: 'POST', body: JSON.stringify({}) });
    document.getElementById('irsStatus').textContent = result.detected.length ? `${result.detected.length} rate staged` : 'No new IRS rates';
    await loadContext();
  } catch (err) { document.getElementById('irsStatus').textContent = err.message; }
});
loadContext().catch(err => { document.querySelector('main').innerHTML = '<section><h2>Access</h2><p>' + err.message + '</p></section>'; });
</script>
</body>
</html>
"""


_PAY_VIEW_TITLES = {
    "entry": "Submit",
    "mileage": "Mileage",
    "demo": "Demo",
    "treasurer": "Treasurer",
    "admin": "Admin",
}


def _pay_nav_html(*, view: str, actor: PayActor) -> str:
    links = [("entry", "Submit"), ("mileage", "Mileage"), ("demo", "Demo")]
    if actor.can_view_all:
        links.append(("treasurer", "Treasurer"))
    if actor.can_lock:
        links.append(("admin", "Admin"))
    rendered: list[str] = []
    for key, label in links:
        current = ' aria-current="page"' if key == view else ""
        rendered.append(f'<a class="nav-link" href="/pay/{key}"{current}>{escape(label)}</a>')
    return "".join(rendered)


def _entry_form_html(*, president: bool = False, advanced: bool = False, president_enabled: bool = False) -> str:
    if president:
        differential_hours_html = (
            '<label>Differential Hours<input name="president_diff_hours" type="number" step="0.25" placeholder="defaults to union hours"></label>'
            if advanced
            else ""
        )
        return """
        <form id="entryForm" class="form-stack">
          <div class="field-grid">
            <label>Date<input name="entry_date" type="date" required></label>
            <label>Union Hours<input name="hours" type="number" step="0.25"></label>
            __DIFFERENTIAL_HOURS__
          </div>
          <label>Notes<textarea name="notes"></textarea></label>
          <div class="toolbar"><button type="submit">Save President Entry</button><span id="entryStatus" class="muted"></span></div>
        </form>
        """.replace("__DIFFERENTIAL_HOURS__", differential_hours_html)
    president_panel_class = "" if president_enabled else " disabled-panel"
    president_disabled = "" if president_enabled else " disabled"
    president_help = (
        "Scale 36 + 20% differential will calculate from your saved president profile."
        if president_enabled
        else "Only the configured president can enter president differential hours."
    )
    return """
        <form id="entryForm" class="form-stack">
          <div class="field-grid">
            <label>Date<input name="entry_date" type="date" required></label>
            <label>Hours<input name="hours" type="number" step="0.25"></label>
            <label>Meals ($)<input name="meals_amount" type="number" step="0.01"></label>
            <label>Hotel ($)<input name="hotel_amount" type="number" step="0.01"></label>
            <label>Rental ($)<input name="rentals_amount" type="number" step="0.01"></label>
            <label>Miscellaneous ($)<input name="miscellaneous_amount" type="number" step="0.01"></label>
            <label>Local<input name="local_number" value="3106"></label>
          </div>
          <label>Address<input name="address"></label>
          <section class="subpanel__PRESIDENT_PANEL_CLASS__" id="presidentDifferentialPanel">
            <div class="section-head"><div><p class="eyebrow">President Differential</p><h2>Scale 36 + 20%</h2></div></div>
            <div class="field-grid">
              <label>Differential Hours<input name="president_diff_hours" type="number" step="0.25" placeholder="defaults to hours"__PRESIDENT_DISABLED__></label>
            </div>
            <div class="muted">__PRESIDENT_HELP__</div>
          </section>
          <label>Notes<textarea name="notes"></textarea></label>
          <label class="certify-line"><input name="submitter_certified" type="checkbox" required> I certify this daily lost-wage and expense entry is accurate and I am signing off on it.</label>
          <input type="hidden" name="submitter_certification_text" value="I certify this daily lost-wage and expense entry is accurate and I am signing off on it.">
          <div class="toolbar"><button type="submit">Sign Off and Save Entry</button><span id="entryStatus" class="muted"></span></div>
        </form>
    """.replace("__PRESIDENT_PANEL_CLASS__", president_panel_class).replace(
        "__PRESIDENT_DISABLED__", president_disabled
    ).replace("__PRESIDENT_HELP__", president_help)


def _stub_form_html(*, title: str = "Commission Pay Proof") -> str:
    return f"""
    <section class="panel hidden" id="commissionProofPanel">
      <div class="section-head">
        <div><p class="eyebrow">{escape(title)}</p><h2>Last Month Payroll</h2></div>
        <div class="muted" id="commissionProofHelp"></div>
      </div>
      <form id="stubForm" class="form-stack">
        <div class="field-grid">
          <label>Member Email<input name="user_email" placeholder="leave blank for yourself"></label>
          <label>Payroll Month<input name="payroll_month" type="month"></label>
          <label>Base Wage Type<select name="base_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
          <label>Base Wage Amount ($)<input name="base_wage_amount" type="number" step="0.01"></label>
          <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label>Commission Month 1 ($)<input name="commission_month_1_amount" type="number" step="0.01"></label>
          <label>Commission Month 2 ($)<input name="commission_month_2_amount" type="number" step="0.01"></label>
          <label>Commission Month 3 ($)<input name="commission_month_3_amount" type="number" step="0.01"></label>
          <label>Pay Stub<input id="stubFile" type="file" accept=".pdf,image/png,image/jpeg"></label>
        </div>
        <label>Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit" class="secondary">Save Pay Proof</button><span id="stubStatus" class="muted"></span></div>
      </form>
      <div class="table-wrap compact-table">
        <table><thead><tr><th>Member</th><th>Payroll Month</th><th>Base</th><th>Commission Avg</th><th>Commission Hr</th><th>Total Hr</th><th>File</th></tr></thead><tbody id="stubsBody"></tbody></table>
      </div>
    </section>
    """


def _my_pay_profile_html() -> str:
    return """
    <section class="panel" id="myPayProfilePanel">
      <div class="section-head">
        <div><p class="eyebrow">My Pay Profile</p><h2>Lost Wage Rate</h2></div>
        <span id="myPayProfileStatus" class="muted"></span>
      </div>
      <form id="myPayProfileForm" class="form-stack">
        <div class="field-grid">
          <label>Email<input name="principal_email" readonly></label>
          <label>Name<input name="principal_display_name" readonly></label>
          <label>Pay Basis<select name="pay_basis">
            <option value="hourly">Hourly</option>
            <option value="weekly">Weekly / Salary</option>
            <option value="commission">Commission</option>
            <option value="president">President</option>
            <option value="expense_only">Expense Only</option>
          </select></label>
          <label data-basis-field="wage">Base Wage Type<select name="base_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
          <label data-basis-field="wage">Base Wage Amount ($)<input name="base_wage_amount" type="number" step="0.01"></label>
          <label data-basis-field="wage">Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label data-basis-field="commission">Commission Month 1 ($)<input name="commission_month_1_amount" type="number" step="0.01"></label>
          <label data-basis-field="commission">Commission Month 2 ($)<input name="commission_month_2_amount" type="number" step="0.01"></label>
          <label data-basis-field="commission">Commission Month 3 ($)<input name="commission_month_3_amount" type="number" step="0.01"></label>
        </div>
        <label>Default Voucher Address<input name="default_address"></label>
        <label>Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit" class="secondary">Save My Pay Profile</button><span id="myPayProfileSummary" class="muted"></span></div>
      </form>
    </section>
    """


def _mileage_tracker_form_html() -> str:
    return """
    <section class="panel mileage-tracker-panel">
      <div class="mileage-tracker">
        <div class="mileage-header">
          <div>
            <h2>Mileage Tracker</h2>
          </div>
          <img src="/static/email/cwa-logo.svg" alt="CWA logo" class="mileage-logo" onerror="this.style.display='none'">
        </div>
        <form id="mileageForm" class="mileage-form">
          <div class="mb-3">
            <label for="mileageEntrySelect" class="form-label">Attach To Pay Entry:</label>
            <select id="mileageEntrySelect" class="form-control">
              <option value="">Create a new mileage entry...</option>
            </select>
          </div>

          <input type="hidden" id="name" name="name">

          <div class="mb-3">
            <label for="local_number" class="form-label">Local Number:</label>
            <input type="text" class="form-control" id="local_number" name="local_number" value="3106">
            <div class="form-text">Enter your CWA local number, for example 3106.</div>
          </div>

          <div class="mb-3">
            <label for="date" class="form-label">Date:</label>
            <input type="date" class="form-control" id="date" name="date" required>
          </div>

          <div class="mb-3">
            <label for="description" class="form-label">Description:</label>
            <input type="text" class="form-control" id="description" name="description" required>
          </div>

          <div class="mb-3">
            <label class="form-label">Mileage Rate:</label>
            <div class="form-control" id="irsRateDisplay" aria-live="polite">Select a date</div>
          </div>

          <div class="mb-3">
            <label class="form-label" for="commonPlaceSelect">Common places:</label>
            <select id="commonPlaceSelect" class="form-control">
              <option value="">Select a place...</option>
            </select>
            <datalist id="commonPlaceOptions"></datalist>
            <button type="button" class="btn btn-outline-secondary w-100 mt-2" id="addCommonPlaceBtn">Add selected place to locations</button>
          </div>

          <div class="mb-3">
            <label class="form-label">Locations:</label>
            <div id="locations">
              <div class="input-group mb-2">
                <input type="text" class="form-control address-input" name="locations" placeholder="Origin" required autocomplete="off" list="commonPlaceOptions">
                <button class="btn btn-danger remove-location" type="button" aria-label="Remove location">&times;</button>
              </div>
              <div class="input-group mb-2">
                <input type="text" class="form-control address-input" name="locations" placeholder="Destination" required autocomplete="off" list="commonPlaceOptions">
                <button class="btn btn-danger remove-location" type="button" aria-label="Remove location">&times;</button>
              </div>
            </div>
            <button type="button" class="btn btn-secondary add-location w-100 mt-2">Add Location</button>
            <button type="button" class="btn btn-outline-secondary w-100 mt-2" id="checkMileageAddressesBtn">Check Addresses</button>
            <div class="form-text" id="addressCheckStatus">Use common places or check typed addresses before generating the mileage sheet.</div>
          </div>

          <div class="toolbar">
            <button type="submit" class="btn btn-primary w-100">Generate Mileage PDF</button>
            <span id="mileageStatus" class="muted"></span>
          </div>
        </form>
      </div>
    </section>
    <section class="panel">
      <div class="section-head"><div><p class="eyebrow">Mileage Forms</p><h2>Generated This Period</h2></div></div>
      <div class="table-wrap compact-table">
        <table><thead><tr><th></th><th>Date</th><th>Name</th><th>File</th><th>Scan</th><th>Miles</th><th>Rate</th><th>Reimbursement</th><th>Actions</th></tr></thead><tbody id="mileageFormsBody"></tbody></table>
      </div>
    </section>
    """


def _daily_tally_html() -> str:
    return """
    <section class="panel" id="dailyTallyPanel">
      <div class="section-head">
        <div><p class="eyebrow">Submitter</p><h2>Daily Tally</h2></div>
        <div id="dailyTallyStats" class="muted"></div>
      </div>
      <div class="table-wrap compact-table">
        <table><thead><tr><th>Date</th><th>Hours</th><th>Lost Wages</th><th>Mileage</th><th>Expenses</th><th>President Diff</th><th>Total</th></tr></thead><tbody id="dailyTallyBody"></tbody></table>
      </div>
    </section>
    """


def _entries_table_html(*, attach: bool, review: bool = False) -> str:
    attach_html = """
      <div class="toolbar">
        <input id="receiptFile" type="file" accept=".pdf,image/png,image/jpeg">
        <button id="uploadReceiptBtn" type="button" class="secondary">Attach Receipt To Selected</button>
      </div>
    """ if attach else ""
    review_header = "<th>Review</th>" if review else ""
    return f"""
    <section class="panel">
      <div class="section-head">
        <div><p class="eyebrow">Current Period</p><h2>Entries</h2></div>
        <div id="periodStats" class="muted"></div>
      </div>
      {attach_html}
      <div class="table-wrap">
        <table data-review="{str(review).lower()}"><thead><tr><th></th><th>Date</th><th>Name</th><th>Hours</th><th>Lost Wages</th><th>Mileage</th><th>Other</th><th>President Diff</th>{review_header}<th>Sign-Off</th><th>Notes</th><th>Actions</th></tr></thead><tbody id="entriesBody"></tbody></table>
      </div>
    </section>
    """


def _pay_view_content(view: str, *, actor: PayActor) -> str:
    if view == "mileage":
        return f"""
        <section class="panel lead-panel">
          <div><p class="eyebrow">Mileage</p><h2>Route Form and PDF</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="actorLabel"></span><strong>Signed in</strong></div></div>
        </section>
        {_mileage_tracker_form_html()}
        {_entries_table_html(attach=True)}
        """
    if view == "demo":
        feedback_table = (
            """
        <section class="panel" id="demoFeedbackPanel">
          <div class="section-head"><div><p class="eyebrow">Officer Feedback</p><h2>Suggestions</h2></div></div>
          <div class="table-wrap compact-table">
            <table><thead><tr><th>Date</th><th>Officer</th><th>Area</th><th>Type</th><th>Suggestion</th><th>Status</th><th></th></tr></thead><tbody id="demoFeedbackBody"></tbody></table>
          </div>
        </section>
            """
            if actor.can_lock
            else ""
        )
        return f"""
        <section class="panel lead-panel">
          <div><p class="eyebrow">Training</p><h2>Demo Cycle</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Live Period</strong></div><div><span id="demoModeLabel"></span><strong>Demo Mode</strong></div></div>
        </section>
        <section class="panel hidden" id="demoDisabledPanel">
          <div class="section-head"><div><p class="eyebrow">Demo</p><h2>Disabled</h2></div></div>
          <div class="muted">A treasurer can enable the training demo from Admin.</div>
        </section>
        <section class="panel" id="demoCyclePanel">
          <div class="section-head">
            <div><p class="eyebrow">Practice Cycle</p><h2 id="demoTitle">Training Demo Cycle</h2></div>
            <span id="demoCycleStatus" class="muted"></span>
          </div>
          <div id="demoNotes" class="muted"></div>
          <div class="demo-two-col">
            <div>
              <h3>Training Checklist</h3>
              <ul id="demoChecklist" class="demo-list"></ul>
            </div>
            <div>
              <h3>Processing Log</h3>
              <ol id="demoActivityLog" class="demo-list"></ol>
            </div>
          </div>
          <div class="toolbar demo-step-row">
            <button type="button" data-demo-step="0" class="secondary">Open Period</button>
            <button type="button" data-demo-step="1" class="secondary">Submit Entry</button>
            <button type="button" data-demo-step="2" class="secondary">Add Mileage</button>
            <button type="button" data-demo-step="3" class="secondary">Treasurer Review</button>
            <button type="button" data-demo-step="4" class="secondary">Demo Lock</button>
          </div>
          <div class="metric-row demo-metrics">
            <div><span id="demoEntryCount"></span><strong>Entries</strong></div>
            <div><span id="demoLostWages"></span><strong>Lost Wages</strong></div>
            <div><span id="demoMileageTotal"></span><strong>Mileage</strong></div>
            <div><span id="demoPresidentDiffTotal"></span><strong>President Diff</strong></div>
            <div><span id="demoPacketStatus"></span><strong>Packet</strong></div>
          </div>
          <div class="table-wrap">
            <table><thead><tr><th>Date</th><th>Name</th><th>Hours</th><th>Lost Wages</th><th>Mileage</th><th>Other</th><th>President Diff</th><th>Sign-Off</th><th>Notes</th></tr></thead><tbody id="demoEntriesBody"></tbody></table>
          </div>
        </section>
        <section class="panel" id="demoFilesPanel">
          <div class="section-head"><div><p class="eyebrow">Demo Output</p><h2>Combined PDF Packet</h2></div><span id="demoFilesStatus" class="muted"></span></div>
          <div class="toolbar"><button type="button" id="generateDemoFilesBtn" class="secondary">Generate Demo Packet</button></div>
          <div class="table-wrap compact-table">
            <table><thead><tr><th>File</th><th>Size</th><th>Updated</th><th></th></tr></thead><tbody id="demoFilesBody"></tbody></table>
          </div>
        </section>
        <section class="panel" id="demoFeedbackFormPanel">
          <div class="section-head"><div><p class="eyebrow">Feedback</p><h2>Suggestions From Demo</h2></div><span id="demoFeedbackStatus" class="muted"></span></div>
          <form id="demoFeedbackForm" class="form-stack">
            <div class="field-grid">
              <label>Area<select name="screen">
                <option value="demo">Overall Demo</option>
                <option value="entry">Entry Form</option>
                <option value="mileage">Mileage</option>
                <option value="treasurer">Treasurer Review</option>
                <option value="admin">Admin Setup</option>
              </select></label>
              <label>Type<select name="category">
                <option value="suggestion">Suggestion</option>
                <option value="confusing">Confusing</option>
                <option value="missing">Missing</option>
                <option value="bug">Bug</option>
                <option value="training">Training Need</option>
              </select></label>
            </div>
            <label>Suggestion<textarea name="comment" maxlength="4000" required></textarea></label>
            <div class="toolbar"><button type="submit" class="secondary">Send Suggestion</button><button type="button" id="resetDemoBtn" class="secondary">Reset Demo</button></div>
          </form>
        </section>
        {feedback_table}
        """
    if view == "president":
        return f"""
        <section class="panel lead-panel">
          <div><p class="eyebrow">President</p><h2>Differential and Lost Time</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="actorLabel"></span><strong>Signed in</strong></div></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Daily Input</p><h2>President Entry</h2></div><div class="muted">Scale 36 + 20% target</div></div>
          {_entry_form_html(president=True, advanced=actor.can_lock)}
        </section>
        {_entries_table_html(attach=True)}
        """
    if view == "treasurer":
        packet_controls = (
            """
          <div class="toolbar">
            <button id="lockBtn" type="button">Lock And Send For Signature</button>
            <button id="revisionBtn" type="button" class="secondary">Create Revision</button>
          </div>
            """
            if actor.can_lock
            else '<div class="muted">Read-only review access.</div>'
        )
        correction_panel = """
        <section class="panel hidden" id="correctionPanel">
          <div class="section-head">
            <div><p class="eyebrow">Treasurer Correction</p><h2 id="correctionTitle">Edit Voucher Additions</h2></div>
            <span id="correctionStatus" class="muted">Choose Edit Voucher on a row below.</span>
          </div>
          <form id="correctionForm" class="form-stack">
            <div class="field-grid">
              <label>Member Email<input name="user_email" required></label>
              <label>Name<input name="display_name"></label>
              <label>Date<input name="entry_date" type="date" required></label>
              <label>Local<input name="local_number" value="3106"></label>
              <label>Hours<input name="hours" type="number" step="0.25" min="0"></label>
              <label>Miles<input name="mileage_miles" type="number" step="0.01" min="0"></label>
              <label>Mileage Rate ($/mi)<input name="mileage_rate" type="number" step="0.001" min="0"></label>
              <label>Mileage Amount ($)<input name="mileage_amount" type="number" step="0.01" min="0"></label>
              <label>Meals ($)<input name="meals_amount" type="number" step="0.01" min="0"></label>
              <label>Hotel ($)<input name="hotel_amount" type="number" step="0.01" min="0"></label>
              <label>Rentals ($)<input name="rentals_amount" type="number" step="0.01" min="0"></label>
              <label>Misc. ($)<input name="miscellaneous_amount" type="number" step="0.01" min="0"></label>
            </div>
            <label>Address<input name="address"></label>
            <label>Correction Note<textarea name="notes" required></textarea></label>
            <div class="toolbar"><button type="submit" class="secondary">Add Correction</button><button type="button" id="cancelCorrectionBtn" class="secondary">Close</button></div>
          </form>
        </section>
        """ if actor.can_lock else ""
        return f"""
        <section class="panel lead-panel">
          <div><p class="eyebrow">Treasurer</p><h2>Review, Lock, and Send</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="periodStats"></span><strong>Totals</strong></div></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Packet Control</p><h2>Voucher Packet</h2></div><span id="treasurerStatus" class="muted"></span></div>
          {packet_controls}
        </section>
        {correction_panel}
        {_entries_table_html(attach=False, review=True)}
        """
    if view == "admin":
        return """
        <section class="panel lead-panel">
          <div><p class="eyebrow">Admin</p><h2>Portal Settings</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Current Period</strong></div><div><span id="actorLabel"></span><strong>Access</strong></div></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Routing</p><h2>President and Treasurer</h2></div><span id="settingsStatus" class="muted"></span></div>
          <form id="settingsForm" class="form-stack">
            <div class="field-grid">
              <label>President Email<input name="president_email"></label>
              <label>Treasurer Emails<input name="treasurer_emails"></label>
              <label>Scale Effective Date<input name="effective_date" type="date"></label>
              <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
              <label>Scale 36 Weekly Base ($)<input name="target_weekly_amount" type="number" step="0.01"></label>
              <label>President Target<input value="Scale 36 + 20%" disabled></label>
            </div>
            <div class="toolbar"><button type="submit" class="secondary">Save Settings</button></div>
          </form>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">SIF / Growth Funds</p><h2>Fund Ledger and Packets</h2></div><span id="fundStatus" class="muted"></span></div>
          <form id="fundForm" class="form-stack">
            <div class="field-grid">
              <label>Fund Name<input name="name" required placeholder="COJ SIF"></label>
              <label>Fund Type<select name="fund_type"><option value="sif">SIF</option><option value="growth">Growth Fund</option></select></label>
              <label>Status<select name="status"><option value="active">Active</option><option value="closed">Closed</option></select></label>
              <label>Local<input name="local_number" value="3106"></label>
            </div>
            <label>Description<textarea name="description"></textarea></label>
            <div class="toolbar"><button type="submit" class="secondary">Save Fund</button></div>
          </form>
          <form id="fundLedgerForm" class="form-stack">
            <div class="field-grid">
              <label>Fund<select id="fundLedgerFundSelect" name="fund_id"></select></label>
              <label>Type<select name="ledger_type"><option value="advance">Advance</option><option value="reimbursement_submitted">Reimbursement Submitted</option><option value="reimbursement_received">Reimbursement Received</option><option value="adjustment">Adjustment</option></select></label>
              <label>Amount ($)<input name="amount" type="number" step="0.01" required></label>
              <label>Date<input name="effective_date" type="date" required></label>
              <label>Reference<input name="reference"></label>
            </div>
            <label>Notes<textarea name="notes"></textarea></label>
            <div class="toolbar"><button type="submit" class="secondary">Add Ledger Entry</button></div>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Fund</th><th>Type</th><th>Status</th><th>Advance</th><th>Allocated</th><th>Submitted</th><th>Needed</th><th>Remaining</th></tr></thead><tbody id="fundBalancesBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">SIF / Growth Funds</p><h2>Excel Packet Generator</h2></div><span id="fundPacketStatus" class="muted"></span></div>
          <form id="fundPacketForm" class="inline-form">
            <select id="fundPacketFundSelect" name="fund_id"></select>
            <input name="period_start" type="date" required>
            <input name="period_end" type="date" required>
            <button type="submit" class="secondary">Generate Excel Packet</button>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Fund</th><th>Dates</th><th>Total</th><th>Ending</th><th></th></tr></thead><tbody id="fundPacketsBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Training</p><h2>Demo Mode</h2></div><span id="demoSettingsStatus" class="muted"></span></div>
          <form id="demoSettingsForm" class="form-stack">
            <div class="field-grid">
              <label>Status<select name="demo_mode_enabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></label>
              <label>Cycle Title<input name="demo_cycle_title" placeholder="Training Demo Cycle"></label>
            </div>
            <label>Demo Notes<textarea name="demo_cycle_notes"></textarea></label>
            <div class="toolbar"><button type="submit" class="secondary">Save Demo Mode</button><a href="/pay/demo">Open Demo</a></div>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Date</th><th>Officer</th><th>Area</th><th>Type</th><th>Suggestion</th><th>Status</th><th></th></tr></thead><tbody id="demoFeedbackBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Internal Access</p><h2>Microsoft People</h2></div><span id="internalRoleStatus" class="muted"></span></div>
          <div class="subpanel form-stack">
            <div>
              <h3>Automatic Internal Roster</h3>
              <p class="muted">Import every active Microsoft account with an assigned paid license as an expense-only pay profile. Existing wage profiles are left unchanged.</p>
            </div>
            <div class="toolbar"><button id="importLicensedUsersBtn" type="button" class="secondary">Import Microsoft Paid Users</button></div>
          </div>
          <form id="internalRoleSearchForm" class="inline-form">
            <input name="search" placeholder="Find president or treasurer by name or email" required>
            <button type="submit" class="secondary">Search For Role</button>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Name</th><th>Email</th><th>Source</th><th></th></tr></thead><tbody id="internalRoleSearchBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">People</p><h2>People and Pay Profiles</h2></div><span id="payProfileStatus" class="muted"></span></div>
          <form id="payProfileForm" class="form-stack">
            <input name="principal_id" type="hidden">
            <div class="field-grid">
              <label>Email<input name="principal_email" required></label>
              <label>Name<input name="principal_display_name"></label>
              <label>Pay Basis<select name="pay_basis">
                <option value="hourly">Hourly</option>
                <option value="weekly">Weekly / Salary</option>
                <option value="commission">Commission</option>
                <option value="president">President</option>
                <option value="expense_only">Expense Only</option>
              </select></label>
              <label>Base Wage Type<select name="base_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
              <label>Base Wage Amount ($)<input name="base_wage_amount" type="number" step="0.01"></label>
              <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
              <label>Commission Month 1 ($)<input name="commission_month_1_amount" type="number" step="0.01"></label>
              <label>Commission Month 2 ($)<input name="commission_month_2_amount" type="number" step="0.01"></label>
              <label>Commission Month 3 ($)<input name="commission_month_3_amount" type="number" step="0.01"></label>
              <label>Status<select name="status"><option value="active">Active</option><option value="disabled">Disabled</option></select></label>
            </div>
            <label>Default Voucher Address<input name="default_address"></label>
            <label>Notes<textarea name="notes"></textarea></label>
            <div class="toolbar"><button type="submit" class="secondary">Save Pay Profile</button></div>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Source</th><th>Name</th><th>Email</th><th>Pay Basis</th><th>Base</th><th>Commission Hr</th><th>Total Hr</th><th>Status</th><th></th></tr></thead><tbody id="payProfilesBody"></tbody></table></div>
          <div class="subpanel form-stack">
            <div><h3>Pending Wage Changes</h3><p class="muted">Submitter wage changes do not affect reimbursements until treasurer/admin approval.</p></div>
            <div class="table-wrap compact-table"><table><thead><tr><th>Requested</th><th>Name</th><th>Email</th><th>Basis</th><th>Base</th><th>Total Hr</th><th>Note</th><th></th></tr></thead><tbody id="profileChangeRequestsBody"></tbody></table></div>
          </div>
          <div class="table-wrap compact-table"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody id="internalRolesBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">External Access</p><h2>Pay User Allowlist</h2></div><span id="payUserStatus" class="muted"></span></div>
          <form id="payUserForm" class="inline-form">
            <input name="email" placeholder="pay.user@example.org" required>
            <input name="display_name" placeholder="Display name">
            <select name="role"><option value="guest">Guest</option><option value="treasurer">Treasurer</option></select>
            <select name="status"><option value="active">Active</option><option value="disabled">Disabled</option></select>
            <button type="submit" class="secondary">Save User</button>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Status</th></tr></thead><tbody id="payUsersBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Mileage</p><h2>IRS Rate Updates</h2></div><span id="irsStatus" class="muted"></span></div>
          <div class="toolbar"><button id="irsSyncBtn" type="button" class="secondary">Check IRS Rates</button></div>
          <div class="table-wrap compact-table"><table><thead><tr><th>Year</th><th>Rate</th><th>Source</th><th>Status</th><th></th></tr></thead><tbody id="irsCandidatesBody"></tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Mileage</p><h2>Locations</h2></div><span id="commonPlaceStatus" class="muted"></span></div>
          <form id="commonPlaceForm" class="inline-form">
            <input name="label" placeholder="Location label" required>
            <input name="address" placeholder="Full address" required>
            <button type="submit" class="secondary">Add Location</button>
          </form>
          <div class="table-wrap compact-table"><table><thead><tr><th>Label</th><th>Address</th><th></th></tr></thead><tbody id="commonPlacesBody"></tbody></table></div>
        </section>
        """
    return f"""
    <section class="panel lead-panel">
      <div><p class="eyebrow">Submit</p><h2>Lost Wage Entry</h2></div>
      <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="actorLabel"></span><strong>Signed in</strong></div></div>
    </section>
    <section class="panel">
      <div class="section-head"><div><p class="eyebrow">Daily Input</p><h2>Lost Time and Expenses</h2></div><span id="entryStatus" class="muted"></span></div>
      {_entry_form_html(president_enabled=actor.is_president)}
    </section>
    {_my_pay_profile_html()}
    {_stub_form_html()}
    <section class="panel" id="fundAllocationPanel">
      <div class="section-head"><div><p class="eyebrow">SIF / Growth Funds</p><h2>Allocate This Entry</h2></div><span id="fundAllocationStatus" class="muted"></span></div>
      <p class="muted" id="fundAllocationEntryLabel">Select an entry below</p>
      <form id="fundAllocationForm" class="form-stack">
        <div class="field-grid">
          <label>Fund<select name="fund_id"></select></label>
          <label>Hours<input name="hours" type="number" step="0.25" min="0"></label>
          <label>Miles<input name="mileage_miles" type="number" step="0.01" min="0"></label>
          <label>Mileage Amount ($)<input name="mileage_amount" type="number" step="0.01" min="0"></label>
          <label>Meals ($)<input name="meals_amount" type="number" step="0.01" min="0"></label>
          <label>Hotel ($)<input name="hotel_amount" type="number" step="0.01" min="0"></label>
          <label>Rental ($)<input name="rentals_amount" type="number" step="0.01" min="0"></label>
          <label>Misc. ($)<input name="miscellaneous_amount" type="number" step="0.01" min="0"></label>
        </div>
        <label>Allocation Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit" class="secondary">Save Fund Allocation</button></div>
      </form>
      <div class="subpanel form-stack">
        <div><h3>Optional Timesheet Screenshot</h3><p class="muted">PDF, PNG, or JPEG. This is stored with the selected entry and included with fund packets for that day.</p></div>
        <div class="toolbar"><input id="timesheetFile" type="file" accept=".pdf,image/png,image/jpeg"><button id="uploadTimesheetBtn" type="button" class="secondary">Attach Timesheet</button></div>
      </div>
      <div class="table-wrap compact-table"><table><thead><tr><th>Fund</th><th>Hours</th><th>Miles</th><th>Mileage</th><th>Meals</th><th>Hotel</th><th>Rental</th><th>Misc.</th><th></th></tr></thead><tbody id="fundAllocationsBody"></tbody></table></div>
    </section>
    {_entries_table_html(attach=True)}
    {_daily_tally_html()}
    """


def _render_pay_workspace_page(*, view: str, actor: PayActor) -> str:
    normalized_view = view if view in _PAY_VIEW_TITLES else "entry"
    page_title = _PAY_VIEW_TITLES[normalized_view]
    actor_name = escape(actor.display_name or actor.email or "Pay user")
    role_label = escape(actor.role.title())
    nav_html = _pay_nav_html(view=normalized_view, actor=actor)
    content_html = _pay_view_content(normalized_view, actor=actor)
    html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lost Wage Portal - __PAGE_TITLE__</title>
  <style>
    :root { color-scheme: light; --bg:#f4f6f8; --panel:#fff; --text:#1f2933; --muted:#5b6776; --line:#d8e0e7; --accent:#155e75; --accent-dark:#10495b; --soft:#eef6f8; --danger:#8a1f1f; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:Arial, sans-serif; }
    a { color:var(--accent); }
    .app-shell { min-height:100vh; display:grid; grid-template-columns:240px minmax(0, 1fr); }
    .side-nav { background:#fff; border-right:1px solid var(--line); padding:18px 14px; position:sticky; top:0; height:100vh; display:flex; flex-direction:column; gap:16px; }
    .brand { display:grid; gap:4px; padding:4px 4px 12px; border-bottom:1px solid var(--line); }
    .brand-title { font-size:19px; font-weight:800; letter-spacing:0; }
    .brand-subtitle { color:var(--muted); font-size:13px; }
    .nav-links { display:grid; gap:6px; }
    .nav-link { display:flex; align-items:center; min-height:38px; padding:8px 10px; border:1px solid transparent; border-radius:4px; text-decoration:none; color:var(--text); font-weight:700; }
    .nav-link:hover, .nav-link:focus { border-color:#c6d5dd; background:#f8fbfc; }
    .nav-link[aria-current="page"] { background:var(--accent); color:#fff; border-color:var(--accent); }
    .side-footer { margin-top:auto; display:grid; gap:4px; color:var(--muted); font-size:13px; padding:10px 4px 0; border-top:1px solid var(--line); }
    .main { min-width:0; display:grid; gap:14px; padding:18px; align-content:start; }
    .topbar { display:flex; justify-content:space-between; gap:12px; align-items:center; background:#fff; border:1px solid var(--line); border-radius:6px; padding:14px 16px; }
    .period-picker { min-width:260px; max-width:360px; }
    h1, h2 { margin:0; letter-spacing:0; }
    h1 { font-size:24px; }
    h2 { font-size:18px; }
    h3 { margin:0 0 8px; font-size:15px; letter-spacing:0; }
    .eyebrow { margin:0 0 4px; color:var(--accent); font-size:12px; font-weight:800; text-transform:uppercase; }
    .muted { color:var(--muted); font-size:13px; }
    .hidden { display:none !important; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:16px; min-width:0; }
    .subpanel { border:1px solid var(--line); border-radius:4px; padding:12px; background:#f8fafb; }
    .disabled-panel { opacity:.58; background:#f3f5f7; }
    .disabled-panel input, .disabled-panel select, .disabled-panel textarea { background:#eef2f5; color:#697586; }
    .lead-panel { display:flex; justify-content:space-between; align-items:center; gap:16px; background:var(--soft); }
    .metric-row { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .metric-row div { min-width:150px; background:#fff; border:1px solid var(--line); border-radius:4px; padding:9px 10px; display:grid; gap:3px; }
    .metric-row span { font-size:14px; font-weight:800; }
    .metric-row strong { color:var(--muted); font-size:12px; }
    .section-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:12px; }
    .form-stack { display:grid; gap:12px; }
    .field-grid { display:grid; grid-template-columns:repeat(4, minmax(130px, 1fr)); gap:10px; }
    label { display:grid; gap:4px; font-size:13px; font-weight:700; }
    input, select, textarea, button { font:inherit; }
    input, select, textarea { width:100%; min-width:0; border:1px solid #b8c4cf; border-radius:4px; padding:8px; background:#fff; }
    textarea { min-height:70px; resize:vertical; }
    button { border:1px solid var(--accent); border-radius:4px; background:var(--accent); color:#fff; padding:8px 12px; cursor:pointer; font-weight:800; }
    button.secondary { background:#fff; color:var(--accent); }
    button.danger { background:var(--danger); border-color:var(--danger); }
    .toolbar, .inline-form { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .inline-form input, .inline-form select { width:auto; min-width:180px; }
    .demo-step-row { margin:14px 0; }
    .demo-step-row button[aria-current="step"] { background:var(--accent); color:#fff; }
    .demo-metrics { justify-content:flex-start; margin:0 0 12px; }
    .demo-two-col { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; margin:12px 0; }
    .demo-two-col > div { border:1px solid var(--line); border-radius:4px; padding:12px; background:#fff; }
    .demo-list { margin:0; padding-left:20px; color:var(--muted); font-size:13px; line-height:1.45; }
    .mileage-tracker { font-size:18px; }
    .mileage-header { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }
    .mileage-header h2 { font-size:45px; line-height:1.05; }
    .mileage-logo { width:100px; height:auto; }
    .mileage-form { display:block; }
    .mileage-form .mb-3, .mileage-form .mb-2 { margin-bottom:24px; }
    .mileage-form label { font-size:18px; font-weight:700; }
    .mileage-form .form-control { min-height:50px; font-size:18px; width:100%; }
    .mileage-form .form-text { margin-top:4px; color:var(--muted); font-size:14px; line-height:1.4; }
    .mileage-form .input-group { display:flex; gap:8px; align-items:stretch; }
    .mileage-form .input-group .form-control { flex:1 1 auto; }
    .mileage-form .btn { min-width:60px; min-height:60px; padding:16px 24px; font-size:19px; }
    .mileage-form .btn-secondary, .mileage-form .btn-outline-secondary { background:#fff; color:var(--accent); }
    .mileage-form .btn-danger { background:var(--danger); border-color:var(--danger); color:#fff; }
    .mileage-form .w-100 { width:100%; }
    .mileage-form .mt-2 { margin-top:8px; }
    .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:4px; }
    table { width:100%; border-collapse:collapse; min-width:900px; background:#fff; }
    .compact-table table { min-width:680px; }
    th, td { text-align:left; padding:8px; border-bottom:1px solid #e6ebf0; font-size:13px; vertical-align:top; }
    .compact-table td button + button { margin-left:6px; }
    th { background:#f8fafb; font-weight:800; color:#384656; }
    tr:last-child td { border-bottom:0; }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns:1fr; }
      .side-nav { position:relative; height:auto; border-right:0; border-bottom:1px solid var(--line); }
      .nav-links { grid-template-columns:repeat(2, minmax(0, 1fr)); }
      .lead-panel, .topbar { align-items:flex-start; flex-direction:column; }
      .metric-row { justify-content:flex-start; }
      .field-grid { grid-template-columns:1fr; }
      .demo-two-col { grid-template-columns:1fr; }
      .inline-form { display:grid; }
      .inline-form input, .inline-form select, button { width:100%; }
      .mileage-header h2 { font-size:36px; }
      .mileage-form .input-group { gap:6px; }
      .mileage-form .remove-location { width:auto; flex:0 0 60px; padding:10px; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="side-nav">
      <div class="brand"><div class="brand-title">Lost Wage Portal</div><div class="brand-subtitle">CWA Local 3106</div></div>
      <nav class="nav-links" aria-label="Pay Portal navigation">__NAV__</nav>
      <div class="side-footer"><strong>__ACTOR_NAME__</strong><span>__ROLE_LABEL__</span><a href="/officers">Main tracker</a></div>
    </aside>
    <main class="main">
      <header class="topbar"><div><p class="eyebrow">Pay Portal</p><h1>__PAGE_TITLE__</h1></div><label class="period-picker">Pay Period<select id="periodSelect"></select></label></header>
      __CONTENT__
    </main>
  </div>
  <script>
const PAY_VIEW = "__VIEW__";
let context = null;
let selectedEntryId = null;
let selectedPeriodId = (() => { try { return localStorage.getItem('paySelectedPeriodId') || ''; } catch (_err) { return ''; } })();
let internalRoleSearchRows = [];
let commonPlaceLookup = new Map();
let commonPlacesDraft = [];
const byId = id => document.getElementById(id);
function money(value) { return Number(value || 0).toFixed(2); }
function currency(value) { return `$${money(value)}`; }
function rateCurrency(value, digits = 3) { return `$${Number(value || 0).toFixed(digits)}`; }
function addCell(row, value) { const cell = document.createElement('td'); cell.textContent = value == null ? '' : String(value); row.appendChild(cell); }
function setText(id, value) { const node = byId(id); if (node) node.textContent = value || ''; }
function bind(id, event, handler) { const node = byId(id); if (node) node.addEventListener(event, handler); }
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return await res.json();
}
function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}
function renderPeriodSelect() {
  const select = byId('periodSelect');
  if (!select || !context) return;
  const periods = context.periods || [];
  select.innerHTML = '';
  for (const period of periods) {
    const option = document.createElement('option');
    option.value = period.id;
    option.textContent = `${period.period_start} to ${period.period_end} - ${period.status} (${period.entry_count || 0})`;
    if (period.id === context.period.id) option.selected = true;
    select.appendChild(option);
  }
  select.disabled = periods.length <= 1;
}
function renderSummary() {
  if (!context) return;
  renderPeriodSelect();
  const entries = context.entries || [];
  const lost = entries.reduce((sum, row) => sum + Number(row.lost_wage_hourly_rate || row.hourly_rate || 0) * Number(row.hours || 0), 0);
  const mileage = entries.reduce((sum, row) => sum + Number(row.mileage_amount || 0), 0);
  const other = entries.reduce((sum, row) => sum + Number(row.rentals_amount || 0) + Number(row.meals_amount || 0) + Number(row.hotel_amount || 0) + Number(row.miscellaneous_amount || 0), 0);
  setText('periodLabel', `${context.period.period_start} to ${context.period.period_end} - ${context.period.status}`);
  setText('actorLabel', context.actor.display_name || context.actor.email || '');
  setText('periodStats', `${entries.length} entries | ${currency(lost + mileage + other)}`);
}
function readDemoStepIndex() {
  try {
    const value = Number(localStorage.getItem('payDemoStepIndex') || 0);
    return Number.isFinite(value) ? value : 0;
  } catch (_err) {
    return 0;
  }
}
function writeDemoStepIndex(value) {
  try { localStorage.setItem('payDemoStepIndex', String(value)); } catch (_err) {}
}
let demoStepIndex = readDemoStepIndex();
const DEMO_STEPS = [
  {
    label: 'Open Period',
    status: 'Period open for officer practice',
    checklist: ['Confirm payroll period dates', 'Confirm officer is signed in', 'Review where suggestions are submitted'],
    log: ['Demo period opened', 'No production entry has been created'],
  },
  {
    label: 'Submit Entry',
    status: 'Daily entry signed off in demo',
    checklist: ['Enter date and lost-time hours', 'Confirm pay profile drives the rate', 'Check the daily sign-off before saving'],
    log: ['Demo officer entry validated', 'Lost wages calculated from the saved profile snapshot', 'Submitter signed off electronically in Pay Portal'],
  },
  {
    label: 'Add Mileage',
    status: 'Mileage attached in demo',
    checklist: ['Select common locations', 'Confirm IRS rate follows the entry date', 'Attach mileage to the same entry'],
    log: ['Common place addresses resolved', 'Mileage reimbursement calculated', 'Mileage PDF marked as attached in demo'],
  },
  {
    label: 'Treasurer Review',
    status: 'Treasurer review ready in demo',
    checklist: ['Review officer entry totals', 'Review president differential example', 'Check receipts and mileage attachments'],
    log: ['Second demo entry added for president differential', 'Treasurer review totals refreshed', 'Feedback table available to admins'],
  },
  {
    label: 'Demo Lock',
    status: 'Demo packet locked without sending',
    checklist: ['Confirm each person has one voucher', 'Confirm each person support support PDFs stay behind their voucher', 'Confirm only the president signs the packet'],
    log: ['Demo packet grouped by person with supporting documents behind each voucher', 'Daily submitter sign-offs stay in Pay Portal audit fields', 'Email, DocuSeal, SharePoint, and real pay entries were not touched'],
  },
];
function addDaysText(dateText, days) {
  const date = new Date(`${dateText || '2026-05-03'}T12:00:00`);
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}
function demoRowsForStep() {
  if (!context || demoStepIndex < 1) return [];
  const start = context.period && context.period.period_start;
  const samples = [
    {
      offset: 1,
      entry_date: addDaysText(start, 1),
      display_name: 'Nick Craig',
      hours: 4,
      rate: 250,
      mileage: 24.65,
      other: 0,
      signed_off: true,
      notes: 'DEMO TRAINING - reviewed route mileage, receipts, and pay profile rate for officer practice.',
    },
    {
      offset: 3,
      entry_date: addDaysText(start, 3),
      display_name: 'Demo President',
      hours: 5,
      rate: 62,
      normal_rate: 45,
      mileage: 0,
      other: 18.75,
      president_diff_hours: 3,
      president_diff_rate: 17,
      signed_off: true,
      notes: 'DEMO TRAINING - president worked 5 union hours at $62.00/hr and 3 scheduled employer hours at the $17.00/hr differential.',
    },
    {
      offset: 6,
      entry_date: addDaysText(start, 6),
      display_name: 'Demo Treasurer',
      hours: 3,
      rate: 46.25,
      mileage: 18.5,
      other: 24,
      signed_off: true,
      notes: 'DEMO TRAINING - reconciled mileage attachment with daily expense voucher totals.',
    },
    {
      offset: 9,
      entry_date: addDaysText(start, 9),
      display_name: 'Demo Steward',
      hours: 6,
      rate: 42.25,
      mileage: 31.75,
      other: 12.5,
      signed_off: true,
      notes: 'DEMO TRAINING - met with member about payroll correction and documented next steps.',
    },
  ];
  const limit = demoStepIndex >= 4 ? samples.length : demoStepIndex >= 3 ? 3 : demoStepIndex >= 2 ? 2 : 1;
  return samples.slice(0, limit).map(row => ({
    ...row,
    lost_wages: row.hours * row.rate,
    mileage: demoStepIndex >= 2 ? row.mileage : 0,
    other: demoStepIndex >= 3 ? row.other : 0,
    president_diff_hours: demoStepIndex >= 3 ? Number(row.president_diff_hours || 0) : 0,
    president_diff_rate: demoStepIndex >= 3 ? Number(row.president_diff_rate || 0) : 0,
    president_diff_amount: demoStepIndex >= 3 ? Number(row.president_diff_hours || 0) * Number(row.president_diff_rate || 0) : 0,
    signed_off: demoStepIndex >= 1 && !!row.signed_off,
  }));
}
function demoModeEnabled() {
  if (!context) return true;
  const settings = context.demo_settings || context.settings || {};
  const value = settings.demo_mode_enabled;
  return !(value === false || String(value).toLowerCase() === 'false');
}
function renderDemoList(id, items) {
  const node = byId(id);
  if (!node) return;
  node.innerHTML = '';
  for (const item of items || []) {
    const li = document.createElement('li');
    li.textContent = item;
    node.appendChild(li);
  }
}
function renderDemoCycle() {
  const panel = byId('demoCyclePanel');
  if (!panel || !context) return;
  const enabled = demoModeEnabled();
  const disabledPanel = byId('demoDisabledPanel');
  const feedbackPanel = byId('demoFeedbackFormPanel');
  if (disabledPanel) disabledPanel.classList.toggle('hidden', enabled);
  if (feedbackPanel) feedbackPanel.classList.toggle('hidden', !enabled);
  panel.classList.toggle('hidden', !enabled);
  setText('demoModeLabel', enabled ? 'Enabled' : 'Disabled');
  if (!enabled) return;
  if (!Number.isFinite(demoStepIndex) || demoStepIndex < 0 || demoStepIndex >= DEMO_STEPS.length) demoStepIndex = 0;
  const step = DEMO_STEPS[demoStepIndex];
  const demoSettings = context.demo_settings || context.settings || {};
  setText('demoTitle', demoSettings.demo_cycle_title || 'Training Demo Cycle');
  setText('demoNotes', demoSettings.demo_cycle_notes || 'Practice data only. The demo shows daily submitter sign-off and president-only packet signing without touching live services.');
  setText('demoCycleStatus', step.status);
  renderDemoList('demoChecklist', step.checklist);
  renderDemoList('demoActivityLog', step.log);
  document.querySelectorAll('[data-demo-step]').forEach(button => {
    const isCurrent = Number(button.dataset.demoStep) === demoStepIndex;
    button.classList.toggle('secondary', !isCurrent);
    if (isCurrent) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  const rows = demoRowsForStep();
  setText('demoEntryCount', String(rows.length));
  setText('demoLostWages', currency(rows.reduce((sum, row) => sum + row.lost_wages, 0)));
  setText('demoMileageTotal', currency(rows.reduce((sum, row) => sum + row.mileage, 0)));
  setText('demoPresidentDiffTotal', currency(rows.reduce((sum, row) => sum + row.president_diff_amount, 0)));
  setText('demoPacketStatus', demoStepIndex >= 4 ? 'Locked' : demoStepIndex >= 3 ? 'Review' : 'Open');
  const body = byId('demoEntriesBody');
  if (!body) return;
  body.innerHTML = '';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.textContent = 'No demo entries yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.entry_date);
    addCell(tr, row.display_name);
    addCell(tr, money(row.hours));
    addCell(tr, currency(row.lost_wages));
    addCell(tr, currency(row.mileage));
    addCell(tr, currency(row.other));
    addCell(tr, row.president_diff_amount ? `${money(row.president_diff_hours)} hrs / ${currency(row.president_diff_amount)}` : '');
    addCell(tr, row.signed_off ? 'Signed off in Pay Portal' : 'Not signed off');
    addCell(tr, row.notes);
    body.appendChild(tr);
  }
}
function setDemoStep(index) {
  const parsed = Number(index || 0);
  demoStepIndex = Number.isFinite(parsed) ? Math.max(0, Math.min(parsed, DEMO_STEPS.length - 1)) : 0;
  writeDemoStepIndex(demoStepIndex);
  renderDemoCycle();
}
function renderDemoFeedback() {
  const body = byId('demoFeedbackBody');
  if (!body || !context) return;
  const rows = context.demo_feedback || [];
  body.innerHTML = '';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.textContent = 'No demo suggestions submitted yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.created_at_utc);
    addCell(tr, row.actor_display_name || row.actor_email);
    addCell(tr, `${row.screen} / step ${row.demo_step || 0}`);
    addCell(tr, row.category);
    addCell(tr, row.comment);
    addCell(tr, row.status);
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = row.status === 'closed' ? 'Reopen' : 'Close';
    button.addEventListener('click', () => updateDemoFeedbackStatus(row.id, row.status === 'closed' ? 'open' : 'closed'));
    actionCell.appendChild(button);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
function renderDemoFiles() {
  const body = byId('demoFilesBody');
  if (!body || !context) return;
  const rows = context.demo_artifacts || [];
  body.innerHTML = '';
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = 'No demo files generated yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.filename);
    addCell(tr, `${row.size_bytes || 0} bytes`);
    addCell(tr, row.updated_at_utc);
    const actionCell = document.createElement('td');
    const link = document.createElement('a');
    link.href = row.download_url;
    link.textContent = 'Download';
    actionCell.appendChild(link);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
async function updateDemoFeedbackStatus(feedbackId, status) {
  if (!feedbackId) return;
  try {
    await api(`/pay/api/demo/feedback/${encodeURIComponent(feedbackId)}`, {
      method: 'PUT',
      body: JSON.stringify({ status }),
    });
    await loadContext();
  } catch (err) {
    setText('demoFeedbackStatus', err.message);
    setText('demoSettingsStatus', err.message);
  }
}
function renderEntries() {
  const body = byId('entriesBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const entries = context.entries || [];
  const table = body.closest('table');
  const showReviewControls = table && table.dataset.review === 'true';
  if ((!selectedEntryId || !entries.some(row => row.id === selectedEntryId)) && entries.length) selectedEntryId = entries[0].id;
  for (const row of entries) {
    const tr = document.createElement('tr');
    const pick = document.createElement('td');
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'entryPick';
    radio.value = row.id;
    radio.checked = row.id === selectedEntryId;
    radio.addEventListener('change', () => { selectedEntryId = row.id; syncMileageFormFromEntry(); renderFundAllocationPanel(); });
    pick.appendChild(radio);
    tr.appendChild(pick);
    addCell(tr, row.entry_date);
    addCell(tr, row.display_name || row.user_email);
    addCell(tr, money(row.hours));
    addCell(tr, currency(Number(row.lost_wage_hourly_rate || row.hourly_rate || 0) * Number(row.hours || 0)));
    addCell(tr, currency(row.mileage_amount));
    addCell(tr, currency(Number(row.rentals_amount || 0) + Number(row.meals_amount || 0) + Number(row.hotel_amount || 0) + Number(row.miscellaneous_amount || 0)));
    addCell(tr, currency(row.president_diff_amount));
    if (showReviewControls) addCell(tr, `${row.review_status || 'pending'}${row.review_note ? ': ' + row.review_note : ''}`);
    addCell(tr, row.submitter_certified_at_utc ? 'Signed off' : 'Not signed off');
    addCell(tr, row.notes || '');
    const actionCell = document.createElement('td');
    const canEditEntryForm = !!byId('entryForm') && (PAY_VIEW === 'entry' || PAY_VIEW === 'president');
    if (context.actor && (context.actor.can_lock || canEditEntryForm)) {
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'secondary';
      editBtn.textContent = 'Edit Voucher';
      editBtn.addEventListener('click', () => canEditEntryForm ? openEntryForEdit(row) : openCorrectionForEntry(row));
      actionCell.appendChild(editBtn);
      if (canEditEntryForm && !row.locked_at_utc) {
        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'danger';
        deleteBtn.textContent = 'Delete';
        deleteBtn.addEventListener('click', () => deleteEntry(row));
        actionCell.appendChild(deleteBtn);
      }
    }
    if (showReviewControls && context.actor && context.actor.can_lock) {
      const reviewInput = document.createElement('input');
      reviewInput.placeholder = 'Review note';
      reviewInput.value = row.review_note || '';
      actionCell.appendChild(reviewInput);
      for (const [status, label] of [['approved', 'Approve'], ['needs_fix', 'Needs Fix'], ['rejected', 'Reject / Exclude']]) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = status === 'rejected' ? 'danger' : 'secondary';
        btn.textContent = label;
        btn.addEventListener('click', () => reviewEntry(row.id, status, reviewInput.value));
        actionCell.appendChild(btn);
      }
    }
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
  syncMileageFormFromEntry();
  renderFundAllocationPanel();
}
function renderDailyTally() {
  const body = byId('dailyTallyBody');
  if (!body || !context) return;
  const rowsByDate = new Map();
  for (const row of context.entries || []) {
    const date = row.entry_date || '';
    if (!date) continue;
    if (!rowsByDate.has(date)) rowsByDate.set(date, { hours: 0, lost: 0, mileage: 0, expenses: 0, president_diff: 0 });
    const tally = rowsByDate.get(date);
    tally.hours += Number(row.hours || 0);
    tally.lost += Number(row.lost_wage_hourly_rate || row.hourly_rate || 0) * Number(row.hours || 0);
    tally.mileage += Number(row.mileage_amount || 0);
    tally.expenses += Number(row.rentals_amount || 0) + Number(row.meals_amount || 0) + Number(row.hotel_amount || 0) + Number(row.miscellaneous_amount || 0);
    tally.president_diff += Number(row.president_diff_amount || 0);
  }
  body.innerHTML = '';
  if (!rowsByDate.size) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 7;
    td.textContent = 'No entries yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    setText('dailyTallyStats', '$0.00');
    return;
  }
  const totals = { hours: 0, lost: 0, mileage: 0, expenses: 0, president_diff: 0 };
  for (const [date, tally] of Array.from(rowsByDate.entries()).sort((left, right) => left[0].localeCompare(right[0]))) {
    totals.hours += tally.hours;
    totals.lost += tally.lost;
    totals.mileage += tally.mileage;
    totals.expenses += tally.expenses;
    totals.president_diff += tally.president_diff;
    const tr = document.createElement('tr');
    addCell(tr, date);
    addCell(tr, money(tally.hours));
    addCell(tr, currency(tally.lost));
    addCell(tr, currency(tally.mileage));
    addCell(tr, currency(tally.expenses));
    addCell(tr, currency(tally.president_diff));
    addCell(tr, currency(tally.lost + tally.mileage + tally.expenses + tally.president_diff));
    body.appendChild(tr);
  }
  setText('dailyTallyStats', `${money(totals.hours)} hrs | ${currency(totals.lost + totals.mileage + totals.expenses + totals.president_diff)}`);
}
let activeAddressInput = null;
function placeKey(value) {
  return String(value || '').trim().toLowerCase();
}
function rememberCommonPlace(label, address) {
  const cleanLabel = String(label || '').trim();
  const cleanAddress = String(address || '').trim();
  if (!cleanAddress) return;
  for (const value of [cleanLabel, cleanAddress, cleanLabel ? `${cleanLabel} - ${cleanAddress}` : cleanAddress]) {
    const key = placeKey(value);
    if (key && !commonPlaceLookup.has(key)) commonPlaceLookup.set(key, cleanAddress);
  }
}
function resolveCommonPlaceInput(input) {
  if (!input) return;
  const resolved = commonPlaceLookup.get(placeKey(input.value));
  if (resolved) input.value = resolved;
}
function selectedEntry() {
  if (!context || !selectedEntryId) return null;
  return (context.entries || []).find(row => row.id === selectedEntryId) || null;
}
function mileageRateForYear(year) {
  const rates = context && context.settings && context.settings.irs_rates;
  if (rates && Object.prototype.hasOwnProperty.call(rates, String(year))) return rates[String(year)];
  return '0.67';
}
function normalizeMileageRate(value) {
  const parsed = Number(String(value || '').replace('$', '').replace(',', '').trim());
  if (!Number.isFinite(parsed) || parsed <= 0) return 0.67;
  return Math.abs(parsed) >= 10 ? parsed / 100 : parsed;
}
function syncMileageRateFromDate() {
  const dateEl = byId('date');
  const display = byId('irsRateDisplay');
  if (!display) return;
  if (!dateEl || !dateEl.value) {
    display.textContent = 'Select a date';
    return;
  }
  const year = dateEl.value.slice(0, 4);
  display.textContent = `${year}: $${normalizeMileageRate(mileageRateForYear(year)).toFixed(3)} per mile`;
}
function syncMileageFormFromEntry() {
  const form = byId('mileageForm');
  if (!form || !context) return;
  const select = byId('mileageEntrySelect');
  const entry = selectedEntry();
  if (select && select.value !== (selectedEntryId || '')) select.value = selectedEntryId || '';
  const nameInput = form.elements.namedItem('name');
  if (nameInput) nameInput.value = entry ? (entry.display_name || context.actor.display_name || entry.user_email || context.actor.email || '') : ((context.actor && (context.actor.display_name || context.actor.email)) || '');
  if (!entry) return;
  form.local_number.value = entry.local_number || '3106';
  form.date.value = entry.entry_date || form.date.value;
  form.description.value = entry.notes || form.description.value || 'Union business';
  syncMileageRateFromDate();
}
function renderMileageEntrySelect() {
  const select = byId('mileageEntrySelect');
  if (!select || !context) return;
  const entries = context.entries || [];
  const previous = selectedEntryId;
  select.innerHTML = '<option value="">Create a new mileage entry...</option>';
  for (const row of entries) {
    const opt = document.createElement('option');
    opt.value = row.id;
    opt.textContent = `${row.entry_date} - ${row.display_name || row.user_email || 'Entry'}`;
    select.appendChild(opt);
  }
  if (previous && entries.some(row => row.id === previous)) selectedEntryId = previous;
  else if (entries.length) selectedEntryId = entries[0].id;
  select.value = selectedEntryId || '';
  syncMileageFormFromEntry();
}
function wireAddressInput(input) {
  input.setAttribute('list', 'commonPlaceOptions');
  input.addEventListener('focus', () => { activeAddressInput = input; });
  input.addEventListener('change', () => resolveCommonPlaceInput(input));
  input.addEventListener('blur', () => resolveCommonPlaceInput(input));
}
function mileageLocationInputs() {
  return Array.from(document.querySelectorAll('#locations .address-input'));
}
function mileageLocationsFromInputs() {
  return mileageLocationInputs().map(input => input.value.trim()).filter(Boolean);
}
function applyResolvedMileageLocations(locations) {
  const inputs = mileageLocationInputs();
  for (let index = 0; index < inputs.length && index < locations.length; index += 1) {
    if (locations[index]) inputs[index].value = locations[index];
  }
}
async function checkMileageAddresses({ quiet = false } = {}) {
  const locations = mileageLocationsFromInputs();
  if (locations.length < 2) {
    const message = 'Enter at least an origin and destination.';
    if (!quiet) setText('addressCheckStatus', message);
    throw new Error(message);
  }
  if (!quiet) setText('addressCheckStatus', 'Checking route addresses...');
  const result = await api('/pay/api/mileage/check-addresses', {
    method: 'POST',
    body: JSON.stringify({ locations }),
  });
  applyResolvedMileageLocations(result.locations || []);
  const summary = `${money(result.total_miles)} miles checked. Addresses updated from Google route results.`;
  setText('addressCheckStatus', summary);
  if (!quiet) setText('mileageStatus', summary);
  return result;
}
function addMileageLocationField(value, placeholder = 'Enter location') {
  const locationsDiv = byId('locations');
  if (!locationsDiv) return;
  const div = document.createElement('div');
  div.className = 'input-group mb-2';
  const input = document.createElement('input');
  input.type = 'text';
  input.name = 'locations';
  input.placeholder = placeholder;
  input.className = 'form-control address-input';
  input.required = true;
  input.autocomplete = 'off';
  input.setAttribute('list', 'commonPlaceOptions');
  if (value) input.value = value;
  const button = document.createElement('button');
  button.className = 'btn btn-danger remove-location';
  button.type = 'button';
  button.setAttribute('aria-label', 'Remove location');
  button.innerHTML = '&times;';
  button.addEventListener('click', () => div.remove());
  div.appendChild(input);
  div.appendChild(button);
  locationsDiv.appendChild(div);
  wireAddressInput(input);
}
function activeFunds() {
  return (context && context.funds ? context.funds : []).filter(row => (row.status || 'active') === 'active');
}
function populateFundSelect(select, { includeBlank = false } = {}) {
  if (!select || !context) return;
  const current = select.value;
  select.innerHTML = '';
  if (includeBlank) {
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = 'Select fund...';
    select.appendChild(blank);
  }
  for (const fund of activeFunds()) {
    const option = document.createElement('option');
    option.value = fund.id;
    option.textContent = `${fund.name} (${fund.fund_type || 'fund'})`;
    select.appendChild(option);
  }
  if (current && Array.from(select.options).some(option => option.value === current)) select.value = current;
}
function currentFundAllocations() {
  if (!context || !selectedEntryId) return [];
  return (context.fund_allocations || []).filter(row => row.entry_id === selectedEntryId);
}
function allocationPayloadFromForm(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  return {
    fund_id: data.fund_id || '',
    hours: Number(data.hours || 0),
    mileage_miles: Number(data.mileage_miles || 0),
    mileage_amount: Number(data.mileage_amount || 0),
    rentals_amount: Number(data.rentals_amount || 0),
    meals_amount: Number(data.meals_amount || 0),
    hotel_amount: Number(data.hotel_amount || 0),
    miscellaneous_amount: Number(data.miscellaneous_amount || 0),
    notes: data.notes || '',
  };
}
async function saveFundAllocations(rows) {
  if (!selectedEntryId) throw new Error('Select a pay entry first.');
  await api(`/pay/api/entries/${encodeURIComponent(selectedEntryId)}/fund-allocations`, {
    method: 'POST',
    body: JSON.stringify({ allocations: rows }),
  });
  await loadContext();
}
async function removeFundAllocation(fundId) {
  const rows = currentFundAllocations().filter(row => row.fund_id !== fundId).map(row => ({
    fund_id: row.fund_id,
    hours: Number(row.hours || 0),
    mileage_miles: Number(row.mileage_miles || 0),
    mileage_amount: Number(row.mileage_amount || 0),
    rentals_amount: Number(row.rentals_amount || 0),
    meals_amount: Number(row.meals_amount || 0),
    hotel_amount: Number(row.hotel_amount || 0),
    miscellaneous_amount: Number(row.miscellaneous_amount || 0),
    notes: row.notes || '',
  }));
  await saveFundAllocations(rows);
}
function renderFundAllocationPanel() {
  const form = byId('fundAllocationForm');
  const body = byId('fundAllocationsBody');
  if (!form || !body || !context) return;
  populateFundSelect(form.elements.namedItem('fund_id'), { includeBlank: true });
  const entry = selectedEntry();
  setText('fundAllocationEntryLabel', entry ? `${entry.entry_date} - ${entry.display_name || entry.user_email}` : 'Select an entry below');
  body.innerHTML = '';
  const rows = currentFundAllocations();
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.textContent = selectedEntryId ? 'No fund allocations saved for this entry.' : 'Select an entry to allocate time or expenses.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.fund_name || row.fund_id);
    addCell(tr, money(row.hours));
    addCell(tr, money(row.mileage_miles));
    addCell(tr, currency(row.mileage_amount));
    addCell(tr, currency(row.meals_amount));
    addCell(tr, currency(row.hotel_amount));
    addCell(tr, currency(row.rentals_amount));
    addCell(tr, currency(row.miscellaneous_amount));
    const action = document.createElement('td');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary';
    remove.textContent = 'Remove';
    remove.addEventListener('click', async () => {
      try { await removeFundAllocation(row.fund_id); setText('fundAllocationStatus', 'Allocation removed'); }
      catch (err) { setText('fundAllocationStatus', err.message); }
    });
    action.appendChild(remove);
    tr.appendChild(action);
    body.appendChild(tr);
  }
}
function renderFundsAdmin() {
  if (!context) return;
  for (const id of ['fundLedgerFundSelect', 'fundPacketFundSelect']) populateFundSelect(byId(id), { includeBlank: true });
  const body = byId('fundBalancesBody');
  if (body) {
    body.innerHTML = '';
    const funds = context.funds || [];
    if (!funds.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 8;
      td.textContent = 'No SIF or Growth funds set up yet.';
      tr.appendChild(td);
      body.appendChild(tr);
    }
    for (const fund of funds) {
      const tr = document.createElement('tr');
      addCell(tr, fund.name);
      addCell(tr, fund.fund_type);
      addCell(tr, fund.status);
      addCell(tr, currency(fund.advance_amount));
      addCell(tr, currency(fund.allocated_amount));
      addCell(tr, currency(fund.reimbursement_submitted_amount));
      addCell(tr, currency(fund.reimbursement_needed));
      addCell(tr, currency(fund.remaining_balance));
      body.appendChild(tr);
    }
  }
  const packetBody = byId('fundPacketsBody');
  if (packetBody) {
    packetBody.innerHTML = '';
    for (const packet of context.fund_packets || []) {
      const tr = document.createElement('tr');
      addCell(tr, packet.fund_name);
      addCell(tr, `${packet.period_start} to ${packet.period_end}`);
      addCell(tr, currency(packet.total_amount));
      addCell(tr, currency(packet.ending_balance));
      const link = document.createElement('td');
      const a = document.createElement('a');
      a.href = packet.workbook_download_url;
      a.textContent = 'Download Excel';
      link.appendChild(a);
      if (packet.sharepoint_folder_web_url) {
        link.appendChild(document.createTextNode(' | '));
        const sp = document.createElement('a');
        sp.href = packet.sharepoint_folder_web_url;
        sp.textContent = 'SharePoint';
        sp.target = '_blank';
        sp.rel = 'noopener';
        link.appendChild(sp);
      }
      tr.appendChild(link);
      packetBody.appendChild(tr);
    }
  }
  const packetForm = byId('fundPacketForm');
  if (packetForm && context.period) {
    if (!packetForm.period_start.value) packetForm.period_start.value = context.period.period_start || '';
    if (!packetForm.period_end.value) packetForm.period_end.value = context.period.period_end || '';
  }
}
function renderCommonPlaces() {
  const select = byId('commonPlaceSelect');
  const datalist = byId('commonPlaceOptions');
  if ((!select && !datalist) || !context) return;
  const places = (context.settings && context.settings.common_places) || [];
  commonPlaceLookup = new Map();
  if (select) select.innerHTML = '<option value="">Select a place...</option>';
  if (datalist) datalist.innerHTML = '';
  for (const place of places) {
    const label = place.label || 'Place';
    const address = place.address || '';
    rememberCommonPlace(label, address);
    if (datalist && address) {
      const byLabel = document.createElement('option');
      byLabel.value = `${label} - ${address}`;
      datalist.appendChild(byLabel);
      const byAddress = document.createElement('option');
      byAddress.value = address;
      datalist.appendChild(byAddress);
    }
    if (!select) continue;
    const opt = document.createElement('option');
    opt.value = address;
    opt.textContent = `${label} - ${address}`;
    select.appendChild(opt);
  }
}
function normalizePlaceRows(rows) {
  const out = [];
  const seen = new Set();
  for (const row of rows || []) {
    const label = String((row && row.label) || '').trim();
    const address = String((row && row.address) || '').trim();
    if (!label || !address) continue;
    const key = `${label.toLowerCase()}|${address.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ label, address });
  }
  return out;
}
function renderAdminCommonPlaces() {
  const body = byId('commonPlacesBody');
  if (!body || !context) return;
  commonPlacesDraft = normalizePlaceRows((context.settings && context.settings.common_places) || []);
  body.innerHTML = '';
  for (const [index, place] of commonPlacesDraft.entries()) {
    const tr = document.createElement('tr');
    addCell(tr, place.label);
    addCell(tr, place.address);
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'danger';
    button.textContent = 'Remove';
    button.addEventListener('click', () => removeCommonPlace(index));
    actionCell.appendChild(button);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
async function saveCommonPlaces(message) {
  const rows = normalizePlaceRows(commonPlacesDraft);
  await api('/pay/api/settings', { method: 'PUT', body: JSON.stringify({ common_places: rows }) });
  setText('commonPlaceStatus', message || 'Saved');
  await loadContext();
}
async function removeCommonPlace(index) {
  commonPlacesDraft.splice(index, 1);
  try {
    await saveCommonPlaces('Removed');
  } catch (err) {
    setText('commonPlaceStatus', err.message);
  }
}
function addCommonPlaceToLocations(value) {
  if (!value) return;
  if (activeAddressInput && activeAddressInput.closest('#locations') && !activeAddressInput.value.trim()) {
    activeAddressInput.value = value;
    return;
  }
  const inputs = Array.from(document.querySelectorAll('#locations .address-input'));
  const empty = inputs.find(input => !input.value.trim());
  if (empty) {
    empty.value = value;
    return;
  }
  addMileageLocationField(value);
}
function appendMileageMetric(parent, value, label) {
  const item = document.createElement('div');
  const span = document.createElement('span');
  span.textContent = value || '';
  const strong = document.createElement('strong');
  strong.textContent = label || '';
  item.appendChild(span);
  item.appendChild(strong);
  parent.appendChild(item);
}
function renderMileageForms() {
  const body = byId('mileageFormsBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const forms = (context.attachments || []).filter(row => row.attachment_type === 'mileage_pdf');
  if (!forms.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.textContent = 'No mileage forms generated yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of forms) {
    const tr = document.createElement('tr');
    const toggleCell = document.createElement('td');
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'secondary';
    toggle.textContent = 'Details';
    toggleCell.appendChild(toggle);
    tr.appendChild(toggleCell);
    addCell(tr, row.entry_date);
    addCell(tr, row.display_name || row.user_email);
    addCell(tr, row.filename);
    addCell(tr, row.scan_status || 'generated');
    addCell(tr, money(row.mileage_miles));
    addCell(tr, row.mileage_rate ? rateCurrency(row.mileage_rate, 3) : '');
    addCell(tr, row.mileage_amount ? currency(row.mileage_amount) : '');
    const actionCell = document.createElement('td');
    const download = document.createElement('a');
    download.href = `/pay/api/attachments/${encodeURIComponent(row.id)}/download`;
    download.textContent = 'Download PDF';
    actionCell.appendChild(download);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary';
    remove.textContent = 'Remove Report';
    remove.disabled = !row.can_remove;
    remove.title = row.can_remove ? 'Remove this mileage report and subtract it from the entry.' : (row.remove_reason || 'Cannot remove this report.');
    remove.addEventListener('click', () => removeMileageReport(row.id, row.filename));
    actionCell.appendChild(remove);
    tr.appendChild(actionCell);
    const details = document.createElement('tr');
    details.className = 'hidden';
    const detailsCell = document.createElement('td');
    detailsCell.colSpan = 9;
    const panel = document.createElement('div');
    panel.className = 'subpanel';
    const title = document.createElement('strong');
    title.textContent = 'Mileage report breakdown';
    const metrics = document.createElement('div');
    metrics.className = 'metric-row';
    appendMileageMetric(metrics, row.display_name || row.user_email || '', 'Name');
    appendMileageMetric(metrics, row.local_number || '', 'Local');
    appendMileageMetric(metrics, row.entry_date || '', 'Date');
    appendMileageMetric(metrics, row.description || '', 'Description');
    appendMileageMetric(metrics, `${rateCurrency(row.mileage_rate, 3)} per mile`, 'IRS Standard Mileage Rate');
    appendMileageMetric(metrics, `${money(row.mileage_miles)} miles`, 'Total Distance');
    appendMileageMetric(metrics, currency(row.mileage_amount), 'Total Reimbursement');
    appendMileageMetric(metrics, row.filename || '', 'PDF');
    appendMileageMetric(metrics, row.scan_status || 'generated', 'Status');
    panel.appendChild(title);
    panel.appendChild(metrics);
    detailsCell.appendChild(panel);
    details.appendChild(detailsCell);
    toggle.addEventListener('click', () => details.classList.toggle('hidden'));
    body.appendChild(tr);
    body.appendChild(details);
  }
}
async function removeMileageReport(attachmentId, filename) {
  if (!attachmentId) return;
  if (!confirm(`Remove ${filename || 'this mileage report'} and subtract it from the entry?`)) return;
  try {
    await api(`/pay/api/attachments/${encodeURIComponent(attachmentId)}`, { method: 'DELETE' });
    setText('mileageStatus', 'Mileage report removed');
    await loadContext();
  } catch (err) {
    setText('mileageStatus', err.message);
  }
}

function renderStubs() {
  const body = byId('stubsBody');
  if (!body || !context) return;
  body.innerHTML = '';
  for (const row of context.compensation_stubs || []) {
    const tr = document.createElement('tr');
    addCell(tr, row.user_email);
    addCell(tr, row.payroll_month);
    addCell(tr, `${row.base_wage_input_type} ${currency(row.base_wage_amount)}`);
    addCell(tr, currency(row.commission_average_monthly));
    addCell(tr, currency(row.commission_hourly_rate));
    addCell(tr, currency(row.calculated_hourly_rate));
    addCell(tr, row.filename);
    body.appendChild(tr);
  }
}
function renderPayUsers() {
  const body = byId('payUsersBody');
  if (!body || !context) return;
  body.innerHTML = '';
  for (const row of context.pay_users || []) {
    const tr = document.createElement('tr');
    addCell(tr, row.email);
    addCell(tr, row.display_name);
    addCell(tr, row.role);
    addCell(tr, row.status);
    body.appendChild(tr);
  }
}
function renderInternalRoles() {
  const body = byId('internalRolesBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const rows = context.internal_roles || [];
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.textContent = 'No internal role assignments saved.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.principal_display_name);
    addCell(tr, row.principal_email);
    addCell(tr, row.role);
    addCell(tr, row.status);
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'danger';
    button.textContent = 'Remove';
    button.addEventListener('click', () => removeInternalRole(row.assignment_id));
    actionCell.appendChild(button);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
function normalizeProfileWageInputType(form) {
  if (!form || !form.base_wage_input_type) return;
  if (!form.base_wage_input_type.value) form.base_wage_input_type.value = 'hourly';
}
function fillPayProfileForm(row) {
  const form = byId('payProfileForm');
  if (!form || !row) return;
  form.principal_id.value = row.principal_id || '';
  form.principal_email.value = row.principal_email || row.email || row.user_principal_name || '';
  form.principal_display_name.value = row.principal_display_name || row.display_name || '';
  form.pay_basis.value = row.pay_basis || 'expense_only';
  form.base_wage_input_type.value = row.base_wage_input_type || 'hourly';
  form.base_wage_amount.value = row.base_wage_amount || '';
  form.weekly_basis_hours.value = row.weekly_basis_hours || '40';
  form.commission_month_1_amount.value = row.commission_month_1_amount || '';
  form.commission_month_2_amount.value = row.commission_month_2_amount || '';
  form.commission_month_3_amount.value = row.commission_month_3_amount || '';
  form.status.value = row.status || 'active';
  form.default_address.value = row.default_address || '';
  form.notes.value = row.notes || '';
  normalizeProfileWageInputType(form);
}
function fillMyPayProfileForm() {
  const form = byId('myPayProfileForm');
  if (!form || !context) return;
  const actor = context.actor || {};
  const row = context.pay_profile || {};
  form.principal_email.value = actor.email || row.principal_email || '';
  form.principal_display_name.value = actor.display_name || row.principal_display_name || actor.email || '';
  const presidentOption = form.pay_basis.querySelector('option[value="president"]');
  const basis = row.pay_basis || 'hourly';
  const canUsePresidentBasis = actor.can_lock || (actor.is_president && basis === 'president');
  if (presidentOption) {
    presidentOption.disabled = !canUsePresidentBasis;
    presidentOption.hidden = !canUsePresidentBasis;
  }
  form.pay_basis.value = basis === 'president' && !canUsePresidentBasis ? 'hourly' : basis;
  form.base_wage_input_type.value = row.base_wage_input_type || 'hourly';
  form.base_wage_amount.value = row.base_wage_amount || '';
  form.weekly_basis_hours.value = row.weekly_basis_hours || '40';
  form.commission_month_1_amount.value = row.commission_month_1_amount || '';
  form.commission_month_2_amount.value = row.commission_month_2_amount || '';
  form.commission_month_3_amount.value = row.commission_month_3_amount || '';
  form.default_address.value = row.default_address || '';
  form.notes.value = row.notes || '';
  normalizeProfileWageInputType(form);
  const entryForm = byId('entryForm');
  if (entryForm && entryForm.address && !entryForm.address.value) entryForm.address.value = row.default_address || '';
  const pending = (context.pay_profile_change_requests || []).find(item => item.status === 'pending' && String(item.principal_email || '').toLowerCase() === String(actor.email || '').toLowerCase());
  const baseSummary = row.calculated_hourly_rate ? `Current hourly rate $${money(row.calculated_hourly_rate)}` : 'No saved pay profile yet';
  setText('myPayProfileSummary', pending ? `${baseSummary}; wage change pending treasurer approval` : baseSummary);
  syncPayProfileVisibility();
}
function previousPayrollMonthFor(value) {
  const raw = String(value || '').slice(0, 10);
  const match = raw.match(/^([0-9]{4})-([0-9]{2})-[0-9]{2}$/);
  const now = new Date();
  let year = match ? Number(match[1]) : now.getFullYear();
  let month = match ? Number(match[2]) : now.getMonth() + 1;
  month -= 1;
  if (month < 1) { month = 12; year -= 1; }
  return `${year}-${String(month).padStart(2, '0')}`;
}
function currentEntryDateValue() {
  const entryForm = byId('entryForm');
  return (entryForm && entryForm.entry_date && entryForm.entry_date.value) || (context && context.period && context.period.period_start) || '';
}
function activePayBasis() {
  const form = byId('myPayProfileForm');
  if (form && form.pay_basis) return form.pay_basis.value || 'hourly';
  return (context && context.pay_profile && context.pay_profile.pay_basis) || 'hourly';
}
function isPresidentDifferentialEnabled() {
  const input = document.querySelector('#presidentDifferentialPanel input[name="president_diff_hours"]');
  return !!input && !input.disabled;
}
function balancePresidentDailyHours(changedName) {
  const form = byId('entryForm');
  if (!form || !isPresidentDifferentialEnabled()) return;
  const hoursInput = form.elements.namedItem('hours');
  const diffInput = form.elements.namedItem('president_diff_hours');
  if (!hoursInput || !diffInput) return;
  const rawHours = Number(hoursInput.value || 0);
  const rawDiff = Number(diffInput.value || 0);
  if (changedName === 'president_diff_hours') {
    const boundedDiff = Math.max(0, Math.min(8, rawDiff));
    diffInput.value = boundedDiff ? boundedDiff.toFixed(2).replace(/[.]00$/, '') : '';
    hoursInput.value = Math.max(0, 8 - boundedDiff).toFixed(2).replace(/[.]00$/, '');
    return;
  }
  if (changedName === 'hours' || rawHours || rawDiff) {
    const boundedHours = Math.max(0, Math.min(8, rawHours));
    hoursInput.value = boundedHours ? boundedHours.toFixed(2).replace(/[.]00$/, '') : '';
    const diff = boundedHours || rawDiff ? Math.max(0, 8 - boundedHours) : 0;
    diffInput.value = diff ? diff.toFixed(2).replace(/[.]00$/, '') : '';
  }
}
function syncPayProfileVisibility() {
  normalizeProfileWageInputType(byId('myPayProfileForm'));
  normalizeProfileWageInputType(byId('payProfileForm'));
  const basis = activePayBasis();
  document.querySelectorAll('[data-basis-field="wage"]').forEach(node => node.classList.toggle('hidden', basis === 'expense_only'));
  document.querySelectorAll('[data-basis-field="commission"]').forEach(node => node.classList.toggle('hidden', basis !== 'commission'));
  const proofPanel = byId('commissionProofPanel');
  if (proofPanel) proofPanel.classList.toggle('hidden', basis !== 'commission');
  const stubForm = byId('stubForm');
  const profile = (context && context.pay_profile) || {};
  if (stubForm && context) {
    if (!stubForm.user_email.value) stubForm.user_email.value = (context.actor && context.actor.email) || '';
    if (!stubForm.payroll_month.value) stubForm.payroll_month.value = previousPayrollMonthFor(currentEntryDateValue());
    stubForm.base_wage_input_type.value = profile.base_wage_input_type || 'hourly';
    stubForm.base_wage_amount.value = profile.base_wage_amount || '';
    stubForm.weekly_basis_hours.value = profile.weekly_basis_hours || '40';
    stubForm.commission_month_1_amount.value = profile.commission_month_1_amount || '';
    stubForm.commission_month_2_amount.value = profile.commission_month_2_amount || '';
    stubForm.commission_month_3_amount.value = profile.commission_month_3_amount || '';
  }
  setText('commissionProofHelp', basis === 'commission' ? `Required for lost-wage hours using ${previousPayrollMonthFor(currentEntryDateValue())} company payroll.` : '');
}
function mergedPayProfileRows() {
  const rowsByEmail = new Map();
  function mergeRow(email, values) {
    const key = String(email || '').toLowerCase();
    if (!key) return;
    const existing = rowsByEmail.get(key) || { principal_email: key, sources: [] };
    const source = values.source || '';
    if (source && !existing.sources.includes(source)) existing.sources.push(source);
    rowsByEmail.set(key, { ...existing, ...values, principal_email: values.principal_email || key });
  }
  for (const row of context.pay_profiles || []) mergeRow(row.principal_email, { ...row, source: 'Profile' });
  for (const row of context.pay_users || []) {
    mergeRow(row.email, {
      source: 'External pay user',
      principal_email: row.email,
      principal_display_name: row.display_name,
      status: row.status,
    });
  }
  for (const row of context.internal_roles || []) {
    mergeRow(row.principal_email, {
      source: row.role === 'president' ? 'President role' : row.role === 'treasurer' ? 'Treasurer role' : row.role === 'pay_viewer' ? 'Pay Viewer role' : 'Internal role',
      principal_id: row.principal_id,
      principal_email: row.principal_email,
      principal_display_name: row.principal_display_name,
      status: row.status,
    });
  }
  return Array.from(rowsByEmail.values()).sort((a, b) => String(a.principal_display_name || a.principal_email || '').localeCompare(String(b.principal_display_name || b.principal_email || '')));
}
function renderPayProfiles() {
  const body = byId('payProfilesBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const rows = mergedPayProfileRows();
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.textContent = 'No people or pay profiles saved.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, (row.sources && row.sources.length ? row.sources : [row.source || 'Profile']).join(', '));
    addCell(tr, row.principal_display_name);
    addCell(tr, row.principal_email);
    addCell(tr, row.pay_basis || 'No profile');
    addCell(tr, row.base_wage_input_type ? `${row.base_wage_input_type} ${currency(row.base_wage_amount)}` : '');
    addCell(tr, currency(row.commission_hourly_rate));
    addCell(tr, currency(row.calculated_hourly_rate));
    addCell(tr, row.status);
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = 'Edit Profile';
    button.addEventListener('click', () => fillPayProfileForm(row));
    actionCell.appendChild(button);
    if (row.pay_basis) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'danger';
      remove.textContent = 'Remove Person';
      remove.addEventListener('click', () => removePayProfile(row.principal_email, row.principal_display_name));
      actionCell.appendChild(remove);
    }
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
function renderProfileChangeRequests() {
  const body = byId('profileChangeRequestsBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const rows = (context.pay_profile_change_requests || []).filter(row => row.status === 'pending');
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 8;
    td.textContent = 'No pending wage changes.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.requested_at_utc);
    addCell(tr, row.principal_display_name);
    addCell(tr, row.principal_email);
    addCell(tr, row.pay_basis);
    addCell(tr, `${row.base_wage_input_type} ${currency(row.base_wage_amount)}`);
    addCell(tr, currency(row.calculated_hourly_rate));
    addCell(tr, row.notes || row.review_note || '');
    const actionCell = document.createElement('td');
    const approve = document.createElement('button');
    approve.type = 'button';
    approve.className = 'secondary';
    approve.textContent = 'Approve';
    approve.addEventListener('click', () => reviewProfileChange(row.id, true));
    const reject = document.createElement('button');
    reject.type = 'button';
    reject.className = 'danger';
    reject.textContent = 'Reject';
    reject.addEventListener('click', () => reviewProfileChange(row.id, false));
    actionCell.appendChild(approve);
    actionCell.appendChild(reject);
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
function renderInternalRoleSearch(rows) {
  const body = byId('internalRoleSearchBody');
  if (!body) return;
  internalRoleSearchRows = Array.isArray(rows) ? rows : [];
  body.innerHTML = '';
  if (!internalRoleSearchRows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = 'No matching internal users found.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  internalRoleSearchRows.forEach((row, index) => {
    const tr = document.createElement('tr');
    addCell(tr, row.display_name);
    addCell(tr, row.email || row.user_principal_name);
    addCell(tr, row.match_source);
    const actionCell = document.createElement('td');
    const useButton = document.createElement('button');
    useButton.type = 'button';
    useButton.className = 'secondary';
    useButton.textContent = 'Use';
    useButton.addEventListener('click', () => fillPayProfileForm(row));
    actionCell.appendChild(useButton);
    for (const role of ['president', 'treasurer', 'pay_viewer']) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = role === 'president' ? 'Set President' : role === 'treasurer' ? 'Set Treasurer' : 'Set Pay Viewer';
      button.addEventListener('click', () => assignInternalRole(index, role));
      actionCell.appendChild(button);
    }
    tr.appendChild(actionCell);
    body.appendChild(tr);
  });
}
function renderIrsCandidates() {
  const body = byId('irsCandidatesBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const rows = context.irs_rate_candidates || [];
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.textContent = 'No staged IRS rates.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.rate_year);
    addCell(tr, rateCurrency(row.rate_per_mile, 3));
    const sourceCell = document.createElement('td');
    const link = document.createElement('a');
    link.href = row.source_url;
    link.textContent = row.source_title || row.source_url;
    link.rel = 'noreferrer';
    sourceCell.appendChild(link);
    tr.appendChild(sourceCell);
    addCell(tr, row.status);
    const actionCell = document.createElement('td');
    if (row.status === 'pending') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'secondary';
      btn.textContent = 'Approve';
      btn.addEventListener('click', () => approveIrsRate(row.id));
      actionCell.appendChild(btn);
    }
    tr.appendChild(actionCell);
    body.appendChild(tr);
  }
}
function latestSavedWageScale() {
  const rows = context && Array.isArray(context.wage_scales) ? context.wage_scales.slice() : [];
  rows.sort((a, b) => {
    const updatedA = Date.parse(a.updated_at_utc || '') || 0;
    const updatedB = Date.parse(b.updated_at_utc || '') || 0;
    if (updatedA !== updatedB) return updatedB - updatedA;
    return Number(b.id || 0) - Number(a.id || 0);
  });
  return rows.find(row => String(row.target_scale || '36') === '36') || rows[0] || null;
}
function fillSettings() {
  const form = byId('settingsForm');
  if (form && context) {
    form.president_email.value = context.settings.president_email || '';
    form.treasurer_emails.value = Array.isArray(context.settings.treasurer_emails) ? context.settings.treasurer_emails.join(', ') : '';
    const wageScale = latestSavedWageScale();
    if (wageScale) {
      if (form.effective_date) form.effective_date.value = wageScale.effective_date || '';
      if (form.weekly_basis_hours) form.weekly_basis_hours.value = String(wageScale.weekly_basis_hours || 40);
      if (form.target_weekly_amount) form.target_weekly_amount.value = wageScale.target_weekly_amount == null ? '' : String(wageScale.target_weekly_amount);
    }
  }
  const demoForm = byId('demoSettingsForm');
  if (demoForm && context) {
    const demoSettings = context.demo_settings || context.settings || {};
    demoForm.demo_mode_enabled.value = demoModeEnabled() ? 'true' : 'false';
    demoForm.demo_cycle_title.value = demoSettings.demo_cycle_title || 'Training Demo Cycle';
    demoForm.demo_cycle_notes.value = demoSettings.demo_cycle_notes || '';
  }
}
async function loadContext() {
  const periodQuery = selectedPeriodId ? `?period_id=${encodeURIComponent(selectedPeriodId)}` : '';
  context = await api(PAY_VIEW === 'demo' ? '/pay/api/demo/context' : `/pay/api/context${periodQuery}`);
  if (!selectedPeriodId && context.current_period_id) selectedPeriodId = context.current_period_id;
  renderSummary();
  renderEntries();
  renderDailyTally();
  renderMileageEntrySelect();
  renderCommonPlaces();
  renderMileageForms();
  renderFundAllocationPanel();
  renderFundsAdmin();
  renderStubs();
  renderPayUsers();
  renderPayProfiles();
  fillMyPayProfileForm();
  renderInternalRoles();
  renderAdminCommonPlaces();
  renderDemoCycle();
  renderDemoFiles();
  renderDemoFeedback();
  renderIrsCandidates();
  fillSettings();
  syncMileageRateFromDate();
}
bind('periodSelect', 'change', async event => {
  selectedPeriodId = event.target.value || '';
  try { localStorage.setItem('paySelectedPeriodId', selectedPeriodId); } catch (_err) {}
  selectedEntryId = null;
  await loadContext();
});
bind('entryForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  data.period_id = context.period.id;
  data.display_name = data.display_name || (context.actor && (context.actor.display_name || context.actor.email)) || '';
  for (const key of ['hourly_rate','lost_wage_amount','hours','mileage_miles','mileage_rate','mileage_amount','rentals_amount','meals_amount','hotel_amount','miscellaneous_amount','president_diff_hours','weekly_basis_hours']) data[key] = Number(data[key] || 0);
  data.submitter_certified = !!event.target.querySelector('input[name="submitter_certified"]:checked');
  try { await api('/pay/api/entries', { method: 'POST', body: JSON.stringify(data) }); setText('entryStatus', 'Signed off and saved'); await loadContext(); }
  catch (err) { setText('entryStatus', err.message); }
});
bind('entryForm', 'change', event => {
  const stubForm = byId('stubForm');
  if (stubForm && activePayBasis() === 'commission') stubForm.payroll_month.value = previousPayrollMonthFor(currentEntryDateValue());
  balancePresidentDailyHours(event.target && event.target.name);
  syncPayProfileVisibility();
});
bind('entryForm', 'input', event => {
  if (event.target && (event.target.name === 'hours' || event.target.name === 'president_diff_hours')) {
    balancePresidentDailyHours(event.target.name);
  }
});
bind('stubForm', 'submit', async event => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target).entries());
  const fileInput = byId('stubFile');
  const file = fileInput && fileInput.files[0];
  if (!file) { setText('stubStatus', 'Pay stub is required'); return; }
  const body = {
    user_email: form.user_email,
    base_wage_input_type: form.base_wage_input_type,
    base_wage_amount: Number(form.base_wage_amount || 0),
    weekly_basis_hours: Number(form.weekly_basis_hours || 40),
    commission_month_1_amount: Number(form.commission_month_1_amount || 0),
    commission_month_2_amount: Number(form.commission_month_2_amount || 0),
    commission_month_3_amount: Number(form.commission_month_3_amount || 0),
    payroll_month: form.payroll_month,
    filename: file.name,
    content_type: file.type,
    content_base64: bytesToBase64(await file.arrayBuffer()),
    notes: form.notes,
  };
  try { await api('/pay/api/compensation-stubs', { method: 'POST', body: JSON.stringify(body) }); setText('stubStatus', 'Saved'); event.target.reset(); await loadContext(); }
  catch (err) { setText('stubStatus', err.message); }
});
bind('uploadReceiptBtn', 'click', async () => {
  const fileInput = byId('receiptFile');
  const file = fileInput && fileInput.files[0];
  if (!selectedEntryId || !file) return;
  try {
    await api(`/pay/api/entries/${selectedEntryId}/attachments`, { method: 'POST', body: JSON.stringify({ period_id: context.period.id, filename: file.name, content_type: file.type, content_base64: bytesToBase64(await file.arrayBuffer()) }) });
    await loadContext();
  } catch (err) { alert(err.message); }
});
bind('uploadTimesheetBtn', 'click', async () => {
  const fileInput = byId('timesheetFile');
  const file = fileInput && fileInput.files[0];
  if (!selectedEntryId || !file) { setText('fundAllocationStatus', 'Select an entry and choose a timesheet screenshot.'); return; }
  try {
    await api(`/pay/api/entries/${encodeURIComponent(selectedEntryId)}/attachments`, {
      method: 'POST',
      body: JSON.stringify({
        period_id: context.period.id,
        filename: file.name,
        content_type: file.type,
        attachment_type: 'timesheet_screenshot',
        content_base64: bytesToBase64(await file.arrayBuffer()),
      }),
    });
    setText('fundAllocationStatus', 'Timesheet screenshot attached');
    fileInput.value = '';
    await loadContext();
  } catch (err) { setText('fundAllocationStatus', err.message); }
});
bind('fundAllocationForm', 'submit', async event => {
  event.preventDefault();
  const form = event.target;
  const next = allocationPayloadFromForm(form);
  if (!next.fund_id) { setText('fundAllocationStatus', 'Select a fund.'); return; }
  const existing = currentFundAllocations()
    .filter(row => row.fund_id !== next.fund_id)
    .map(row => ({
      fund_id: row.fund_id,
      hours: Number(row.hours || 0),
      mileage_miles: Number(row.mileage_miles || 0),
      mileage_amount: Number(row.mileage_amount || 0),
      rentals_amount: Number(row.rentals_amount || 0),
      meals_amount: Number(row.meals_amount || 0),
      hotel_amount: Number(row.hotel_amount || 0),
      miscellaneous_amount: Number(row.miscellaneous_amount || 0),
      notes: row.notes || '',
    }));
  try {
    await saveFundAllocations([...existing, next]);
    setText('fundAllocationStatus', 'Fund allocation saved');
    form.reset();
  } catch (err) { setText('fundAllocationStatus', err.message); }
});
bind('mileageEntrySelect', 'change', event => {
  selectedEntryId = event.target.value || null;
  syncMileageFormFromEntry();
});
bind('date', 'change', syncMileageRateFromDate);
bind('date', 'input', syncMileageRateFromDate);
bind('addCommonPlaceBtn', 'click', () => {
  const select = byId('commonPlaceSelect');
  const value = select && select.value;
  addCommonPlaceToLocations(value);
});
bind('commonPlaceSelect', 'change', event => {
  addCommonPlaceToLocations(event.target.value);
  event.target.value = '';
});
bind('mileageForm', 'submit', async event => {
  event.preventDefault();
  const form = event.target;
  let locations = mileageLocationsFromInputs();
  if (locations.length < 2) { setText('mileageStatus', 'Enter at least an origin and destination.'); return; }
  const entry = selectedEntry();
  const signedInName = (context.actor && (context.actor.display_name || context.actor.email)) || '';
  const nameInput = form.elements.namedItem('name');
  const mileageName = (nameInput && nameInput.value.trim()) || (entry && (entry.display_name || entry.user_email)) || signedInName;
  const body = {
    period_id: context.period.id,
    name: mileageName,
    local_number: form.local_number.value.trim() || '3106',
    date: form.date.value,
    description: form.description.value.trim(),
    locations,
    rate: null,
  };
  try {
    const checked = await checkMileageAddresses({ quiet: true });
    locations = checked.locations || locations;
    let entryId = selectedEntryId;
    if (!entryId) {
      const entry = await api('/pay/api/entries', {
        method: 'POST',
        body: JSON.stringify({
          period_id: context.period.id,
          entry_date: body.date,
          display_name: body.name,
          local_number: body.local_number,
          lost_wage_input_type: 'hourly',
          lost_wage_amount: 0,
          hours: 0,
          notes: body.description,
        }),
      });
      entryId = entry.id;
      selectedEntryId = entryId;
    }
    const result = await api(`/pay/api/entries/${entryId}/mileage`, { method: 'POST', body: JSON.stringify(body) });
    setText('mileageStatus', `${result.filename || 'Mileage PDF'} attached | ${money(result.mileage_miles)} miles | ${currency(result.reimbursement)}`);
    await loadContext();
  } catch (err) { setText('mileageStatus', err.message); }
});
bind('mileageForm', 'reset', () => {
  setText('mileageStatus', '');
  setTimeout(syncMileageRateFromDate, 0);
});
document.querySelectorAll('#locations .address-input').forEach(input => wireAddressInput(input));
document.querySelectorAll('#locations .remove-location').forEach(button => button.addEventListener('click', () => button.parentElement.remove()));
const addLocationButton = document.querySelector('#mileageForm .add-location');
if (addLocationButton) addLocationButton.addEventListener('click', () => addMileageLocationField(''));
bind('checkMileageAddressesBtn', 'click', async () => {
  try { await checkMileageAddresses(); }
  catch (err) { setText('addressCheckStatus', err.message); setText('mileageStatus', err.message); }
});
async function deleteEntry(row) {
  if (!row || !row.id) return;
  const label = `${row.entry_date || 'this date'}${row.notes ? ' - ' + row.notes : ''}`;
  if (!confirm(`Delete this voucher entry?

${label}

This removes the entry and any receipts or mileage reports attached to it while the period is unlocked.`)) return;
  try {
    await api(`/pay/api/entries/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
    if (selectedEntryId === row.id) selectedEntryId = null;
    setText('entryStatus', 'Entry deleted');
    setText('mileageStatus', 'Entry deleted');
    await loadContext();
  } catch (err) {
    setText('entryStatus', err.message);
    setText('mileageStatus', err.message);
  }
}
function setFormValue(form, name, value) {
  if (form && form[name]) form[name].value = value == null ? '' : String(value);
}
function openEntryForEdit(row) {
  const form = byId('entryForm');
  if (!form || !row) return;
  selectedEntryId = row.id;
  setFormValue(form, 'entry_date', row.entry_date || '');
  setFormValue(form, 'hours', row.hours || '');
  setFormValue(form, 'meals_amount', row.meals_amount || '');
  setFormValue(form, 'hotel_amount', row.hotel_amount || '');
  setFormValue(form, 'rentals_amount', row.rentals_amount || '');
  setFormValue(form, 'miscellaneous_amount', row.miscellaneous_amount || '');
  setFormValue(form, 'local_number', row.local_number || '3106');
  setFormValue(form, 'address', row.address || '');
  setFormValue(form, 'president_diff_hours', row.president_diff_hours || '');
  balancePresidentDailyHours();
  setFormValue(form, 'notes', row.notes || '');
  const certify = form.querySelector('input[name="submitter_certified"]');
  if (certify) certify.checked = !!row.submitter_certified_at_utc;
  document.querySelectorAll('input[name="entryPick"]').forEach(input => { input.checked = input.value === String(row.id); });
  setText('entryStatus', `Editing ${row.entry_date || 'voucher entry'}. Sign off and save to update this voucher.`);
  syncMileageFormFromEntry();
  syncPayProfileVisibility();
  form.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const first = form.querySelector('input[name="entry_date"]');
  if (first) first.focus({ preventScroll: true });
}
function hideCorrectionPanel() {
  const panel = byId('correctionPanel');
  if (panel) panel.classList.add('hidden');
  setText('correctionStatus', 'Choose Edit Voucher on a row below.');
}
function openCorrectionForEntry(row) {
  const panel = byId('correctionPanel');
  const form = byId('correctionForm');
  if (!panel || !form || !row) return;
  selectedEntryId = row.id;
  panel.classList.remove('hidden');
  if (form.user_email) form.user_email.value = row.user_email || '';
  if (form.display_name) form.display_name.value = row.display_name || '';
  if (form.entry_date) form.entry_date.value = row.entry_date || '';
  if (form.local_number) form.local_number.value = row.local_number || '3106';
  if (form.address) form.address.value = row.address || '';
  for (const key of ['hours','mileage_miles','mileage_rate','mileage_amount','rentals_amount','meals_amount','hotel_amount','miscellaneous_amount']) {
    if (form[key]) form[key].value = '';
  }
  if (form.notes) form.notes.value = '';
  setText('correctionTitle', `Edit Voucher Additions: ${row.display_name || row.user_email || 'Member'} ${row.entry_date || ''}`.trim());
  setText('correctionStatus', 'Add only missing time, mileage, or expenses. Existing submitted values are not reduced here.');
  syncMileageFormFromEntry();
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
async function reviewEntry(entryId, reviewStatus, reviewNote) {
  try {
    await api(`/pay/api/entries/${encodeURIComponent(entryId)}/review`, {
      method: 'POST',
      body: JSON.stringify({ review_status: reviewStatus, review_note: reviewNote }),
    });
    setText('treasurerStatus', 'Review saved');
    await loadContext();
  } catch (err) {
    setText('treasurerStatus', err.message);
  }
}
bind('correctionForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  data.period_id = context.period.id;
  for (const key of ['hours','mileage_miles','mileage_rate','mileage_amount','rentals_amount','meals_amount','hotel_amount','miscellaneous_amount']) data[key] = Number(data[key] || 0);
  try {
    await api('/pay/api/entries/corrections', { method: 'POST', body: JSON.stringify(data) });
    setText('correctionStatus', 'Correction added');
    event.target.reset();
    hideCorrectionPanel();
    await loadContext();
  } catch (err) {
    setText('correctionStatus', err.message);
  }
});
bind('cancelCorrectionBtn', 'click', () => {
  const form = byId('correctionForm');
  if (form) form.reset();
  hideCorrectionPanel();
});
function renderLockSendResult(result) {
  const node = byId('treasurerStatus');
  if (!node) return;
  node.textContent = '';
  const status = document.createElement('span');
  status.textContent = result.signing_link ? 'Packet sent. President signing link: ' : 'Packet sent.';
  node.appendChild(status);
  if (result.signing_link) {
    const link = document.createElement('a');
    link.href = result.signing_link;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'Open DocuSeal';
    node.appendChild(document.createTextNode(' '));
    node.appendChild(link);
  }
  const excluded = result.excluded_entries || [];
  if (excluded.length) {
    const warning = document.createElement('span');
    warning.textContent = ` | ${excluded.length} needs-fix/rejected entr${excluded.length === 1 ? 'y was' : 'ies were'} excluded.`;
    node.appendChild(warning);
  }
}
bind('lockBtn', 'click', async event => {
  const button = event.currentTarget;
  const originalLabel = button ? button.textContent : 'Lock And Send';
  try {
    if (button) { button.disabled = true; button.textContent = 'Sending...'; }
    setText('treasurerStatus', 'Building packet and sending to DocuSeal...');
    const result = await api(`/pay/api/periods/${context.period.id}/lock`, { method: 'POST', body: JSON.stringify({}) });
    renderLockSendResult(result);
    await loadContext();
  }
  catch (err) { setText('treasurerStatus', `Lock/send failed: ${err.message}`); }
  finally { if (button) { button.disabled = false; button.textContent = originalLabel; } }
});
bind('revisionBtn', 'click', async () => {
  try { await api(`/pay/api/periods/${context.period.id}/revision`, { method: 'POST', body: JSON.stringify({}) }); setText('treasurerStatus', 'Revision opened'); await loadContext(); }
  catch (err) { setText('treasurerStatus', err.message); }
});
bind('settingsForm', 'submit', async event => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target).entries());
  const settings = { president_email: form.president_email, treasurer_emails: String(form.treasurer_emails || '').split(',').map(v => v.trim()).filter(Boolean) };
  try {
    await api('/pay/api/settings', { method: 'PUT', body: JSON.stringify(settings) });
    if (form.effective_date && form.target_weekly_amount) {
      await api('/pay/api/wage-scales', { method: 'POST', body: JSON.stringify({ effective_date: form.effective_date, weekly_basis_hours: Number(form.weekly_basis_hours || 40), target_weekly_amount: Number(form.target_weekly_amount), target_multiplier: 1.20 }) });
    }
    setText('settingsStatus', 'Saved');
    await loadContext();
  } catch (err) { setText('settingsStatus', err.message); }
});
bind('demoSettingsForm', 'submit', async event => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target).entries());
  const settings = {
    demo_mode_enabled: String(form.demo_mode_enabled || 'true') === 'true',
    demo_cycle_title: String(form.demo_cycle_title || '').trim(),
    demo_cycle_notes: String(form.demo_cycle_notes || '').trim(),
  };
  try {
    await api('/pay/api/demo/settings', { method: 'PUT', body: JSON.stringify(settings) });
    setText('demoSettingsStatus', 'Saved');
    await loadContext();
  } catch (err) { setText('demoSettingsStatus', err.message); }
});
async function generateDemoFiles() {
  const demoSettings = (context && (context.demo_settings || context.settings)) || {};
  try {
    const started = await api('/pay/api/demo/artifacts', {
      method: 'POST',
      body: JSON.stringify({
        demo_step: demoStepIndex,
        demo_cycle_title: demoSettings.demo_cycle_title || 'Training Demo Cycle',
      }),
    });
    let result = started;
    const jobId = started.job_id;
    if (jobId) {
      for (let attempt = 0; attempt < 300; attempt++) {
        const progress = result.progress || {};
        const suffix = progress.total ? ` (${progress.current || 0}/${progress.total})` : '';
        setText('demoFilesStatus', `${progress.message || result.message || result.status || 'Building demo packet'}${suffix}`);
        if (result.status === 'completed' || result.status === 'failed') break;
        await new Promise(resolve => setTimeout(resolve, 1500));
        result = await api(`/pay/api/demo/artifact-jobs/${encodeURIComponent(jobId)}`);
      }
    }
    if (result.status === 'failed') throw new Error(result.error || result.message || 'Demo packet failed');
    context.demo_artifacts = result.rows || [];
    renderDemoFiles();
    setText('demoFilesStatus', context.demo_artifacts.length ? 'Demo PDF packet ready' : 'No demo packet generated');
  } catch (err) {
    setText('demoFilesStatus', err.message);
  }
}
document.querySelectorAll('[data-demo-step]').forEach(button => {
  button.addEventListener('click', async () => {
    setDemoStep(button.dataset.demoStep);
    if (Number(button.dataset.demoStep) >= 4) await generateDemoFiles();
  });
});
bind('resetDemoBtn', 'click', () => setDemoStep(0));
bind('generateDemoFilesBtn', 'click', generateDemoFiles);
bind('demoFeedbackForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  const demoSettings = (context && (context.demo_settings || context.settings)) || {};
  data.demo_step = demoStepIndex;
  data.demo_cycle_title = demoSettings.demo_cycle_title || 'Training Demo Cycle';
  try {
    await api('/pay/api/demo/feedback', { method: 'POST', body: JSON.stringify(data) });
    setText('demoFeedbackStatus', 'Suggestion saved');
    event.target.reset();
    await loadContext();
  } catch (err) {
    setText('demoFeedbackStatus', err.message);
  }
});
bind('fundForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  try {
    await api('/pay/api/funds', { method: 'POST', body: JSON.stringify(data) });
    setText('fundStatus', 'Fund saved');
    event.target.reset();
    await loadContext();
  } catch (err) { setText('fundStatus', err.message); }
});
bind('fundLedgerForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  const fundId = data.fund_id || '';
  if (!fundId) { setText('fundStatus', 'Select a fund.'); return; }
  data.amount = Number(data.amount || 0);
  try {
    await api(`/pay/api/funds/${encodeURIComponent(fundId)}/ledger`, { method: 'POST', body: JSON.stringify(data) });
    setText('fundStatus', 'Ledger entry saved');
    event.target.reset();
    await loadContext();
  } catch (err) { setText('fundStatus', err.message); }
});
bind('fundPacketForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  const fundId = data.fund_id || '';
  if (!fundId) { setText('fundPacketStatus', 'Select a fund.'); return; }
  try {
    const packet = await api(`/pay/api/funds/${encodeURIComponent(fundId)}/packets`, {
      method: 'POST',
      body: JSON.stringify({ period_start: data.period_start, period_end: data.period_end }),
    });
    setText('fundPacketStatus', `Packet generated: ${currency(packet.total_amount)} (${packet.support_document_count || 0} support docs)`);
    await loadContext();
  } catch (err) { setText('fundPacketStatus', err.message); }
});
bind('payUserForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  try { await api('/pay/api/users', { method: 'POST', body: JSON.stringify(data) }); setText('payUserStatus', 'Saved'); event.target.reset(); await loadContext(); }
  catch (err) { setText('payUserStatus', err.message); }
});
bind('myPayProfileForm', 'change', syncPayProfileVisibility);
bind('myPayProfileForm', 'submit', async event => {
  event.preventDefault();
  normalizeProfileWageInputType(event.target);
  const data = Object.fromEntries(new FormData(event.target).entries());
  for (const key of ['base_wage_amount','weekly_basis_hours','commission_month_1_amount','commission_month_2_amount','commission_month_3_amount']) data[key] = Number(data[key] || 0);
  data.status = 'active';
  try {
    await api('/pay/api/profiles', { method: 'POST', body: JSON.stringify(data) });
    setText('myPayProfileStatus', data.pending_wage_approval ? 'Address saved; wage change pending treasurer approval' : 'Saved');
    await loadContext();
  } catch (err) {
    setText('myPayProfileStatus', err.message);
  }
});
bind('payProfileForm', 'submit', async event => {
  event.preventDefault();
  normalizeProfileWageInputType(event.target);
  const data = Object.fromEntries(new FormData(event.target).entries());
  for (const key of ['base_wage_amount','weekly_basis_hours','commission_month_1_amount','commission_month_2_amount','commission_month_3_amount']) data[key] = Number(data[key] || 0);
  try {
    await api('/pay/api/profiles', { method: 'POST', body: JSON.stringify(data) });
    setText('payProfileStatus', 'Pay profile saved');
    await loadContext();
  } catch (err) {
    setText('payProfileStatus', err.message);
  }
});
async function reviewProfileChange(requestId, approved) {
  if (!requestId) return;
  const reviewNote = approved ? '' : prompt('Reason for rejecting this wage change?') || '';
  try {
    await api(`/pay/api/profiles/changes/${encodeURIComponent(requestId)}/review`, {
      method: 'POST',
      body: JSON.stringify({ approved, review_note: reviewNote }),
    });
    setText('payProfileStatus', approved ? 'Wage change approved' : 'Wage change rejected');
    await loadContext();
  } catch (err) {
    setText('payProfileStatus', err.message);
  }
}
async function removePayProfile(email, name) {
  if (!email) return;
  const label = name || email;
  if (!confirm(`Remove ${label} from People and Pay Profiles? Historical submitted entries will stay on file.`)) return;
  try {
    await api(`/pay/api/profiles/${encodeURIComponent(email)}`, { method: 'DELETE' });
    setText('payProfileStatus', 'Person removed');
    await loadContext();
  } catch (err) {
    setText('payProfileStatus', err.message);
  }
}
bind('commonPlaceForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  commonPlacesDraft.push({
    label: String(data.label || '').trim(),
    address: String(data.address || '').trim(),
  });
  try {
    await saveCommonPlaces('Location saved');
    event.target.reset();
  } catch (err) {
    setText('commonPlaceStatus', err.message);
  }
});
bind('importLicensedUsersBtn', 'click', async () => {
  try {
    const preview = await api('/pay/api/internal-users/import', { method: 'POST', body: JSON.stringify({ limit: 999, confirm: false }) });
    const count = Number(preview.candidate_count || 0);
    const skipped = Number(preview.skipped_count || 0);
    if (!count) {
      setText('internalRoleStatus', `No new paid users to import${skipped ? `; skipped ${skipped}` : ''}`);
      return;
    }
    if (!confirm(`Import ${count} Microsoft paid user${count === 1 ? '' : 's'} as expense-only profiles? ${skipped ? `${skipped} will be skipped.` : ''}`)) {
      setText('internalRoleStatus', 'Import cancelled');
      return;
    }
    const result = await api('/pay/api/internal-users/import', { method: 'POST', body: JSON.stringify({ limit: 999, confirm: true }) });
    const imported = Number(result.imported_count || 0);
    setText('internalRoleStatus', `Imported ${imported} paid Microsoft user${imported === 1 ? '' : 's'}${skipped ? `, skipped ${skipped}` : ''}`);
    await loadContext();
  } catch (err) {
    setText('internalRoleStatus', err.message);
  }
});
bind('internalRoleSearchForm', 'submit', async event => {
  event.preventDefault();
  const form = Object.fromEntries(new FormData(event.target).entries());
  try {
    const result = await api(`/pay/api/directory/users?search=${encodeURIComponent(form.search || '')}&limit=10`);
    setText('internalRoleStatus', result.warning || `${result.count} match${result.count === 1 ? '' : 'es'}`);
    renderInternalRoleSearch(result.rows || []);
  } catch (err) {
    setText('internalRoleStatus', err.message);
  }
});
bind('irsSyncBtn', 'click', async () => {
  try { const result = await api('/pay/api/irs-rates/sync', { method: 'POST', body: JSON.stringify({}) }); setText('irsStatus', result.detected.length ? `${result.detected.length} rate staged` : 'No new IRS rates'); await loadContext(); }
  catch (err) { setText('irsStatus', err.message); }
});
async function assignInternalRole(index, role) {
  const row = internalRoleSearchRows[index];
  if (!row) return;
  const email = row.email || row.user_principal_name;
  if (!email) {
    setText('internalRoleStatus', 'Selected user has no email or UPN.');
    return;
  }
  const payload = {
    principal_id: row.principal_id || null,
    principal_email: email,
    principal_display_name: row.display_name || email,
    role,
    status: 'active'
  };
  try {
    await api('/pay/api/internal-roles', { method: 'POST', body: JSON.stringify(payload) });
    setText('internalRoleStatus', 'Saved');
    await loadContext();
  } catch (err) {
    setText('internalRoleStatus', err.message);
  }
}
async function removeInternalRole(assignmentId) {
  if (!assignmentId) return;
  try {
    await api(`/pay/api/internal-roles/${encodeURIComponent(assignmentId)}`, { method: 'DELETE' });
    setText('internalRoleStatus', 'Removed');
    await loadContext();
  } catch (err) {
    setText('internalRoleStatus', err.message);
  }
}
async function approveIrsRate(candidateId) {
  try { const result = await api(`/pay/api/irs-rates/${candidateId}/approve`, { method: 'POST', body: JSON.stringify({}) }); setText('irsStatus', `Approved ${result.rate_year}: ${rateCurrency(result.active_rate, 3)}`); await loadContext(); }
  catch (err) { setText('irsStatus', err.message); }
}
loadContext().catch(err => { const main = document.querySelector('.main'); if (main) main.innerHTML = '<section class="panel"><h2>Access</h2><p>' + err.message + '</p></section>'; });
  </script>
</body>
</html>
"""
    return (
        html.replace("__PAGE_TITLE__", escape(page_title))
        .replace("__NAV__", nav_html)
        .replace("__ACTOR_NAME__", actor_name)
        .replace("__ROLE_LABEL__", role_label)
        .replace("__CONTENT__", content_html)
        .replace("__VIEW__", normalized_view)
    )


def _render_pay_start_page() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lost Wage Portal</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f6f8;
      --panel: #ffffff;
      --text: #202a34;
      --muted: #586675;
      --line: #d8e0e7;
      --accent: #155e75;
      --accent-dark: #10495b;
      --warm: #7a4b12;
      --warm-bg: #fff7e8;
      --ok: #1f6f49;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, sans-serif;
    }
    main {
      width: min(1080px, 100%);
      margin: 0 auto;
      padding: 22px 18px 44px;
      display: grid;
      gap: 18px;
    }
    .top-menu {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .top-menu a {
      color: var(--accent);
      font-weight: 700;
      font-size: 14px;
      text-decoration: none;
      border: 1px solid #c6d5dd;
      border-radius: 4px;
      padding: 8px 10px;
      background: #fff;
    }
    .top-menu a[aria-current="page"] {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .hero {
      min-height: 260px;
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(280px, 0.75fr);
      gap: 22px;
      align-items: stretch;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
    }
    .hero-copy {
      padding: clamp(22px, 4vw, 42px);
      display: grid;
      align-content: center;
      gap: 16px;
    }
    .eyebrow {
      margin: 0;
      color: var(--accent);
      font-weight: 800;
      font-size: 13px;
      text-transform: uppercase;
    }
    h1 {
      margin: 0;
      font-size: clamp(30px, 4vw, 48px);
      line-height: 1.05;
      letter-spacing: 0;
    }
    p {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
      font-size: 16px;
    }
    .hero-aside {
      background: #eef6f8;
      border-left: 1px solid var(--line);
      padding: 24px;
      display: grid;
      align-content: center;
      gap: 14px;
    }
    .status-line {
      display: flex;
      gap: 10px;
      align-items: flex-start;
      color: var(--text);
      font-weight: 700;
    }
    .status-dot {
      width: 10px;
      height: 10px;
      margin-top: 5px;
      border-radius: 50%;
      background: var(--ok);
      flex: 0 0 10px;
    }
    .signin-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .signin-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 18px;
      display: grid;
      gap: 12px;
      min-height: 188px;
      align-content: space-between;
    }
    .signin-card h2 {
      margin: 0;
      font-size: 20px;
      letter-spacing: 0;
    }
    .signin-card p {
      font-size: 14px;
    }
    .button-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      width: fit-content;
      max-width: 100%;
      padding: 0 14px;
      border-radius: 4px;
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      font-weight: 800;
      white-space: normal;
      text-align: center;
    }
    .button-link:hover, .button-link:focus {
      background: var(--accent-dark);
      border-color: var(--accent-dark);
    }
    .button-link.secondary {
      background: #fff;
      color: var(--accent);
    }
    .notice {
      background: var(--warm-bg);
      border: 1px solid #efd6aa;
      border-left: 5px solid var(--warm);
      border-radius: 6px;
      padding: 14px 16px;
      color: #3c352b;
      font-size: 14px;
      line-height: 1.5;
    }
    .notice strong { color: var(--warm); }
    @media (max-width: 780px) {
      main { padding: 14px 10px 32px; }
      .hero, .signin-grid { grid-template-columns: 1fr; }
      .hero-aside { border-left: 0; border-top: 1px solid var(--line); }
      .button-link { width: 100%; }
    }
  </style>
</head>
<body>
  <main>
    <nav class="top-menu" aria-label="Page navigation">
      <a href="/officers">Main tracker</a>
      <a href="/forms">Hosted forms</a>
      <a href="/pay/start" aria-current="page">Pay Portal</a>
    </nav>
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">CWA Local 3106</p>
        <h1>Lost Wage Portal</h1>
        <p>Submit lost time, mileage, pay proof, and receipt files from one private workspace.</p>
      </div>
      <div class="hero-aside" aria-label="Portal status">
        <div class="status-line"><span class="status-dot"></span><span>Private access required</span></div>
        <p>Choose the sign-in path that matches the account you were given.</p>
      </div>
    </section>
    <section class="signin-grid" aria-label="Sign in choices">
      <div class="signin-card">
        <div>
          <h2>Officers and admins</h2>
          <p>Use your Microsoft account for the officer workspace and treasurer review tools.</p>
        </div>
        <a class="button-link" href="/auth/login?next=/pay">Sign in with Microsoft</a>
      </div>
      <div class="signin-card">
        <div>
          <h2>Approved pay users</h2>
          <p>Use this path if the local gave you external access for your own voucher entries.</p>
        </div>
        <a class="button-link secondary" href="/auth/steward/login?next=/pay">Sign in as pay user</a>
      </div>
    </section>
    <div class="notice"><strong>Need access?</strong> Contact the treasurer or an officer. Pay information, uploaded files, and voucher packets are not public.</div>
  </main>
</body>
</html>
"""


@router.get("/pay/start", response_class=HTMLResponse)
async def pay_start_page(request: Request):
    if not request.app.state.cfg.pay_portal.enabled:
        raise HTTPException(status_code=503, detail="pay portal is disabled")
    return HTMLResponse(_render_pay_start_page())


@router.get("/pay", response_class=HTMLResponse)
async def pay_page(request: Request):
    actor = await _current_pay_actor(request)
    if not actor:
        return RedirectResponse(url="/pay/start", status_code=303)
    landing = "treasurer" if actor.can_view_all else "entry"
    return RedirectResponse(url=f"/pay/{landing}", status_code=303)


@router.get("/pay/{view}", response_class=HTMLResponse)
async def pay_view_page(view: str, request: Request):
    normalized_view = str(view or "").strip().lower()
    actor = await _current_pay_actor(request)
    if not actor:
        return RedirectResponse(url="/pay/start", status_code=303)
    if normalized_view == "president":
        return RedirectResponse(url="/pay/entry", status_code=303)
    if normalized_view not in _PAY_VIEW_TITLES:
        raise HTTPException(status_code=404, detail="pay page not found")
    if normalized_view == "treasurer" and not actor.can_view_all:
        raise _forbidden("officer access required")
    if normalized_view == "admin" and not actor.can_lock:
        raise _forbidden("treasurer access required")
    return HTMLResponse(_render_pay_workspace_page(view=normalized_view, actor=actor))


async def _pay_period_choices(db: Db, *, actor: PayActor) -> list[dict[str, object]]:
    if actor.can_view_all:
        rows = await db.fetchall(
            """SELECT p.id, p.period_start, p.period_end, p.status, p.revision,
                      COUNT(e.id) AS entry_count
               FROM pay_periods p
               LEFT JOIN pay_entries e ON e.period_id = p.id
               GROUP BY p.id, p.period_start, p.period_end, p.status, p.revision
               ORDER BY p.period_start DESC, p.revision DESC
               LIMIT 26"""
        )
    else:
        rows = await db.fetchall(
            """SELECT p.id, p.period_start, p.period_end, p.status, p.revision,
                      COUNT(e.id) AS entry_count
               FROM pay_periods p
               LEFT JOIN pay_entries e ON e.period_id = p.id AND e.user_email=?
               GROUP BY p.id, p.period_start, p.period_end, p.status, p.revision
               ORDER BY p.period_start DESC, p.revision DESC
               LIMIT 26""",
            (actor.email,),
        )
    return [
        {
            "id": row[0],
            "period_start": row[1],
            "period_end": row[2],
            "status": row[3],
            "revision": row[4],
            "entry_count": row[5],
        }
        for row in rows
    ]


@router.get("/pay/api/context")
async def pay_context(request: Request):
    actor = await _require_pay_actor(request)
    db: Db = request.app.state.db
    current_period = await ensure_pay_period(db)
    requested_period_id = str(getattr(request, "query_params", {}).get("period_id", "") or "").strip()
    period = current_period
    if requested_period_id and requested_period_id != str(current_period["id"]):
        row = await db.fetchone(
            "SELECT id, period_start, period_end, status, revision, locked_at_utc, sharepoint_folder_path, created_at_utc, updated_at_utc FROM pay_periods WHERE id=?",
            (requested_period_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="pay period not found")
        period = {
            "id": row[0],
            "period_start": row[1],
            "period_end": row[2],
            "status": row[3],
            "revision": row[4],
            "locked_at_utc": row[5],
            "sharepoint_folder_path": row[6],
            "created_at_utc": row[7],
            "updated_at_utc": row[8],
        }
    periods = await _pay_period_choices(db, actor=actor)
    entries = await list_entries(db, period_id=str(period["id"]), actor=actor)
    attachments = await list_attachments(db, period_id=str(period["id"]), actor=actor)
    compensation_stubs = await list_compensation_stubs(db, actor=actor)
    settings = await _pay_settings_for_request(request)
    funds = await list_pay_funds(
        db,
        include_inactive=actor.can_lock,
        include_financials=actor.can_lock,
        fica_rate=fund_fica_rate_from_settings(settings),
    )
    fund_allocations = await list_pay_fund_allocations(db, actor=actor, period_id=str(period["id"]))
    fund_attachment_links = await list_pay_fund_attachment_links(db, actor=actor, period_id=str(period["id"]))
    current_profile = await pay_profile_by_email(db, email=actor.email, active_only=True)
    return {
        "actor": {
            "email": actor.email,
            "display_name": actor.display_name,
            "role": actor.role,
            "can_view_all": actor.can_view_all,
            "can_edit_all": actor.can_edit_all,
            "can_lock": actor.can_lock,
            "is_guest": actor.is_guest,
            "is_president": actor.is_president,
        },
        "period": period,
        "current_period_id": current_period["id"],
        "periods": periods,
        "entries": entries,
        "attachments": attachments,
        "compensation_stubs": compensation_stubs,
        "funds": funds,
        "fund_allocations": fund_allocations,
        "fund_attachment_links": fund_attachment_links,
        "fund_packets": await list_pay_fund_packets(db) if actor.can_lock else [],
        "settings": settings,
        "demo_settings": await pay_demo_settings(db),
        "pay_profile": current_profile,
        "pay_profile_change_requests": await list_pay_profile_change_requests(
            db, email=None if actor.can_lock else actor.email, pending_only=not actor.can_lock
        ),
        "pay_users": await list_pay_users(db) if actor.can_lock else [],
        "pay_profiles": await list_pay_profiles(db) if actor.can_lock else [],
        "internal_roles": await list_internal_role_assignments(db) if actor.can_lock else [],
        "wage_scales": await list_wage_scales(db) if actor.can_lock else [],
        "irs_rate_candidates": await list_irs_rate_candidates(db) if actor.can_lock else [],
        "demo_feedback": await list_pay_demo_feedback(db) if actor.can_lock else [],
    }


@router.get("/pay/api/demo/context")
async def pay_demo_context(request: Request):
    actor = await _require_pay_actor(request)
    db: Db = request.app.state.db
    start, end = current_period_bounds()
    settings = await pay_settings(db, pay_cfg=request.app.state.cfg.pay_portal)
    demo_settings = await pay_demo_settings(db)
    return {
        "actor": {
            "email": actor.email,
            "display_name": actor.display_name,
            "role": actor.role,
            "can_view_all": actor.can_view_all,
            "can_edit_all": actor.can_edit_all,
            "can_lock": actor.can_lock,
            "is_guest": actor.is_guest,
            "is_president": actor.is_president,
        },
        "period": {
            "id": "demo",
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "status": "demo",
            "revision": 0,
        },
        "entries": [],
        "attachments": [],
        "compensation_stubs": [],
        "funds": [],
        "fund_allocations": [],
        "fund_attachment_links": [],
        "fund_packets": [],
        "settings": settings,
        "demo_settings": demo_settings,
        "pay_profile": None,
        "pay_profile_change_requests": [],
        "pay_users": [],
        "pay_profiles": [],
        "internal_roles": [],
        "wage_scales": [],
        "irs_rate_candidates": [],
        "demo_feedback": await list_pay_demo_feedback(db) if actor.can_lock else [],
        "demo_artifacts": _demo_artifact_rows_for_response(request, actor),
    }


@router.get("/pay/api/directory/users", response_model=DirectoryUserSearchResponse)
async def pay_directory_users(request: Request, search: str = "", limit: int = 10):
    await _require_treasurer(request)
    return await search_directory_users_for_request(request, search=search, limit=limit)


@router.get("/pay/api/funds")
async def pay_funds(request: Request):
    await _require_treasurer(request)
    db: Db = request.app.state.db
    settings = await _pay_settings_for_request(request)
    return {
        "rows": await list_pay_funds(
            db,
            include_inactive=True,
            include_financials=True,
            fica_rate=fund_fica_rate_from_settings(settings),
        )
    }


@router.post("/pay/api/funds")
async def save_pay_fund(body: PayFundUpsertRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await upsert_pay_fund(
            request.app.state.db,
            fund_id=body.id,
            fund_type=body.fund_type,
            name=body.name,
            status=body.status,
            local_number=body.local_number,
            description=body.description,
            actor_email=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/funds/{fund_id}/ledger")
async def save_pay_fund_ledger_entry(fund_id: str, body: PayFundLedgerEntryRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await add_pay_fund_ledger_entry(
            request.app.state.db,
            fund_id=fund_id,
            ledger_type=body.ledger_type,
            amount=body.amount,
            effective_date=body.effective_date,
            reference=body.reference,
            notes=body.notes,
            actor_email=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/funds/{fund_id}/packets")
async def generate_pay_fund_packet_route(fund_id: str, body: PayFundPacketRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await generate_pay_fund_packet(
            request.app.state.db,
            cfg=request.app.state.cfg,
            fund_id=fund_id,
            actor=actor,
            period_start=body.period_start,
            period_end=body.period_end,
            graph=getattr(request.app.state, "graph", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/pay/api/funds/packets/{packet_id}/workbook")
async def download_pay_fund_packet_workbook(packet_id: str, request: Request):
    await _require_treasurer(request)
    try:
        packet = await pay_fund_packet_by_id(request.app.state.db, packet_id=packet_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    workbook_path = str(packet.get("workbook_path") or "")
    if not workbook_path:
        raise HTTPException(status_code=404, detail="fund packet workbook not found")
    return FileResponse(
        workbook_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(workbook_path).name,
    )


@router.post("/pay/api/entries/{entry_id}/fund-allocations")
async def save_pay_entry_fund_allocations(entry_id: str, body: PayFundAllocationsRequest, request: Request):
    actor = await _require_pay_actor(request)
    try:
        return {
            "rows": await save_pay_fund_allocations_for_entry(
                request.app.state.db,
                entry_id=entry_id,
                actor=actor,
                allocations=[row.model_dump() for row in body.allocations],
            )
        }
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/attachments/{attachment_id}/fund-links")
async def save_pay_attachment_fund_link(attachment_id: str, body: PayFundAttachmentLinkRequest, request: Request):
    actor = await _require_pay_actor(request)
    try:
        return await link_pay_attachment_to_fund(
            request.app.state.db,
            attachment_id=attachment_id,
            fund_id=body.fund_id,
            allocation_id=body.allocation_id,
            notes=body.notes,
            actor=actor,
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/pay/api/profiles")
async def pay_profiles(request: Request):
    await _require_treasurer(request)
    return {"rows": await list_pay_profiles(request.app.state.db)}


@router.get("/pay/api/profiles/{email}")
async def pay_profile(email: str, request: Request):
    actor = await _require_pay_actor(request)
    normalized_email = normalize_email(email)
    if not actor.can_lock and normalized_email != actor.email:
        raise _forbidden("treasurer access required")
    profile = await pay_profile_by_email(request.app.state.db, email=normalized_email)
    if not profile:
        raise HTTPException(status_code=404, detail="pay profile not found")
    return profile


@router.post("/pay/api/entries")
async def save_pay_entry(body: PayEntryUpsertRequest, request: Request):
    actor = await _require_pay_actor(request)
    db: Db = request.app.state.db
    if body.period_id:
        period_id = body.period_id
    else:
        start, _ = current_period_bounds()
        period_id = str((await ensure_pay_period(db, for_date=start))["id"])
    try:
        return await upsert_entry(
            db,
            period_id=period_id,
            actor=actor,
            data=body.model_dump(),
            pay_cfg=request.app.state.cfg.pay_portal,
            require_submitter_certification=True,
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/pay/api/entries/{entry_id}")
async def delete_pay_entry_route(entry_id: str, request: Request):
    actor = await _require_pay_actor(request)
    try:
        return await delete_pay_entry(request.app.state.db, entry_id=entry_id, actor=actor)
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/compensation-stubs")
async def upload_pay_compensation_stub(body: PayCompensationStubRequest, request: Request):
    actor = await _require_pay_actor(request)
    try:
        content = decode_content_base64(body.content_base64)
        return await store_compensation_stub(
            request.app.state.db,
            cfg=request.app.state.cfg,
            actor=actor,
            user_email=body.user_email,
            base_wage_input_type=body.base_wage_input_type,
            base_wage_amount=body.base_wage_amount,
            weekly_basis_hours=body.weekly_basis_hours,
            commission_month_1_amount=body.commission_month_1_amount,
            commission_month_2_amount=body.commission_month_2_amount,
            commission_month_3_amount=body.commission_month_3_amount,
            payroll_month=body.payroll_month,
            filename=body.filename,
            content_type=body.content_type,
            content=content,
            notes=body.notes,
            scan=True,
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/pay/api/entries/{entry_id}/review")
async def review_pay_entry_route(entry_id: str, body: PayEntryReviewRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await review_pay_entry(
            request.app.state.db,
            entry_id=entry_id,
            actor=actor,
            review_status=body.review_status,
            review_note=body.review_note,
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/entries/corrections")
async def create_pay_entry_correction_route(body: PayEntryCorrectionRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await create_pay_entry_correction(
            request.app.state.db,
            period_id=body.period_id,
            actor=actor,
            data=body.model_dump(),
            pay_cfg=request.app.state.cfg.pay_portal,
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/entries/{entry_id}/attachments")
async def upload_pay_attachment(entry_id: str, body: PayAttachmentUploadRequest, request: Request):
    actor = await _require_pay_actor(request)
    try:
        content = decode_content_base64(body.content_base64)
        return await store_attachment(
            request.app.state.db,
            cfg=request.app.state.cfg,
            period_id=body.period_id,
            entry_id=entry_id,
            actor=actor,
            attachment_type=body.attachment_type,
            filename=body.filename,
            content_type=body.content_type,
            content=content,
            scan=True,
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/pay/api/attachments/{attachment_id}/download")
async def download_pay_attachment(attachment_id: str, request: Request):
    actor = await _require_pay_actor(request)
    try:
        attachment = await attachment_for_actor(request.app.state.db, attachment_id=attachment_id, actor=actor)
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    local_path = str(attachment.get("local_path") or "")
    if not local_path:
        raise HTTPException(status_code=404, detail="attachment file not found")
    return FileResponse(
        local_path,
        media_type=str(attachment.get("content_type") or "application/octet-stream"),
        filename=str(attachment.get("filename") or "attachment"),
    )


@router.delete("/pay/api/attachments/{attachment_id}")
async def delete_pay_attachment(attachment_id: str, request: Request):
    actor = await _require_pay_actor(request)
    try:
        return await remove_mileage_attachment(request.app.state.db, attachment_id=attachment_id, actor=actor)
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/mileage/check-addresses")
async def check_pay_mileage_addresses(body: PayMileageAddressCheckRequest, request: Request):
    await _require_pay_actor(request)
    try:
        return validate_mileage_locations(
            google_maps_api_key=request.app.state.cfg.pay_portal.google_maps_api_key,
            locations=body.locations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/pay/api/entries/{entry_id}/mileage")
async def create_pay_mileage(entry_id: str, body: PayMileageRequest, request: Request):
    actor = await _require_pay_actor(request)
    try:
        mileage_name = str(body.name or "").strip() or str(actor.display_name or actor.email or "Pay User").strip()
        return await create_mileage_attachment(
            db=request.app.state.db,
            cfg=request.app.state.cfg,
            period_id=body.period_id,
            entry_id=entry_id,
            actor=actor,
            name=mileage_name,
            local_number=body.local_number or "3106",
            date_str=body.date,
            description=body.description,
            locations=body.locations,
            rate_text=body.rate,
            graph=getattr(request.app.state, "graph", None),
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/pay/api/settings")
async def update_pay_settings(body: PaySettingsUpdateRequest, request: Request):
    actor = await _require_treasurer(request)
    payload: dict[str, object] = {}
    if body.president_email is not None:
        payload["president_email"] = body.president_email.strip()
    if body.treasurer_emails is not None:
        payload["treasurer_emails"] = [email.strip() for email in body.treasurer_emails if email.strip()]
    if body.irs_rates is not None:
        payload["irs_rates"] = body.irs_rates
    if body.common_places is not None:
        payload["common_places"] = body.common_places
    return await save_pay_settings(
        request.app.state.db,
        setting=payload,
        updated_by=actor.email,
        pay_cfg=request.app.state.cfg.pay_portal,
    )


@router.put("/pay/api/demo/settings")
async def update_pay_demo_settings(body: PayDemoSettingsUpdateRequest, request: Request):
    actor = await _require_treasurer(request)
    payload: dict[str, object] = {}
    if body.demo_mode_enabled is not None:
        payload["demo_mode_enabled"] = bool(body.demo_mode_enabled)
    if body.demo_cycle_title is not None:
        payload["demo_cycle_title"] = body.demo_cycle_title
    if body.demo_cycle_notes is not None:
        payload["demo_cycle_notes"] = body.demo_cycle_notes
    return await save_pay_demo_settings(request.app.state.db, setting=payload, updated_by=actor.email)


@router.post("/pay/api/demo/artifacts")
async def generate_pay_demo_output_files(body: PayDemoArtifactRequest, request: Request):
    actor = await _require_pay_actor(request)
    settings = await pay_demo_settings(request.app.state.db)
    demo_enabled = settings.get("demo_mode_enabled")
    if demo_enabled is False or str(demo_enabled).lower() == "false":
        raise HTTPException(status_code=400, detail="demo mode is disabled")
    start, end = current_period_bounds()
    job_id = uuid4().hex
    now = time.time()
    with _DEMO_JOBS_LOCK:
        _DEMO_JOBS[job_id] = {
            "job_id": job_id,
            "actor_email": actor.email,
            "status": "queued",
            "message": "Queued demo packet",
            "progress": {"stage": "queued", "current": 0, "total": 1, "message": "Queued demo packet"},
            "rows": [],
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
    _DEMO_JOB_EXECUTOR.submit(
        _run_demo_artifact_job,
        job_id=job_id,
        cfg=request.app.state.cfg,
        graph=getattr(request.app.state, "graph", None),
        actor=actor,
        pay_settings_payload=await pay_settings(request.app.state.db, pay_cfg=request.app.state.cfg.pay_portal),
        demo_step=body.demo_step or 0,
        demo_cycle_title=body.demo_cycle_title or str(settings.get("demo_cycle_title") or "Training Demo Cycle"),
        period_start=start.isoformat(),
        period_end=end.isoformat(),
    )
    return _demo_job_snapshot(job_id, actor=actor, request=request)


@router.get("/pay/api/demo/artifact-jobs/{job_id}")
async def pay_demo_artifact_job(job_id: str, request: Request):
    actor = await _require_pay_actor(request)
    return _demo_job_snapshot(job_id, actor=actor, request=request)


@router.get("/pay/api/demo/artifacts/{filename}")
async def download_pay_demo_output_file(filename: str, request: Request):
    actor = await _require_pay_actor(request)
    try:
        path = pay_demo_artifact_path(
            data_root=request.app.state.cfg.data_root,
            actor=actor,
            filename=filename,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


@router.post("/pay/api/demo/feedback")
async def submit_pay_demo_feedback(body: PayDemoFeedbackRequest, request: Request):
    actor = await _require_pay_actor(request)
    settings = await pay_demo_settings(request.app.state.db)
    demo_enabled = settings.get("demo_mode_enabled")
    if demo_enabled is False or str(demo_enabled).lower() == "false":
        raise HTTPException(status_code=400, detail="demo mode is disabled")
    try:
        return await create_pay_demo_feedback(
            request.app.state.db,
            actor=actor,
            screen=body.screen,
            category=body.category,
            demo_step=body.demo_step,
            demo_cycle_title=body.demo_cycle_title or str(settings.get("demo_cycle_title") or "Training Demo Cycle"),
            comment=body.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/pay/api/demo/feedback/{feedback_id}")
async def update_pay_demo_feedback(feedback_id: int, body: PayDemoFeedbackStatusRequest, request: Request):
    await _require_treasurer(request)
    try:
        return await update_pay_demo_feedback_status(
            request.app.state.db,
            feedback_id=feedback_id,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/irs-rates/sync")
async def sync_pay_irs_rates(request: Request):
    actor = await _require_treasurer(request)
    result = await sync_irs_mileage_rate_candidates(
        request.app.state.db,
        pay_cfg=request.app.state.cfg.pay_portal,
    )
    return {**result, "actor": actor.email}


@router.post("/pay/api/irs-rates/{candidate_id}/approve")
async def approve_pay_irs_rate(candidate_id: int, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await approve_irs_rate_candidate(
            request.app.state.db,
            candidate_id=candidate_id,
            actor=actor.email,
            pay_cfg=request.app.state.cfg.pay_portal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/internal-users/import")
async def import_pay_internal_users(body: PayInternalUserImportRequest, request: Request):
    actor = await _require_treasurer(request)
    graph = getattr(request.app.state, "graph", None)
    if graph is None or not hasattr(graph, "list_licensed_directory_users"):
        raise HTTPException(status_code=503, detail="Microsoft licensed-user import is unavailable")
    try:
        rows = graph.list_licensed_directory_users(limit=body.limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    candidates: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for row in rows:
        email = normalize_email(getattr(row, "email", None) or getattr(row, "user_principal_name", None))
        display_name = str(getattr(row, "display_name", None) or email).strip()
        principal_id = str(getattr(row, "id", "") or "").strip() or None
        if not email:
            skipped.append({"email": "", "reason": "missing email"})
            continue
        existing = await pay_profile_by_email(request.app.state.db, email=email)
        if existing:
            skipped.append({"email": email, "reason": "profile exists"})
            continue
        candidates.append({"email": email, "display_name": display_name, "principal_id": principal_id})

    if not body.confirm:
        return {
            "preview": True,
            "candidate_count": len(candidates),
            "skipped_count": len(skipped),
            "candidates": candidates,
            "skipped": skipped,
            "imported_count": 0,
            "imported": [],
        }

    imported: list[dict[str, object]] = []
    for candidate in candidates:
        saved = await upsert_pay_profile(
            request.app.state.db,
            principal_id=str(candidate.get("principal_id") or "").strip() or None,
            principal_email=str(candidate["email"]),
            principal_display_name=str(candidate.get("display_name") or candidate["email"]),
            pay_basis="expense_only",
            base_wage_input_type="hourly",
            base_wage_amount=0,
            weekly_basis_hours=40,
            commission_month_1_amount=0,
            commission_month_2_amount=0,
            commission_month_3_amount=0,
            status="active",
            notes="Auto-imported from Microsoft paid license roster. Add wages before reimbursing lost time.",
            updated_by=actor.email,
        )
        imported.append(saved)

    return {
        "preview": False,
        "candidate_count": len(candidates),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "imported": imported,
        "skipped": skipped,
    }


@router.post("/pay/api/users")
async def save_pay_user(body: PayUserUpsertRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await upsert_pay_user(
            request.app.state.db,
            email=body.email,
            display_name=body.display_name,
            role=body.role,
            status=body.status,
            actor=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/internal-roles")
async def save_pay_internal_role(body: InternalRoleAssignmentRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        role = normalize_internal_role(body.role)
        saved = await upsert_internal_role_assignment(
            request.app.state.db,
            principal_id=body.principal_id,
            principal_email=body.principal_email,
            principal_display_name=body.principal_display_name,
            role=role,
            status=body.status,
            assigned_by=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if saved["role"] == "president" and saved["status"] == "active":
        await save_pay_settings(
            request.app.state.db,
            setting={"president_email": saved["principal_email"]},
            updated_by=actor.email,
            pay_cfg=request.app.state.cfg.pay_portal,
        )
    return saved


@router.delete("/pay/api/internal-roles/{assignment_id}")
async def remove_pay_internal_role(assignment_id: int, request: Request):
    actor = await _require_treasurer(request)
    existing = await internal_role_assignment_by_id(request.app.state.db, assignment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="assignment_id not found")
    try:
        deleted = await delete_internal_role_assignment(request.app.state.db, assignment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if deleted["role"] == "president":
        settings = await pay_settings(request.app.state.db, pay_cfg=request.app.state.cfg.pay_portal)
        if normalize_email(settings.get("president_email")) == normalize_email(deleted.get("principal_email")):
            await save_pay_settings(
                request.app.state.db,
                setting={"president_email": ""},
                updated_by=actor.email,
                pay_cfg=request.app.state.cfg.pay_portal,
            )
    return deleted


async def _save_pay_profile_request(
    body: PayProfileUpsertRequest,
    request: Request,
    *,
    email_override: str | None = None,
):
    actor = await _require_pay_actor(request)
    requested_email = normalize_email(email_override or body.principal_email)
    if actor.can_lock:
        target_email = requested_email
        target_principal_id = body.principal_id
        target_display_name = body.principal_display_name
        target_status = body.status
        target_pay_basis = body.pay_basis
    else:
        if requested_email and requested_email != actor.email:
            raise _forbidden("cannot edit another user's pay profile")
        existing = await pay_profile_by_email(request.app.state.db, email=actor.email)
        existing_basis = str((existing or {}).get("pay_basis") or "")
        requested_basis = str(body.pay_basis or "expense_only").strip().lower()
        if requested_basis == "president" and not (actor.is_president and existing_basis == "president"):
            raise _forbidden("treasurer access required for president pay profiles")
        target_email = actor.email
        target_principal_id = str((existing or {}).get("principal_id") or "").strip() or None
        target_display_name = body.principal_display_name or (existing or {}).get("principal_display_name") or actor.display_name
        target_status = str((existing or {}).get("status") or "active")
        target_pay_basis = "president" if actor.is_president and existing_basis == "president" else body.pay_basis
    wage_input_type = body.base_wage_input_type
    if str(target_pay_basis or "").strip().lower() in {"hourly", "weekly"}:
        wage_input_type = str(target_pay_basis).strip().lower()
    try:
        requested_profile = {
            "pay_basis": target_pay_basis,
            "base_wage_input_type": wage_input_type,
            "base_wage_amount": body.base_wage_amount,
            "weekly_basis_hours": body.weekly_basis_hours,
            "commission_month_1_amount": body.commission_month_1_amount,
            "commission_month_2_amount": body.commission_month_2_amount,
            "commission_month_3_amount": body.commission_month_3_amount,
        }
        if not actor.can_lock:
            existing = await pay_profile_by_email(request.app.state.db, email=target_email)
            wage_changed = pay_profile_wage_fields_changed(existing, requested_profile)
            safe_basis = str((existing or {}).get("pay_basis") or "expense_only")
            safe_wage_type = str((existing or {}).get("base_wage_input_type") or "hourly")
            safe_profile = await upsert_pay_profile(
                request.app.state.db,
                principal_id=target_principal_id,
                principal_email=target_email,
                principal_display_name=target_display_name,
                pay_basis=safe_basis,
                base_wage_input_type=safe_wage_type,
                base_wage_amount=float((existing or {}).get("base_wage_amount") or 0),
                weekly_basis_hours=float((existing or {}).get("weekly_basis_hours") or body.weekly_basis_hours or 40),
                commission_month_1_amount=float((existing or {}).get("commission_month_1_amount") or 0),
                commission_month_2_amount=float((existing or {}).get("commission_month_2_amount") or 0),
                commission_month_3_amount=float((existing or {}).get("commission_month_3_amount") or 0),
                status=str((existing or {}).get("status") or "active"),
                notes=body.notes,
                updated_by=actor.email,
                default_address=body.default_address,
            )
            if wage_changed:
                pending = await request_pay_profile_change(
                    request.app.state.db,
                    principal_id=target_principal_id,
                    principal_email=target_email,
                    principal_display_name=target_display_name,
                    pay_basis=target_pay_basis,
                    base_wage_input_type=wage_input_type,
                    base_wage_amount=body.base_wage_amount,
                    weekly_basis_hours=body.weekly_basis_hours,
                    commission_month_1_amount=body.commission_month_1_amount,
                    commission_month_2_amount=body.commission_month_2_amount,
                    commission_month_3_amount=body.commission_month_3_amount,
                    status=target_status,
                    notes=body.notes,
                    requested_by=actor.email,
                    default_address=body.default_address,
                )
                return {**safe_profile, "pending_wage_approval": True, "pending_profile_change": pending}
            return safe_profile
        return await upsert_pay_profile(
            request.app.state.db,
            principal_id=target_principal_id,
            principal_email=target_email,
            principal_display_name=target_display_name,
            pay_basis=target_pay_basis,
            base_wage_input_type=wage_input_type,
            base_wage_amount=body.base_wage_amount,
            weekly_basis_hours=body.weekly_basis_hours,
            commission_month_1_amount=body.commission_month_1_amount,
            commission_month_2_amount=body.commission_month_2_amount,
            commission_month_3_amount=body.commission_month_3_amount,
            status=target_status,
            notes=body.notes,
            updated_by=actor.email,
            default_address=body.default_address,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/profiles")
async def create_pay_profile(body: PayProfileUpsertRequest, request: Request):
    return await _save_pay_profile_request(body, request)


@router.put("/pay/api/profiles/{email}")
async def update_pay_profile(email: str, body: PayProfileUpsertRequest, request: Request):
    return await _save_pay_profile_request(body, request, email_override=email)


@router.post("/pay/api/profiles/changes/{request_id}/review")
async def review_pay_profile_change(request_id: str, body: PayProfileChangeReviewRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await review_pay_profile_change_request(
            request.app.state.db,
            request_id=request_id,
            actor=actor.email,
            approved=body.approved,
            review_note=body.review_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/pay/api/profiles/{email}")
async def remove_pay_profile(email: str, request: Request):
    actor = await _require_treasurer(request)
    try:
        removed = await delete_pay_profile(request.app.state.db, email=email, actor=actor.email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"removed": removed, "removed_by": actor.email}


@router.post("/pay/api/wage-scales")
async def save_pay_wage_scale(body: PayWageScaleUpsertRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await upsert_wage_scale(
            request.app.state.db,
            effective_date=body.effective_date,
            weekly_basis_hours=body.weekly_basis_hours,
            target_weekly_amount=body.target_weekly_amount,
            actual_weekly_amount=body.actual_weekly_amount,
            target_multiplier=body.target_multiplier,
            target_scale=body.target_scale,
            actual_scale=body.actual_scale,
            notes=body.notes,
            updated_by=actor.email,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/periods/{period_id}/lock")
async def lock_pay_period(period_id: str, body: PayLockRequest, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await lock_period_and_send_packet(
            db=request.app.state.db,
            cfg=request.app.state.cfg,
            graph=request.app.state.graph,
            docuseal=request.app.state.docuseal,
            period_id=period_id,
            actor=actor,
            president_signer_email=body.president_email,
            docx_to_pdf_func=docx_to_pdf,
            mailer=getattr(request.app.state, "mailer", None),
        )
    except PermissionError as exc:
        await _record_pay_lock_failure(request, period_id=period_id, actor=actor, exc=exc, status_code=403)
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        await _record_pay_lock_failure(request, period_id=period_id, actor=actor, exc=exc, status_code=400)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        await _record_pay_lock_failure(request, period_id=period_id, actor=actor, exc=exc, status_code=502)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        await _record_pay_lock_failure(request, period_id=period_id, actor=actor, exc=exc, status_code=502)
        raise HTTPException(status_code=502, detail=f"failed to lock and send pay packet: {exc}") from exc


@router.post("/pay/api/periods/{period_id}/revision")
async def revise_pay_period(period_id: str, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await create_revision(request.app.state.db, period_id=period_id, actor=actor)
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
