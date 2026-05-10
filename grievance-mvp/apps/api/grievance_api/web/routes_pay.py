from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..db.db import Db
from ..services.pay_portal import (
    PayActor,
    approve_irs_rate_candidate,
    create_mileage_attachment,
    create_revision,
    current_period_bounds,
    decode_content_base64,
    ensure_pay_period,
    list_attachments,
    list_compensation_stubs,
    list_entries,
    list_irs_rate_candidates,
    list_pay_users,
    list_wage_scales,
    lock_period_and_send_packet,
    normalize_email,
    pay_settings,
    save_pay_settings,
    store_attachment,
    store_compensation_stub,
    sync_irs_mileage_rate_candidates,
    treasurer_recipients,
    upsert_entry,
    upsert_pay_user,
    upsert_wage_scale,
)
from ..services.pdf_convert import docx_to_pdf
from .officer_auth import (
    current_external_steward_user,
    current_officer_user,
)


router = APIRouter()


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
    notes: str | None = None


class PayAttachmentUploadRequest(BaseModel):
    period_id: str
    filename: str
    content_type: str | None = None
    content_base64: str
    attachment_type: str = "receipt"


class PayCompensationStubRequest(BaseModel):
    user_email: str | None = None
    base_wage_input_type: str = "hourly"
    base_wage_amount: float = 0
    weekly_basis_hours: float = 40.0
    commission_month_1_amount: float = 0
    commission_month_2_amount: float = 0
    commission_month_3_amount: float = 0
    filename: str
    content_type: str | None = None
    content_base64: str
    notes: str | None = None


class PayMileageRequest(BaseModel):
    period_id: str
    name: str
    local_number: str | None = "3106"
    date: str
    description: str
    locations: list[str] = Field(default_factory=list)
    rate: str | None = None


class PaySettingsUpdateRequest(BaseModel):
    president_email: str | None = None
    treasurer_emails: list[str] | None = None
    irs_rates: dict[str, str] | None = None
    common_places: list[dict[str, str]] | None = None


