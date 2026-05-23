from __future__ import annotations

import json
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..core.intake_auth import verify_intake_request_auth
from ..services.referral_service import REFERRAL_STATUSES, ReferralService
from .officer_auth import actor_identity, require_admin_user, require_authenticated_officer, require_officer_page_access
from .referral_models import (
    ReferralListResponse,
    ReferralProgramSettingsResponse,
    ReferralProgramSettingsUpdateRequest,
    ReferralRow,
    ReferralRunDueResponse,
    ReferralSubmissionRequest,
    ReferralSubmissionResponse,
    ReferralUpdateRequest,
)

router = APIRouter()


def _service(request: Request) -> ReferralService:
    return request.app.state.referrals


def _client_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for", "") or "").strip()
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    return str(request.client.host if request.client else "").strip()


def _handle_runtime_error(exc: RuntimeError) -> HTTPException:
    message = str(exc)
    status_code = 404 if "not found" in message else 400
    if "not configured" in message or "disabled" in message:
        status_code = 503
    if "sunset date has passed" in message:
        status_code = 409
    return HTTPException(status_code=status_code, detail=message)


def _public_row(item: dict) -> ReferralRow:  # noqa: ANN001
    allowed = set(ReferralRow.model_fields.keys())
    return ReferralRow(**{key: value for key, value in item.items() if key in allowed})