class PayUserUpsertRequest(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "guest"
    status: str = "active"


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


def _actor_from_officer(user: Any, *, treasurer: bool) -> PayActor:
    role = str(getattr(user, "role", "") or "").lower()
    is_admin = role == "admin"
    return PayActor(
        email=normalize_email(getattr(user, "email", "")),
        display_name=getattr(user, "display_name", None),
        role="treasurer" if treasurer or is_admin else "officer",
        can_view_all=True,
        can_edit_all=bool(treasurer or is_admin),
        can_lock=bool(treasurer or is_admin),
        is_guest=False,
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
        return _actor_from_officer(officer, treasurer=officer_email in treasurer_emails)

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
          <label>Name<input id="displayName" name="display_name"></label>
          <label>Hourly Rate<input name="hourly_rate" type="number" step="0.01"></label>
          <label>Lost Wage Type<select name="lost_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option><option value="profile">Saved Profile</option></select></label>
          <label>Lost Wage Amount<input name="lost_wage_amount" type="number" step="0.01"></label>
          <label>Hours<input name="hours" type="number" step="0.25"></label>
          <label>Mileage Amount<input name="mileage_amount" type="number" step="0.01"></label>
          <label>Rentals<input name="rentals_amount" type="number" step="0.01"></label>
          <label>Meals<input name="meals_amount" type="number" step="0.01"></label>
          <label>Hotel<input name="hotel_amount" type="number" step="0.01"></label>
          <label>Miscellaneous<input name="miscellaneous_amount" type="number" step="0.01"></label>
          <label>President Diff Hours<input name="president_diff_hours" type="number" step="0.25"></label>
          <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label>Local<input name="local_number" value="3106"></label>
        </div>
        <label>Address<input name="address"></label>
        <label>Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit">Save Entry</button><span id="entryStatus" class="muted"></span></div>
      </form>
    </section>
    <section>
      <h2>Lost Wage Proof</h2>
      <form id="stubForm">
        <div class="grid">
          <label>Member Email<input name="user_email"></label>
          <label>Base Wage Type<select name="base_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
          <label>Base Wage Amount<input name="base_wage_amount" type="number" step="0.01"></label>
          <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label>Commission Month 1<input name="commission_month_1_amount" type="number" step="0.01"></label>
          <label>Commission Month 2<input name="commission_month_2_amount" type="number" step="0.01"></label>
          <label>Commission Month 3<input name="commission_month_3_amount" type="number" step="0.01"></label>
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
      <table><thead><tr><th></th><th>Date</th><th>Name</th><th>Hours</th><th>Mileage</th><th>Other</th><th>President Diff</th><th>Notes</th></tr></thead><tbody id="entriesBody"></tbody></table>
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
          <label>Scale 36 Weekly Base<input name="target_weekly_amount" type="number" step="0.01"></label>
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
    tr.innerHTML = `<td><input type="radio" name="entryPick" value="${row.id}"></td><td>${row.entry_date}</td><td>${row.display_name || row.user_email}</td><td>${money(row.hours)}</td><td>${money(row.mileage_amount)}</td><td>${money(other)}</td><td>${money(row.president_diff_amount)}</td><td></td>`;
    tr.lastChild.textContent = row.notes || '';
    body.appendChild(tr);
  }
  body.querySelectorAll('input[name="entryPick"]').forEach(input => input.addEventListener('change', () => selectedEntryId = input.value));
  const stubsBody = document.getElementById('stubsBody');
  stubsBody.innerHTML = '';
  for (const row of context.compensation_stubs || []) {
    const tr = document.createElement('tr');
    addCell(tr, row.user_email);
    addCell(tr, `${row.base_wage_input_type} ${money(row.base_wage_amount)}`);
    addCell(tr, money(row.commission_average_monthly));
    addCell(tr, money(row.commission_hourly_rate));
    addCell(tr, money(row.calculated_hourly_rate));
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
    td.colSpan = 5;
    td.textContent = 'No staged IRS rates.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    addCell(tr, row.rate_year);
    addCell(tr, row.rate_per_mile);
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
    document.getElementById('irsStatus').textContent = `Approved ${result.rate_year}: ${result.active_rate}`;
    await loadContext();
  } catch (err) {
    document.getElementById('irsStatus').textContent = err.message;
  }
}
document.getElementById('entryForm').addEventListener('submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  data.period_id = context.period.id;
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
    "president": "President",
    "treasurer": "Treasurer",
    "admin": "Admin",
}


def _pay_nav_html(*, view: str, actor: PayActor) -> str:
    links = [("entry", "Submit"), ("mileage", "Mileage")]
    if not actor.is_guest:
        links.append(("president", "President"))
    if actor.can_view_all:
        links.append(("treasurer", "Treasurer"))
    if actor.can_lock:
        links.append(("admin", "Admin"))
    rendered: list[str] = []
    for key, label in links:
        current = ' aria-current="page"' if key == view else ""
        rendered.append(f'<a class="nav-link" href="/pay/{key}"{current}>{escape(label)}</a>')
    return "".join(rendered)


def _entry_form_html(*, president: bool = False) -> str:
    if president:
        return """
        <form id="entryForm" class="form-stack">
          <div class="field-grid">
            <label>Date<input name="entry_date" type="date" required></label>
            <label>Lost Wage Type<select name="lost_wage_input_type"><option value="profile">Saved Profile</option><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
            <label>Lost Wage Amount<input name="lost_wage_amount" type="number" step="0.01"></label>
            <label>Union Hours<input name="hours" type="number" step="0.25"></label>
            <label>Differential Hours<input name="president_diff_hours" type="number" step="0.25"></label>
            <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          </div>
          <label>Notes<textarea name="notes"></textarea></label>
          <div class="toolbar"><button type="submit">Save President Entry</button><span id="entryStatus" class="muted"></span></div>
        </form>
        """
    return """
        <form id="entryForm" class="form-stack">
          <div class="field-grid">
            <label>Date<input name="entry_date" type="date" required></label>
            <label>Name<input name="display_name"></label>
            <label>Lost Wage Type<select name="lost_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option><option value="profile">Saved Profile</option></select></label>
            <label>Lost Wage Amount<input name="lost_wage_amount" type="number" step="0.01"></label>
            <label>Hours<input name="hours" type="number" step="0.25"></label>
            <label>Mileage Miles<input name="mileage_miles" type="number" step="0.01" readonly></label>
            <label>IRS Rate<input name="mileage_rate" type="number" step="0.001" readonly></label>
            <label>Mileage Amount<input name="mileage_amount" type="number" step="0.01" readonly></label>
            <label>Meals<input name="meals_amount" type="number" step="0.01"></label>
            <label>Hotel<input name="hotel_amount" type="number" step="0.01"></label>
            <label>Rental<input name="rentals_amount" type="number" step="0.01"></label>
            <label>Miscellaneous<input name="miscellaneous_amount" type="number" step="0.01"></label>
            <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
            <label>Local<input name="local_number" value="3106"></label>
          </div>
          <label>Address<input name="address"></label>
          <label>Notes<textarea name="notes"></textarea></label>
          <div class="toolbar"><button type="submit">Save Entry</button><span id="entryStatus" class="muted"></span></div>
        </form>
    """