@router.post("/referrals", response_model=ReferralSubmissionResponse)
async def create_referral(body: ReferralSubmissionRequest, request: Request):
    await verify_intake_request_auth(request, request.app.state.cfg.intake_auth)
    try:
        row = await _service(request).create_referral(
            payload=body.model_dump(mode="json"),
            client_ip=_client_ip(request),
            user_agent=str(request.headers.get("user-agent", "") or ""),
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return ReferralSubmissionResponse(
        referral_id=row["id"],
        status=row["status"],
        reminder_due_at_utc=row["reminder_due_at_utc"],
    )


@router.get("/officers/referrals", response_class=HTMLResponse)
async def referrals_page(request: Request):
    gate = await require_officer_page_access(request, next_path="/officers/referrals")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(_render_referrals_page())


@router.get("/officers/referrals/data", response_model=ReferralListResponse)
async def referral_rows(
    request: Request,
    search: str | None = None,
    group: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    reminder: str | None = None,
):
    await require_authenticated_officer(request)
    rows = await _service(request).list_referrals(
        search=search,
        group=group,
        status=status,
        assignee=assignee,
        reminder=reminder,
    )
    return ReferralListResponse(rows=[_public_row(row) for row in rows], count=len(rows))


@router.get("/officers/referrals/settings", response_model=ReferralProgramSettingsResponse)
async def referral_program_settings(request: Request):
    await require_authenticated_officer(request)
    return ReferralProgramSettingsResponse(**await _service(request).program_settings())


@router.put("/officers/referrals/settings", response_model=ReferralProgramSettingsResponse)
async def update_referral_program_settings(body: ReferralProgramSettingsUpdateRequest, request: Request):
    user = await require_authenticated_officer(request)
    try:
        settings = await _service(request).update_program_settings(
            sunset_date=body.sunset_date,
            updated_by=actor_identity(user, fallback="officer-ui"),
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return ReferralProgramSettingsResponse(**settings)


@router.patch("/officers/referrals/{referral_id}", response_model=ReferralRow)
async def update_referral(referral_id: str, body: ReferralUpdateRequest, request: Request):
    await require_authenticated_officer(request)
    try:
        row = await _service(request).update_referral(
            referral_id,
            {key: value for key, value in body.model_dump(mode="json").items() if value is not None},
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return _public_row(row)


@router.delete("/officers/referrals/{referral_id}", response_model=ReferralRow)
async def delete_referral(referral_id: str, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    try:
        row = await _service(request).delete_referral(referral_id)
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return _public_row(row)


@router.get("/officers/referrals/export.csv")
async def export_referrals(
    request: Request,
    search: str | None = None,
    group: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    reminder: str | None = None,
):
    await require_admin_user(request, allow_local_fallback=True)
    rows = await _service(request).list_referrals(
        search=search,
        group=group,
        status=status,
        assignee=assignee,
        reminder=reminder,
    )
    csv_text = await _service(request).export_csv(rows)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="referrals.csv"'},
    )


@router.post("/officers/referrals/run-due", response_model=ReferralRunDueResponse)
async def run_referral_reminders(request: Request):
    await require_authenticated_officer(request)
    try:
        result = await _service(request).run_due()
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return ReferralRunDueResponse(**result)


def _render_referrals_page() -> str:
    status_options = "".join(
        f'<option value="{escape(status, quote=True)}">{escape(status.replace("_", " ").title())}</option>'
        for status in REFERRAL_STATUSES
    )
    statuses_json = json.dumps(list(REFERRAL_STATUSES))
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Referral Tracker</title>
  <style>
    :root {{
      --bg: #f4f7f9;
      --panel: #ffffff;
      --border: #d7e0e7;
      --text: #1f2933;
      --muted: #5b6b78;
      --accent: #0f766e;
      --danger: #a4262c;
      --warning: #8a3b00;
      --success: #107c10;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      color: var(--text);
      background: linear-gradient(180deg, #f8fbfc 0%, var(--bg) 100%);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }}
    .shell {{ max-width: 1720px; margin: 0 auto; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 14px;
      box-shadow: 0 14px 34px rgba(15, 23, 42, 0.06);
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
      flex-wrap: wrap;
    }}
    .settings-row {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      flex-wrap: wrap;
    }}
    h1, h2 {{ margin: 0 0 8px; }}
    .summary {{ color: var(--muted); font-size: 14px; line-height: 1.5; }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 10px;
    }}
    .metric-card {{
      border: 1px solid var(--border);
      border-radius: 10px;
      background: linear-gradient(180deg, #ffffff 0%, #f6fafb 100%);
      padding: 13px 14px;
      min-height: 92px;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .metric-value {{
      margin-top: 8px;
      color: #15242e;
      font-size: 30px;
      font-weight: 800;
      line-height: 1;
    }}
    .metric-note {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(160px, 1fr));
      gap: 10px;
      align-items: end;
    }}
    label {{ display: grid; gap: 5px; font-size: 13px; font-weight: 700; color: #314250; }}
    input, select, textarea {{
      width: 100%;
      border: 1px solid #aebbc6;
      border-radius: 6px;
      padding: 8px 9px;
      color: var(--text);
      font: inherit;
      background: white;
    }}
    textarea {{ min-height: 80px; resize: vertical; }}
    button, .button-link {{
      border: 0;
      border-radius: 999px;
      padding: 9px 14px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      color: white;
      background: var(--accent);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    button.secondary, .button-link.secondary {{ color: var(--accent); background: #fff; border: 1px solid var(--accent); }}
    button.danger {{ background: var(--danger); }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .table-wrap {{ overflow: auto; border: 1px solid var(--border); border-radius: 10px; }}
    table {{ width: 100%; min-width: 1460px; border-collapse: collapse; background: white; }}
    th, td {{ border-bottom: 1px solid #e3e9ee; padding: 11px 12px; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ position: sticky; top: 0; background: #e8f1f4; color: #243746; z-index: 1; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
    tr:hover td {{ background: #f8fbfc; }}
    .pill {{ display: inline-flex; border-radius: 999px; padding: 4px 8px; background: #edf4f7; font-weight: 700; }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 800;
      background: #edf4f7;
      color: #244253;
    }}
    .status-open {{ background: #fff4ce; color: #6b3a00; }}
    .status-contacted {{ background: #dff3f1; color: #075b61; }}
    .status-converted {{ background: #dff3df; color: #0b5a0b; }}
    .status-not_interested, .status-closed {{ background: #e6ebef; color: #384b59; }}
    .paid-badge {{
      display: inline-flex;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 6px;
    }}
    .paid-badge.paid {{ background: #dff3df; color: #0b5a0b; }}
    .paid-badge.unpaid {{ background: #edf4f7; color: #40515e; }}
    .reminder-badge {{
      display: inline-flex;
      border-radius: 999px;
      padding: 4px 9px;
      font-weight: 800;
      font-size: 12px;
      margin-bottom: 4px;
    }}
    .due {{ color: var(--warning); background: #fff2d6; }}
    .sent {{ color: var(--success); background: #e3f4e3; }}
    .error {{ color: var(--danger); font-weight: 700; }}
    .date-main {{ font-weight: 700; }}
    .date-sub {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .edit-stack {{ display: grid; gap: 8px; margin-top: 8px; }}
    .paid-toggle {{ display: flex; gap: 8px; align-items: center; margin-top: 8px; }}
    .paid-toggle input {{ width: auto; }}
    .note-preview {{ max-width: 280px; white-space: pre-wrap; color: #40515e; }}
    .hidden {{ display: none; }}
    @media (max-width: 900px) {{
      body {{ padding: 12px; }}
      .grid, .metric-grid {{ grid-template-columns: 1fr; }}
      .header {{ display: block; }}
      .settings-row {{ display: block; }}
      .actions {{ margin-top: 10px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel header">
      <div>
        <h1>Referral Tracker</h1>
        <div class="summary">Track public referral submissions and the one-time 60-day officer reminder.</div>
      </div>
      <div class="actions">
        <a class="button-link secondary" href="/officers">Officer Tracker</a>
        <a class="button-link secondary" href="/forms/referral" target="_blank" rel="noreferrer">Public Link</a>
        <button id="runDueBtn" type="button">Run Due Reminders</button>
        <button id="exportBtn" class="secondary" type="button">Export CSV</button>
      </div>
    </section>

    <section class="panel">
      <div class="settings-row">
        <div>
          <h2>Referral Window</h2>
          <div id="settingsSummary" class="summary">Loading sunset date...</div>
        </div>
        <div class="actions">
          <label>Sunset Date
            <input id="sunsetDateInput" type="date" />
          </label>
          <button id="saveSettingsBtn" type="button">Save Sunset</button>
        </div>
      </div>
    </section>

    <section class="panel">
      <div id="metricGrid" class="metric-grid" aria-label="Referral summary metrics"></div>
    </section>

    <section class="panel">
      <div class="grid">
        <label>Search
          <input id="searchInput" placeholder="Name, group, phone, UID" />
        </label>
        <label>Group
          <input id="groupInput" placeholder="Referrer or referred group" />
        </label>
        <label>Status
          <select id="statusFilter">
            <option value="">All statuses</option>
            {status_options}
          </select>
        </label>
        <label>Assignee
          <input id="assigneeFilter" />
        </label>
        <label>Reminder
          <select id="reminderFilter">
            <option value="">All reminders</option>
            <option value="due">Due or overdue</option>
            <option value="upcoming">Upcoming</option>
            <option value="sent">Sent</option>
          </select>
        </label>
      </div>
      <div class="actions" style="margin-top:12px;">
        <button id="applyFiltersBtn" type="button">Apply Filters</button>
        <button id="clearFiltersBtn" class="secondary" type="button">Clear</button>
        <span id="statusText" class="summary"></span>
      </div>
    </section>

    <section class="panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Created</th>
              <th>Status</th>
              <th>Paid</th>
              <th>Referred Person</th>
              <th>Referrer</th>
              <th>Group</th>
              <th>Reminder</th>
              <th>Assignee</th>
              <th>Notes</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="rowsBody"><tr><td colspan="10">Loading referrals...</td></tr></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const STATUSES = {statuses_json};
    const TERMINAL_STATUSES = ['converted', 'not_interested', 'closed'];
    let rows = [];

    function esc(value) {{
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function params() {{
      const out = new URLSearchParams();
      for (const [key, id] of [
        ['search', 'searchInput'],
        ['group', 'groupInput'],
        ['status', 'statusFilter'],
        ['assignee', 'assigneeFilter'],
        ['reminder', 'reminderFilter']
      ]) {{
        const value = document.getElementById(id).value.trim();
        if (value) out.set(key, value);
      }}
      return out;
    }}

    function titleCaseStatus(status) {{
      return String(status || '').replaceAll('_', ' ').replace(/\\b\\w/g, (char) => char.toUpperCase());
    }}

    function formatDateTime(value) {{
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat(undefined, {{
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit'
      }}).format(date);
    }}

    function formatDateOnly(value) {{
      if (!value) return '';
      const date = new Date(`${{value}}T00:00:00`);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat(undefined, {{ month: 'short', day: 'numeric', year: 'numeric' }}).format(date);
    }}

    function reminderState(row) {{
      if (row.reminder_sent_at_utc) return 'sent';
      if (row.reminder_error) return 'failed';
      const dueDate = Date.parse(row.reminder_due_at_utc || '');
      if (dueDate && dueDate <= Date.now() && !TERMINAL_STATUSES.includes(row.status)) return 'due';
      return 'upcoming';
    }}

    function renderMetrics() {{
      const counts = {{
        open: rows.filter((row) => row.status === 'open').length,
        contacted: rows.filter((row) => row.status === 'contacted').length,
        converted: rows.filter((row) => row.status === 'converted').length,
        paid: rows.filter((row) => row.paid).length,
        unpaid: rows.filter((row) => !row.paid).length,
        due: rows.filter((row) => reminderState(row) === 'due').length,
        failed: rows.filter((row) => reminderState(row) === 'failed').length
      }};
      const cards = [
        ['Open', counts.open, 'New referrals awaiting review'],
        ['Contacted', counts.contacted, 'Follow-up has started'],
        ['Converted', counts.converted, 'Referral became a member/contact'],
        ['Paid', counts.paid, 'Referral payment recorded'],
        ['Unpaid', counts.unpaid, 'Still needs payment review'],
        ['Due or Overdue', counts.due, '60-day reminders ready'],
        ['Failed Reminders', counts.failed, 'Needs email retry or review']
      ];
      document.getElementById('metricGrid').innerHTML = cards.map(([label, value, note]) => `
        <div class="metric-card">
          <div class="metric-label">${{esc(label)}}</div>
          <div class="metric-value">${{esc(value)}}</div>
          <div class="metric-note">${{esc(note)}}</div>
        </div>
      `).join('');
    }}

    function reminderLabel(row) {{
      const state = reminderState(row);
      if (state === 'sent') return `<span class="reminder-badge sent">Sent</span><div class="date-sub">${{esc(formatDateTime(row.reminder_sent_at_utc))}}</div>`;
      if (state === 'failed') return `<span class="reminder-badge error">Failed</span><div class="date-sub">${{esc(row.reminder_error)}}</div>`;
      const label = state === 'due' ? 'Due now' : 'Upcoming';
      const className = state === 'due' ? 'due' : '';
      return `<span class="reminder-badge ${{className}}">${{esc(label)}}</span><div class="date-sub">${{esc(formatDateTime(row.reminder_due_at_utc))}}</div>`;
    }}

    function statusSelect(row) {{
      return `<select data-role="status">${{
        STATUSES.map((status) => `<option value="${{esc(status)}}"${{row.status === status ? ' selected' : ''}}>${{esc(titleCaseStatus(status))}}</option>`).join('')
      }}</select>`;
    }}

    function renderRows() {{
      const body = document.getElementById('rowsBody');
      if (!rows.length) {{
        body.innerHTML = '<tr><td colspan="10">No referrals match the current filters.</td></tr>';
        return;
      }}
      body.innerHTML = rows.map((row) => `
        <tr data-id="${{esc(row.id)}}">
          <td>
            <div class="date-main">${{esc(formatDateTime(row.created_at_utc))}}</div>
            <div class="date-sub">${{esc(row.id)}}</div>
          </td>
          <td>
            <span class="status-badge status-${{esc(row.status)}}">${{esc(titleCaseStatus(row.status))}}</span>
            <div class="edit-stack">${{statusSelect(row)}}</div>
          </td>
          <td>
            <span class="paid-badge ${{row.paid ? 'paid' : 'unpaid'}}">${{row.paid ? 'Paid' : 'Unpaid'}}</span>
            <div class="date-sub">${{row.paid_at_utc ? esc(formatDateTime(row.paid_at_utc)) : 'Not paid yet'}}</div>
            <label class="paid-toggle"><input data-role="paid" type="checkbox"${{row.paid ? ' checked' : ''}} /> Paid</label>
          </td>
          <td>
            <strong>${{esc(row.referred_name)}}</strong><br>
            <span class="summary">UID: ${{esc(row.referred_att_uid || '')}}</span><br>
            <div class="edit-stack">
              <label>Group<input data-role="referred_group" value="${{esc(row.referred_group || '')}}" /></label>
              <label>AT&T UID<input data-role="referred_att_uid" value="${{esc(row.referred_att_uid || '')}}" /></label>
            </div>
          </td>
          <td>
            <strong>${{esc(row.referrer_name)}}</strong><br>
            ${{esc(row.referrer_phone)}}<br>
            ${{esc(row.referrer_email || '')}}<br>
            <span class="summary">${{esc(row.referrer_address)}}</span>
          </td>
          <td><span class="pill">${{esc(row.referrer_group)}}</span><br><span class="summary">${{esc(row.referred_group || '')}}</span></td>
          <td>${{reminderLabel(row)}}<div class="edit-stack"><label>Due<input data-role="reminder_due_at_utc" value="${{esc(row.reminder_due_at_utc || '')}}" /></label></div></td>
          <td><input data-role="assignee" value="${{esc(row.assignee || '')}}" /></td>
          <td>
            <div class="note-preview">${{esc(row.referral_notes || '')}}</div>
            <textarea data-role="officer_notes">${{esc(row.officer_notes || '')}}</textarea>
          </td>
          <td>
            <div class="actions">
              <button data-role="save" type="button">Save</button>
              <button data-role="delete" class="danger" type="button">Delete</button>
            </div>
          </td>
        </tr>
      `).join('');
    }}

    function setStatus(message, ok) {{
      const el = document.getElementById('statusText');
      el.textContent = message || '';
      el.style.color = ok ? '#107c10' : '#a4262c';
    }}

    function setSettingsSummary(settings, message, ok) {{
      const el = document.getElementById('settingsSummary');
      if (settings) {{
        const status = settings.is_active ? 'active' : 'closed';
        const updated = settings.updated_at_utc ? ` Updated by ${{settings.updated_by || 'officer'}} at ${{formatDateTime(settings.updated_at_utc)}}.` : '';
        const disabled = settings.enabled ? '' : ' Referral tracking is disabled in config.';
        el.textContent = `Referral form is ${{status}} through ${{formatDateOnly(settings.sunset_date)}}.${{updated}}${{disabled}}`;
        el.style.color = settings.is_active ? '#5b6b78' : '#a4262c';
        return;
      }}
      el.textContent = message || '';
      el.style.color = ok ? '#107c10' : '#a4262c';
    }}

    async function loadSettings() {{
      const response = await fetch('/officers/referrals/settings');
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));
      document.getElementById('sunsetDateInput').value = data.sunset_date || '';
      setSettingsSummary(data, '', true);
    }}

    async function saveSettings() {{
      const sunsetDate = document.getElementById('sunsetDateInput').value;
      const response = await fetch('/officers/referrals/settings', {{
        method: 'PUT',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ sunset_date: sunsetDate }})
      }});
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));
      document.getElementById('sunsetDateInput').value = data.sunset_date || '';
      setSettingsSummary(data, '', true);
    }}

    async function loadRows() {{
      setStatus('Loading...', true);
      const qs = params().toString();
      const response = await fetch(`/officers/referrals/data${{qs ? '?' + qs : ''}}`);
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));
      rows = Array.isArray(data.rows) ? data.rows : [];
      renderMetrics();
      renderRows();
      setStatus(`${{data.count || 0}} referrals loaded.`, true);
    }}

    async function saveRow(tr) {{
      const id = tr.dataset.id;
      const payload = {{}};
      for (const role of ['status', 'paid', 'assignee', 'officer_notes', 'referred_group', 'referred_att_uid', 'reminder_due_at_utc']) {{
        const el = tr.querySelector(`[data-role="${{role}}"]`);
        if (el) payload[role] = el.type === 'checkbox' ? el.checked : el.value;
      }}
      const response = await fetch(`/officers/referrals/${{encodeURIComponent(id)}}`, {{
        method: 'PATCH',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));
      await loadRows();
      setStatus(`Saved ${{id}}.`, true);
    }}

    async function deleteRow(tr) {{
      const id = tr.dataset.id;
      if (!window.confirm(`Delete referral ${{id}}?`)) return;
      const response = await fetch(`/officers/referrals/${{encodeURIComponent(id)}}`, {{ method: 'DELETE' }});
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));
      await loadRows();
      setStatus(`Deleted ${{id}}.`, true);
    }}

    document.getElementById('applyFiltersBtn').addEventListener('click', () => void loadRows().catch((error) => setStatus(error.message, false)));
    document.getElementById('clearFiltersBtn').addEventListener('click', () => {{
      for (const id of ['searchInput', 'groupInput', 'statusFilter', 'assigneeFilter', 'reminderFilter']) document.getElementById(id).value = '';
      void loadRows().catch((error) => setStatus(error.message, false));
    }});
    document.getElementById('exportBtn').addEventListener('click', () => {{
      const qs = params().toString();
      window.location.assign(`/officers/referrals/export.csv${{qs ? '?' + qs : ''}}`);
    }});
    document.getElementById('runDueBtn').addEventListener('click', async () => {{
      try {{
        const response = await fetch('/officers/referrals/run-due', {{ method: 'POST' }});
        const data = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(data));
        await loadRows();
        setStatus(`Reminder run complete. Sent ${{data.sent_count || 0}}, failed ${{data.failed_count || 0}}.`, true);
      }} catch (error) {{
        setStatus(error.message, false);
      }}
    }});
    document.getElementById('saveSettingsBtn').addEventListener('click', () => {{
      void saveSettings().catch((error) => setSettingsSummary(null, error.message, false));
    }});
    document.getElementById('rowsBody').addEventListener('click', (event) => {{
      const button = event.target.closest('button[data-role]');
      if (!button) return;
      const tr = button.closest('tr');
      if (!tr) return;
      if (button.dataset.role === 'save') void saveRow(tr).catch((error) => setStatus(error.message, false));
      if (button.dataset.role === 'delete') void deleteRow(tr).catch((error) => setStatus(error.message, false));
    }});
    void loadSettings().catch((error) => setSettingsSummary(null, error.message, false));
    void loadRows().catch((error) => setStatus(error.message, false));
  </script>
</body>
</html>
"""