def _stub_form_html(*, title: str = "First-Time Wage Setup") -> str:
    return f"""
    <section class="panel" id="wageProfilePanel">
      <div class="section-head">
        <div><p class="eyebrow">{escape(title)}</p><h2>Work Wage Proof</h2></div>
        <div class="muted">Hourly, weekly, or commission based</div>
      </div>
      <form id="stubForm" class="form-stack">
        <div class="field-grid">
          <label>Member Email<input name="user_email"></label>
          <label>Base Wage Type<select name="base_wage_input_type"><option value="hourly">Hourly</option><option value="weekly">Weekly</option></select></label>
          <label>Base Wage Amount<input name="base_wage_amount" type="number" step="0.01"></label>
          <label>Weekly Basis<select name="weekly_basis_hours"><option value="40">40</option><option value="37.5">37.5</option></select></label>
          <label>Commission Month 1<input name="commission_month_1_amount" type="number" step="0.01"></label>
          <label>Commission Month 2<input name="commission_month_2_amount" type="number" step="0.01"></label>
          <label>Commission Month 3<input name="commission_month_3_amount" type="number" step="0.01"></label>
          <label>Pay Stub<input id="stubFile" type="file" accept=".pdf,image/png,image/jpeg"></label>
        </div>
        <label>Notes<textarea name="notes"></textarea></label>
        <div class="toolbar"><button type="submit" class="secondary">Save Wage Profile</button><span id="stubStatus" class="muted"></span></div>
      </form>
      <div class="table-wrap compact-table">
        <table><thead><tr><th>Member</th><th>Base</th><th>Commission Avg</th><th>Commission Hr</th><th>Total Hr</th><th>File</th></tr></thead><tbody id="stubsBody"></tbody></table>
      </div>
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

          <div class="mb-3">
            <label for="name" class="form-label">Name:</label>
            <input type="text" class="form-control" id="name" name="name" required>
          </div>

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
            <label for="irs_rate" class="form-label">IRS Rate ($ per mile):</label>
            <input type="text" class="form-control" id="irs_rate" name="irs_rate" placeholder="auto">
            <div class="form-text">Leave blank or type "auto" to use the IRS rate for that year. If you enter 72.5 it will be treated as 72.5 cents (0.725).</div>
          </div>

          <div class="mb-3">
            <label class="form-label" for="commonPlaceSelect">Common places:</label>
            <select id="commonPlaceSelect" class="form-control">
              <option value="">Select a place...</option>
            </select>
            <button type="button" class="btn btn-outline-secondary w-100 mt-2" id="addCommonPlaceBtn">Add selected place to locations</button>
          </div>

          <div class="mb-3">
            <label class="form-label">Locations:</label>
            <div id="locations">
              <div class="input-group mb-2">
                <input type="text" class="form-control address-input" name="locations" placeholder="Origin" required autocomplete="off">
                <button class="btn btn-danger remove-location" type="button" aria-label="Remove location">&times;</button>
              </div>
              <div class="input-group mb-2">
                <input type="text" class="form-control address-input" name="locations" placeholder="Destination" required autocomplete="off">
                <button class="btn btn-danger remove-location" type="button" aria-label="Remove location">&times;</button>
              </div>
            </div>
            <button type="button" class="btn btn-secondary add-location w-100 mt-2">Add Location</button>
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
        <table><thead><tr><th>Date</th><th>Name</th><th>File</th><th>Scan</th></tr></thead><tbody id="mileageFormsBody"></tbody></table>
      </div>
    </section>
    """


def _entries_table_html(*, attach: bool) -> str:
    attach_html = """
      <div class="toolbar">
        <input id="receiptFile" type="file" accept=".pdf,image/png,image/jpeg">
        <button id="uploadReceiptBtn" type="button" class="secondary">Attach Receipt To Selected</button>
      </div>
    """ if attach else ""
    return f"""
    <section class="panel">
      <div class="section-head">
        <div><p class="eyebrow">Current Period</p><h2>Entries</h2></div>
        <div id="periodStats" class="muted"></div>
      </div>
      {attach_html}
      <div class="table-wrap">
        <table><thead><tr><th></th><th>Date</th><th>Name</th><th>Hours</th><th>Lost Wages</th><th>Mileage</th><th>Other</th><th>President Diff</th><th>Notes</th></tr></thead><tbody id="entriesBody"></tbody></table>
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
    if view == "president":
        return f"""
        <section class="panel lead-panel">
          <div><p class="eyebrow">President</p><h2>Differential and Lost Time</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="actorLabel"></span><strong>Signed in</strong></div></div>
        </section>
        {_stub_form_html(title="President Wage Setup")}
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Daily Input</p><h2>President Entry</h2></div><div class="muted">Scale 36 + 20% target</div></div>
          {_entry_form_html(president=True)}
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
        return f"""
        <section class="panel lead-panel">
          <div><p class="eyebrow">Treasurer</p><h2>Review, Lock, and Send</h2></div>
          <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="periodStats"></span><strong>Totals</strong></div></div>
        </section>
        <section class="panel">
          <div class="section-head"><div><p class="eyebrow">Packet Control</p><h2>Voucher Packet</h2></div><span id="treasurerStatus" class="muted"></span></div>
          {packet_controls}
        </section>
        {_entries_table_html(attach=False)}
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
              <label>Scale 36 Weekly Base<input name="target_weekly_amount" type="number" step="0.01"></label>
              <label>President Target<input value="Scale 36 + 20%" disabled></label>
            </div>
            <div class="toolbar"><button type="submit" class="secondary">Save Settings</button></div>
          </form>
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
        """
    return f"""
    <section class="panel lead-panel">
      <div><p class="eyebrow">Submit</p><h2>Lost Wage Entry</h2></div>
      <div class="metric-row"><div><span id="periodLabel"></span><strong>Period</strong></div><div><span id="actorLabel"></span><strong>Signed in</strong></div></div>
    </section>
    {_stub_form_html()}
    <section class="panel">
      <div class="section-head"><div><p class="eyebrow">Daily Input</p><h2>Lost Time and Expenses</h2></div><span id="entryStatus" class="muted"></span></div>
      {_entry_form_html()}
    </section>
    {_entries_table_html(attach=True)}
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
    h1, h2 { margin:0; letter-spacing:0; }
    h1 { font-size:24px; }
    h2 { font-size:18px; }
    .eyebrow { margin:0 0 4px; color:var(--accent); font-size:12px; font-weight:800; text-transform:uppercase; }
    .muted { color:var(--muted); font-size:13px; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:16px; min-width:0; }
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
    th { background:#f8fafb; font-weight:800; color:#384656; }
    tr:last-child td { border-bottom:0; }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns:1fr; }
      .side-nav { position:relative; height:auto; border-right:0; border-bottom:1px solid var(--line); }
      .nav-links { grid-template-columns:repeat(2, minmax(0, 1fr)); }
      .lead-panel, .topbar { align-items:flex-start; flex-direction:column; }
      .metric-row { justify-content:flex-start; }
      .field-grid { grid-template-columns:1fr; }
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
      <header class="topbar"><div><p class="eyebrow">Pay Portal</p><h1>__PAGE_TITLE__</h1></div><div class="muted">Private workspace</div></header>
      __CONTENT__
    </main>
  </div>
  <script>
const PAY_VIEW = "__VIEW__";
let context = null;
let selectedEntryId = null;
const byId = id => document.getElementById(id);
function money(value) { return Number(value || 0).toFixed(2); }
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
function renderSummary() {
  if (!context) return;
  const entries = context.entries || [];
  const lost = entries.reduce((sum, row) => sum + Number(row.lost_wage_hourly_rate || row.hourly_rate || 0) * Number(row.hours || 0), 0);
  const mileage = entries.reduce((sum, row) => sum + Number(row.mileage_amount || 0), 0);
  const other = entries.reduce((sum, row) => sum + Number(row.rentals_amount || 0) + Number(row.meals_amount || 0) + Number(row.hotel_amount || 0) + Number(row.miscellaneous_amount || 0), 0);
  setText('periodLabel', `${context.period.period_start} to ${context.period.period_end} - ${context.period.status}`);
  setText('actorLabel', context.actor.display_name || context.actor.email || '');
  setText('periodStats', `${entries.length} entries | $${money(lost + mileage + other)}`);
}
function renderEntries() {
  const body = byId('entriesBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const entries = context.entries || [];
  if ((!selectedEntryId || !entries.some(row => row.id === selectedEntryId)) && entries.length) selectedEntryId = entries[0].id;
  for (const row of entries) {
    const tr = document.createElement('tr');
    const pick = document.createElement('td');
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'entryPick';
    radio.value = row.id;
    radio.checked = row.id === selectedEntryId;
    radio.addEventListener('change', () => { selectedEntryId = row.id; syncMileageFormFromEntry(); });
    pick.appendChild(radio);
    tr.appendChild(pick);
    addCell(tr, row.entry_date);
    addCell(tr, row.display_name || row.user_email);
    addCell(tr, money(row.hours));
    addCell(tr, money(Number(row.lost_wage_hourly_rate || row.hourly_rate || 0) * Number(row.hours || 0)));
    addCell(tr, money(row.mileage_amount));
    addCell(tr, money(Number(row.rentals_amount || 0) + Number(row.meals_amount || 0) + Number(row.hotel_amount || 0) + Number(row.miscellaneous_amount || 0)));
    addCell(tr, money(row.president_diff_amount));
    addCell(tr, row.notes || '');
    body.appendChild(tr);
  }
  syncMileageFormFromEntry();
}
let activeAddressInput = null;
function selectedEntry() {
  if (!context || !selectedEntryId) return null;
  return (context.entries || []).find(row => row.id === selectedEntryId) || null;
}
function mileageRateForYear(year) {
  const rates = context && context.settings && context.settings.irs_rates;
  if (rates && Object.prototype.hasOwnProperty.call(rates, String(year))) return rates[String(year)];
  return '0.67';
}
function syncMileageRateFromDate() {
  const dateEl = byId('date');
  const rateEl = byId('irs_rate');
  if (!dateEl || !rateEl || !dateEl.value) return;
  const current = String(rateEl.value || '').trim().toLowerCase();
  if (current && current !== 'auto') return;
  rateEl.value = mileageRateForYear(dateEl.value.slice(0, 4));
}
function syncMileageFormFromEntry() {
  const form = byId('mileageForm');
  if (!form || !context) return;
  const select = byId('mileageEntrySelect');
  const entry = selectedEntry();
  if (select && select.value !== (selectedEntryId || '')) select.value = selectedEntryId || '';
  if (!entry) return;
  form.name.value = entry.display_name || context.actor.display_name || entry.user_email || context.actor.email || '';
  form.local_number.value = entry.local_number || '3106';
  form.date.value = entry.entry_date || form.date.value;
  form.description.value = entry.notes || form.description.value || 'Union business';
  const rateEl = byId('irs_rate');
  if (rateEl && (!rateEl.value || String(rateEl.value).trim().toLowerCase() === 'auto')) syncMileageRateFromDate();
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
  input.addEventListener('focus', () => { activeAddressInput = input; });
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
function renderCommonPlaces() {
  const select = byId('commonPlaceSelect');
  if (!select || !context) return;
  const places = (context.settings && context.settings.common_places) || [];
  select.innerHTML = '<option value="">Select a place...</option>';
  for (const place of places) {
    const opt = document.createElement('option');
    opt.value = place.address || '';
    opt.textContent = `${place.label || 'Place'} - ${place.address || ''}`;
    select.appendChild(opt);
  }
}
function renderMileageForms() {
  const body = byId('mileageFormsBody');
  if (!body || !context) return;
  body.innerHTML = '';
  const forms = (context.attachments || []).filter(row => row.attachment_type === 'mileage_pdf');
  if (!forms.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = 'No mileage forms generated yet.';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  for (const row of forms) {
    const tr = document.createElement('tr');
    addCell(tr, row.entry_date);
    addCell(tr, row.user_email);
    addCell(tr, row.filename);
    addCell(tr, row.scan_status || 'generated');
    body.appendChild(tr);
  }
}
function renderStubs() {
  const body = byId('stubsBody');
  if (!body || !context) return;
  body.innerHTML = '';
  for (const row of context.compensation_stubs || []) {
    const tr = document.createElement('tr');
    addCell(tr, row.user_email);
    addCell(tr, `${row.base_wage_input_type} ${money(row.base_wage_amount)}`);
    addCell(tr, money(row.commission_average_monthly));
    addCell(tr, money(row.commission_hourly_rate));
    addCell(tr, money(row.calculated_hourly_rate));
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
    addCell(tr, row.rate_per_mile);
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
function fillSettings() {
  const form = byId('settingsForm');
  if (!form || !context) return;
  form.president_email.value = context.settings.president_email || '';
  form.treasurer_emails.value = Array.isArray(context.settings.treasurer_emails) ? context.settings.treasurer_emails.join(', ') : '';
}
async function loadContext() {
  context = await api('/pay/api/context');
  renderSummary();
  renderEntries();
  renderMileageEntrySelect();
  renderCommonPlaces();
  renderMileageForms();
  renderStubs();
  renderPayUsers();
  renderIrsCandidates();
  fillSettings();
}
bind('entryForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  data.period_id = context.period.id;
  for (const key of ['hourly_rate','lost_wage_amount','hours','mileage_miles','mileage_rate','mileage_amount','rentals_amount','meals_amount','hotel_amount','miscellaneous_amount','president_diff_hours','weekly_basis_hours']) data[key] = Number(data[key] || 0);
  try { await api('/pay/api/entries', { method: 'POST', body: JSON.stringify(data) }); setText('entryStatus', 'Saved'); await loadContext(); }
  catch (err) { setText('entryStatus', err.message); }
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
bind('mileageEntrySelect', 'change', event => {
  selectedEntryId = event.target.value || null;
  syncMileageFormFromEntry();
});
bind('date', 'change', syncMileageRateFromDate);
bind('addCommonPlaceBtn', 'click', () => {
  const select = byId('commonPlaceSelect');
  const value = select && select.value;
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
});
bind('mileageForm', 'submit', async event => {
  event.preventDefault();
  const form = event.target;
  const locations = Array.from(form.querySelectorAll('input[name="locations"]')).map(input => input.value.trim()).filter(Boolean);
  if (locations.length < 2) { setText('mileageStatus', 'Enter at least an origin and destination.'); return; }
  let rate = String(form.irs_rate.value || '').trim();
  if (!rate || rate.toLowerCase() === 'auto') rate = null;
  const body = {
    period_id: context.period.id,
    name: form.name.value.trim(),
    local_number: form.local_number.value.trim() || '3106',
    date: form.date.value,
    description: form.description.value.trim(),
    locations,
    rate,
  };
  try {
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
    setText('mileageStatus', `${result.filename || 'Mileage PDF'} attached | ${money(result.mileage_miles)} miles | $${money(result.reimbursement)}`);
    await loadContext();
  } catch (err) { setText('mileageStatus', err.message); }
});
bind('mileageForm', 'reset', () => {
  setText('mileageStatus', '');
});
document.querySelectorAll('#locations .address-input').forEach(input => wireAddressInput(input));
document.querySelectorAll('#locations .remove-location').forEach(button => button.addEventListener('click', () => button.parentElement.remove()));
const addLocationButton = document.querySelector('#mileageForm .add-location');
if (addLocationButton) addLocationButton.addEventListener('click', () => addMileageLocationField(''));
bind('lockBtn', 'click', async () => {
  try { const result = await api(`/pay/api/periods/${context.period.id}/lock`, { method: 'POST', body: JSON.stringify({}) }); setText('treasurerStatus', result.signing_link || 'Sent'); await loadContext(); }
  catch (err) { setText('treasurerStatus', err.message); }
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
bind('payUserForm', 'submit', async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  try { await api('/pay/api/users', { method: 'POST', body: JSON.stringify(data) }); setText('payUserStatus', 'Saved'); event.target.reset(); await loadContext(); }
  catch (err) { setText('payUserStatus', err.message); }
});
bind('irsSyncBtn', 'click', async () => {
  try { const result = await api('/pay/api/irs-rates/sync', { method: 'POST', body: JSON.stringify({}) }); setText('irsStatus', result.detected.length ? `${result.detected.length} rate staged` : 'No new IRS rates'); await loadContext(); }
  catch (err) { setText('irsStatus', err.message); }
});
async function approveIrsRate(candidateId) {
  try { const result = await api(`/pay/api/irs-rates/${candidateId}/approve`, { method: 'POST', body: JSON.stringify({}) }); setText('irsStatus', `Approved ${result.rate_year}: ${result.active_rate}`); await loadContext(); }
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
    landing = "treasurer" if actor.can_lock else "entry"
    return RedirectResponse(url=f"/pay/{landing}", status_code=303)


@router.get("/pay/{view}", response_class=HTMLResponse)
async def pay_view_page(view: str, request: Request):
    normalized_view = str(view or "").strip().lower()
    if normalized_view not in _PAY_VIEW_TITLES:
        raise HTTPException(status_code=404, detail="pay page not found")
    actor = await _current_pay_actor(request)
    if not actor:
        return RedirectResponse(url="/pay/start", status_code=303)
    if normalized_view == "treasurer" and not actor.can_view_all:
        raise _forbidden("officer access required")
    if normalized_view == "admin" and not actor.can_lock:
        raise _forbidden("treasurer access required")
    return HTMLResponse(_render_pay_workspace_page(view=normalized_view, actor=actor))


@router.get("/pay/api/context")
async def pay_context(request: Request):
    actor = await _require_pay_actor(request)
    db: Db = request.app.state.db
    period = await ensure_pay_period(db)
    entries = await list_entries(db, period_id=str(period["id"]), actor=actor)
    attachments = await list_attachments(db, period_id=str(period["id"]), actor=actor)
    compensation_stubs = await list_compensation_stubs(db, actor=actor)
    settings = await pay_settings(db, pay_cfg=request.app.state.cfg.pay_portal)
    return {
        "actor": {
            "email": actor.email,
            "display_name": actor.display_name,
            "role": actor.role,
            "can_view_all": actor.can_view_all,
            "can_edit_all": actor.can_edit_all,
            "can_lock": actor.can_lock,
            "is_guest": actor.is_guest,
        },
        "period": period,
        "entries": entries,
        "attachments": attachments,
        "compensation_stubs": compensation_stubs,
        "settings": settings,
        "pay_users": await list_pay_users(db) if actor.can_lock else [],
        "wage_scales": await list_wage_scales(db) if actor.can_lock else [],
        "irs_rate_candidates": await list_irs_rate_candidates(db) if actor.can_lock else [],
    }


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
        )
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


@router.post("/pay/api/entries/{entry_id}/mileage")
async def create_pay_mileage(entry_id: str, body: PayMileageRequest, request: Request):
    actor = await _require_pay_actor(request)
    try:
        return await create_mileage_attachment(
            db=request.app.state.db,
            cfg=request.app.state.cfg,
            period_id=body.period_id,
            entry_id=entry_id,
            actor=actor,
            name=body.name,
            local_number=body.local_number or "3106",
            date_str=body.date,
            description=body.description,
            locations=body.locations,
            rate_text=body.rate,
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
        )
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pay/api/periods/{period_id}/revision")
async def revise_pay_period(period_id: str, request: Request):
    actor = await _require_treasurer(request)
    try:
        return await create_revision(request.app.state.db, period_id=period_id, actor=actor)
    except PermissionError as exc:
        raise _forbidden(str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
