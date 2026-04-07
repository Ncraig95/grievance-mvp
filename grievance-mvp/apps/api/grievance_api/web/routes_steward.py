from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db.db import Db, utcnow
from ..services.contract_timeline import parse_incident_date
from .models import (
    ExternalStewardActionRequest,
    ExternalStewardActionResponse,
    ExternalStewardCaseAssignmentCreateRequest,
    ExternalStewardCaseAssignmentListResponse,
    ExternalStewardCaseAssignmentRow,
    ExternalStewardCaseListResponse,
    ExternalStewardCaseRow,
    ExternalStewardUserCreateRequest,
    ExternalStewardUserListResponse,
    ExternalStewardUserRow,
    ExternalStewardUserUpdateRequest,
    ExternalStewardViewerContext,
)
from .officer_auth import (
    ExternalStewardUserContext,
    actor_identity,
    current_external_steward_user,
    current_officer_user,
    external_steward_auth_enabled,
    require_admin_user,
    require_authenticated_external_steward,
    require_external_steward_page_access,
)
from .routes_officers import _load_officer_case_row, _load_officer_case_rows

router = APIRouter()

_ACTIVE = "active"
_DISABLED = "disabled"
_ACTION_SETTLEMENT_COMPLETE = "settlement_complete"
_ACTION_SENT_SECOND_LEVEL = "sent_second_level"
_ACTION_SENT_THIRD_LEVEL = "sent_third_level"
_ACTION_SENT_FOURTH_LEVEL = "sent_fourth_level"


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_email(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or "@" not in text:
        raise HTTPException(status_code=400, detail="email must be a valid email address")
    return text


def _normalize_status(value: object) -> str:
    text = str(value or "").strip().lower()
    if text not in {_ACTIVE, _DISABLED}:
        raise HTTPException(status_code=400, detail="invalid external steward status")
    return text


def _today_iso(cfg) -> str:  # noqa: ANN001
    tz_name = str(getattr(getattr(cfg, "grievance_id", None), "timezone", "") or "America/New_York").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def _normalize_action_date(cfg, value: object) -> str:  # noqa: ANN001
    text = str(value or "").strip()
    if not text:
        return _today_iso(cfg)
    parsed = parse_incident_date(text)
    return parsed.isoformat() if parsed else text


def _external_user_row(row: tuple[object, ...]) -> ExternalStewardUserRow:
    return ExternalStewardUserRow(
        user_id=int(row[0]),
        email=str(row[1] or ""),
        display_name=_normalize_optional_text(row[2]),
        status=str(row[3] or ""),
        auth_source=_normalize_optional_text(row[4]),
        auth_issuer=_normalize_optional_text(row[5]),
        auth_subject=_normalize_optional_text(row[6]),
        invited_by=str(row[7] or ""),
        created_at_utc=str(row[8] or ""),
        updated_at_utc=str(row[9] or ""),
        last_login_at_utc=_normalize_optional_text(row[10]),
        assignment_count=int(row[11] or 0),
    )


def _external_assignment_row(row: tuple[object, ...]) -> ExternalStewardCaseAssignmentRow:
    return ExternalStewardCaseAssignmentRow(
        assignment_id=int(row[0]),
        case_id=str(row[1] or ""),
        external_steward_user_id=int(row[2] or 0),
        email=str(row[3] or ""),
        display_name=_normalize_optional_text(row[4]),
        status=str(row[5] or ""),
        assigned_by=str(row[6] or ""),
        created_at_utc=str(row[7] or ""),
        updated_at_utc=str(row[8] or ""),
    )


async def _load_external_steward_users(db: Db) -> list[ExternalStewardUserRow]:
    rows = await db.fetchall(
        """
        SELECT u.id, u.email, u.display_name, u.status, u.auth_source, u.auth_issuer, u.auth_subject,
               u.invited_by, u.created_at_utc, u.updated_at_utc, u.last_login_at_utc,
               COUNT(a.id) as assignment_count
        FROM external_steward_users u
        LEFT JOIN external_steward_case_assignments a ON a.external_steward_user_id=u.id
        GROUP BY u.id, u.email, u.display_name, u.status, u.auth_source, u.auth_issuer, u.auth_subject,
                 u.invited_by, u.created_at_utc, u.updated_at_utc, u.last_login_at_utc
        ORDER BY lower(COALESCE(u.display_name, '')), lower(u.email), u.id
        """
    )
    return [_external_user_row(row) for row in rows]


async def _load_case_assignments(db: Db, *, case_id: str) -> list[ExternalStewardCaseAssignmentRow]:
    rows = await db.fetchall(
        """
        SELECT a.id, a.case_id, a.external_steward_user_id, u.email, u.display_name, u.status,
               a.assigned_by, a.created_at_utc, a.updated_at_utc
        FROM external_steward_case_assignments a
        JOIN external_steward_users u ON u.id=a.external_steward_user_id
        WHERE a.case_id=?
        ORDER BY lower(COALESCE(u.display_name, '')), lower(u.email), a.id
        """,
        (case_id,),
    )
    return [_external_assignment_row(row) for row in rows]


async def _load_external_steward_user(db: Db, *, user_id: int) -> ExternalStewardUserRow:
    row = await db.fetchone(
        """
        SELECT u.id, u.email, u.display_name, u.status, u.auth_source, u.auth_issuer, u.auth_subject,
               u.invited_by, u.created_at_utc, u.updated_at_utc, u.last_login_at_utc,
               (SELECT COUNT(*) FROM external_steward_case_assignments a WHERE a.external_steward_user_id=u.id)
        FROM external_steward_users u
        WHERE u.id=?
        """,
        (int(user_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="external steward user not found")
    return _external_user_row(row)


async def _upsert_external_steward_user(
    db: Db,
    *,
    email: str,
    display_name: str | None,
    invited_by: str,
) -> ExternalStewardUserRow:
    normalized_email = _normalize_email(email)
    normalized_name = _normalize_optional_text(display_name)
    now = utcnow()
    await db.exec(
        """
        INSERT INTO external_steward_users(
          email, display_name, status, invited_by, created_at_utc, updated_at_utc
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(email) DO UPDATE SET
          display_name=CASE
            WHEN excluded.display_name IS NOT NULL AND excluded.display_name<>''
            THEN excluded.display_name
            ELSE external_steward_users.display_name
          END,
          updated_at_utc=excluded.updated_at_utc
        """,
        (normalized_email, normalized_name, _ACTIVE, invited_by, now, now),
    )
    row = await db.fetchone("SELECT id FROM external_steward_users WHERE email=?", (normalized_email,))
    user = await _load_external_steward_user(db, user_id=int(row[0]))
    await db.add_event(
        f"external_steward:{user.user_id}",
        None,
        "external_steward_user_upserted",
        {
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status,
            "invited_by": invited_by,
        },
    )
    return user


async def _update_external_steward_status(
    db: Db,
    *,
    user_id: int,
    status: str,
    updated_by: str,
) -> ExternalStewardUserRow:
    normalized_status = _normalize_status(status)
    existing = await _load_external_steward_user(db, user_id=user_id)
    now = utcnow()
    await db.exec(
        "UPDATE external_steward_users SET status=?, updated_at_utc=? WHERE id=?",
        (normalized_status, now, int(user_id)),
    )
    updated = await _load_external_steward_user(db, user_id=user_id)
    await db.add_event(
        f"external_steward:{updated.user_id}",
        None,
        "external_steward_status_updated",
        {
            "email": updated.email,
            "previous_status": existing.status,
            "status": updated.status,
            "updated_by": updated_by,
        },
    )
    return updated


async def _assign_external_steward_to_case(
    db: Db,
    *,
    case_id: str,
    external_steward_user_id: int,
    assigned_by: str,
) -> ExternalStewardCaseAssignmentRow:
    user = await _load_external_steward_user(db, user_id=external_steward_user_id)
    if user.status != _ACTIVE:
        raise HTTPException(status_code=400, detail="cannot assign a disabled external steward")
    now = utcnow()
    await db.exec(
        """
        INSERT INTO external_steward_case_assignments(
          external_steward_user_id, case_id, created_at_utc, updated_at_utc, assigned_by
        ) VALUES(?,?,?,?,?)
        ON CONFLICT(external_steward_user_id, case_id) DO UPDATE SET
          updated_at_utc=excluded.updated_at_utc,
          assigned_by=excluded.assigned_by
        """,
        (int(external_steward_user_id), case_id, now, now, assigned_by),
    )
    row = await db.fetchone(
        """
        SELECT a.id, a.case_id, a.external_steward_user_id, u.email, u.display_name, u.status,
               a.assigned_by, a.created_at_utc, a.updated_at_utc
        FROM external_steward_case_assignments a
        JOIN external_steward_users u ON u.id=a.external_steward_user_id
        WHERE a.case_id=? AND a.external_steward_user_id=?
        """,
        (case_id, int(external_steward_user_id)),
    )
    if not row:
        raise HTTPException(status_code=500, detail="failed to save external steward assignment")
    assignment = _external_assignment_row(row)
    await db.add_event(
        case_id,
        None,
        "external_steward_case_assignment_added",
        {
            "external_steward_user_id": assignment.external_steward_user_id,
            "external_steward_email": assignment.email,
            "assigned_by": assigned_by,
        },
    )
    return assignment


async def _remove_external_steward_assignment(
    db: Db,
    *,
    case_id: str,
    assignment_id: int,
    removed_by: str,
) -> ExternalStewardCaseAssignmentRow:
    row = await db.fetchone(
        """
        SELECT a.id, a.case_id, a.external_steward_user_id, u.email, u.display_name, u.status,
               a.assigned_by, a.created_at_utc, a.updated_at_utc
        FROM external_steward_case_assignments a
        JOIN external_steward_users u ON u.id=a.external_steward_user_id
        WHERE a.id=? AND a.case_id=?
        """,
        (assignment_id, case_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="external steward assignment not found")
    assignment = _external_assignment_row(row)
    await db.exec("DELETE FROM external_steward_case_assignments WHERE id=? AND case_id=?", (assignment_id, case_id))
    await db.add_event(
        case_id,
        None,
        "external_steward_case_assignment_removed",
        {
            "external_steward_user_id": assignment.external_steward_user_id,
            "external_steward_email": assignment.email,
            "removed_by": removed_by,
        },
    )
    return assignment


async def _assigned_case_ids_for_external_user(db: Db, *, external_user_id: int) -> set[str]:
    rows = await db.fetchall(
        "SELECT case_id FROM external_steward_case_assignments WHERE external_steward_user_id=?",
        (int(external_user_id),),
    )
    return {str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()}


def _available_actions(row: ExternalStewardCaseRow) -> list[str]:
    actions: list[str] = []
    if row.officer_status != "closed":
        actions.append(_ACTION_SETTLEMENT_COMPLETE)
    if not row.second_level_request_sent_date:
        actions.append(_ACTION_SENT_SECOND_LEVEL)
    if not row.third_level_request_sent_date:
        actions.append(_ACTION_SENT_THIRD_LEVEL)
    if not row.fourth_level_request_sent_date:
        actions.append(_ACTION_SENT_FOURTH_LEVEL)
    return actions


async def _external_portal_rows(request: Request, user: ExternalStewardUserContext) -> list[ExternalStewardCaseRow]:
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    assigned_case_ids = await _assigned_case_ids_for_external_user(db, external_user_id=user.external_user_id)
    if not assigned_case_ids:
        return []
    rows = await _load_officer_case_rows(db, cfg=cfg)
    external_rows: list[ExternalStewardCaseRow] = []
    for row in rows:
        if row.case_id not in assigned_case_ids:
            continue
        external_row = ExternalStewardCaseRow(
            case_id=row.case_id,
            display_grievance=row.display_grievance,
            contract=row.contract,
            member_name=row.member_name,
            issue_summary=row.issue_summary,
            first_level_request_sent_date=row.first_level_request_sent_date,
            second_level_request_sent_date=row.second_level_request_sent_date,
            third_level_request_sent_date=row.third_level_request_sent_date,
            fourth_level_request_sent_date=row.fourth_level_request_sent_date,
            officer_status=row.officer_status,
            workflow_status=row.workflow_status,
        )
        external_row.available_actions = _available_actions(external_row)
        external_rows.append(external_row)
    return external_rows


async def _require_assigned_case(
    request: Request,
    *,
    case_id: str,
) -> tuple[ExternalStewardUserContext, object]:  # noqa: ANN401
    user = await require_authenticated_external_steward(request)
    assigned_case_ids = await _assigned_case_ids_for_external_user(request.app.state.db, external_user_id=user.external_user_id)
    if case_id not in assigned_case_ids:
        raise HTTPException(status_code=403, detail="case is not assigned to this external steward")
    case_row = await _load_officer_case_row(request.app.state.db, cfg=request.app.state.cfg, case_id=case_id)
    return user, case_row


def _steward_actor_details(user: ExternalStewardUserContext, *, action: str, action_date: str) -> dict[str, object]:
    return {
        "actor_email": user.email,
        "actor_display_name": user.display_name,
        "actor_role": "external_steward",
        "actor_auth_source": user.auth_source,
        "actor_issuer": user.issuer,
        "actor_provider_subject": user.provider_subject,
        "action": action,
        "action_date": action_date,
    }


def _render_steward_page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Steward Action Portal</title>
  <style>
    body { font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; color: #1f2933; background: #f7fafc; }
    h1, h2 { margin: 0 0 12px; }
    .page-shell { max-width: 1320px; margin: 0 auto; }
    .panel { background: #fff; border: 1px solid #dde4ea; border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 10px 25px rgba(15, 23, 42, 0.05); }
    .user-panel { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    .summary { color: #52606d; font-size: 14px; }
    .table-wrap { overflow: auto; border-radius: 12px; -webkit-overflow-scrolling: touch; }
    table { border-collapse: collapse; min-width: 1080px; width: 100%; background: white; }
    th, td { border: 1px solid #d9e2ec; padding: 10px; vertical-align: top; font-size: 14px; }
    th { background: #95cf46; color: white; text-align: left; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    button { background: #1f6feb; color: white; border: 0; border-radius: 8px; padding: 9px 12px; cursor: pointer; font: inherit; }
    button.secondary { background: #627d98; }
    button:disabled { background: #bcccdc; cursor: not-allowed; }
    input[type="date"] { border: 1px solid #bfc8d2; border-radius: 8px; padding: 8px 10px; font: inherit; }
    .badge { display: inline-block; padding: 4px 10px; border-radius: 999px; background: #e7f0ff; color: #1f3a5f; font-weight: 600; }
    pre { white-space: pre-wrap; word-break: break-word; margin: 0; }
    @media (max-width: 760px) {
      body { padding: 12px; }
      .panel { padding: 14px; border-radius: 14px; }
      .user-panel { flex-direction: column; align-items: stretch; }
      .actions { width: 100%; }
      .actions button,
      input[type="date"] { width: 100%; }
      .table-wrap { overflow: visible; }
      #stewardCasesTable { min-width: 0; }
      #stewardCasesTable thead { display: none; }
      #stewardCasesTable,
      #stewardCasesTable tbody,
      #stewardCasesTable tr,
      #stewardCasesTable td {
        display: block;
        width: 100%;
      }
      #stewardCasesTable tr {
        margin-bottom: 12px;
        border: 1px solid #d9e2ec;
        border-radius: 14px;
        overflow: hidden;
        background: white;
      }
      #stewardCasesTable td {
        border: 0;
        border-top: 1px solid #e5edf4;
        padding: 10px 12px;
      }
      #stewardCasesTable td:first-child { border-top: 0; }
      #stewardCasesTable td::before {
        content: attr(data-label);
        display: block;
        margin-bottom: 4px;
        font-size: 11px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: #607181;
      }
    }
  </style>
</head>
<body>
  <div class="page-shell">
  <h1>Steward Action Portal</h1>
  <div class="panel user-panel">
    <div>
      <div class="summary">Outside steward access is limited to assigned grievances and fixed workflow actions.</div>
      <div id="viewerLabel"></div>
    </div>
    <form method="post" action="/auth/logout">
      <button type="submit" class="secondary">Sign Out</button>
    </form>
  </div>

  <div class="panel">
    <div class="actions">
      <button id="reloadBtn" type="button">Reload Assigned Cases</button>
    </div>
    <div id="tableSummary" class="summary" style="margin-top: 12px;">Loading assigned cases.</div>
  </div>

  <div class="panel">
    <div class="table-wrap">
      <table id="stewardCasesTable">
        <thead>
          <tr>
            <th>Grievance</th>
            <th>Contract</th>
            <th>Member</th>
            <th>Issue</th>
            <th>2nd Level</th>
            <th>3rd Level</th>
            <th>4th Level</th>
            <th>Status</th>
            <th>Action Date</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="tableBody">
          <tr><td colspan="10">Loading assigned grievances.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>Last Response</h2>
    <pre id="out">Ready.</pre>
  </div>
  </div>

  <script>
    const tableBody = document.getElementById('tableBody');
    const tableSummary = document.getElementById('tableSummary');
    const viewerLabel = document.getElementById('viewerLabel');
    const out = document.getElementById('out');
    const ACTION_LABELS = {
      settlement_complete: 'Complete Settlement',
      sent_second_level: 'Mark Sent 2nd Level',
      sent_third_level: 'Mark Sent 3rd Level',
      sent_fourth_level: 'Mark Sent 4th Level'
    };

    function esc(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function show(data) {
      out.textContent = JSON.stringify(data, null, 2);
    }

    async function call(url, opts) {
      const res = await fetch(url, opts || {});
      if (res.status === 401) {
        window.location.href = '/auth/steward/login?next=' + encodeURIComponent('/steward');
        throw new Error('login required');
      }
      const text = await res.text();
      let data = text;
      try { data = JSON.parse(text); } catch {}
      if (!res.ok) throw { status: res.status, data };
      return data;
    }

    function renderRows(rows) {
      tableSummary.textContent = `${rows.length} assigned grievance(s).`;
      if (!rows.length) {
        tableBody.innerHTML = '<tr><td colspan="10">No grievances are currently assigned to you.</td></tr>';
        return;
      }
      tableBody.innerHTML = rows.map((row) => `
        <tr>
          <td data-label="Grievance">${esc(row.display_grievance)}<div class="summary">${esc(row.case_id)}</div></td>
          <td data-label="Contract">${esc(row.contract || '')}</td>
          <td data-label="Member">${esc(row.member_name || '')}</td>
          <td data-label="Issue">${esc(row.issue_summary || '')}</td>
          <td data-label="2nd Level">${esc(row.second_level_request_sent_date || '')}</td>
          <td data-label="3rd Level">${esc(row.third_level_request_sent_date || '')}</td>
          <td data-label="4th Level">${esc(row.fourth_level_request_sent_date || '')}</td>
          <td data-label="Status"><span class="badge">${esc(row.officer_status || '')}</span></td>
          <td data-label="Action Date"><input type="date" data-action-date-case="${esc(row.case_id)}" /></td>
          <td data-label="Actions">
            <div class="actions">
              ${(row.available_actions || []).map((action) => `<button type="button" data-case-id="${esc(row.case_id)}" data-action-key="${esc(action)}">${esc(ACTION_LABELS[action] || action)}</button>`).join('')}
            </div>
          </td>
        </tr>
      `).join('');
    }

    async function loadCases() {
      try {
        const data = await call('/steward/cases');
        if (data.viewer) {
          viewerLabel.textContent = `${data.viewer.display_name || data.viewer.email} · ${data.viewer.email}`;
        }
        renderRows(Array.isArray(data.rows) ? data.rows : []);
        show(data);
      } catch (e) {
        show(e);
      }
    }

    async function applyAction(caseId, action) {
      const dateInput = document.querySelector(`input[data-action-date-case="${CSS.escape(caseId)}"]`);
      const actionDate = dateInput && dateInput.value ? dateInput.value : null;
      try {
        const data = await call(`/steward/cases/${encodeURIComponent(caseId)}/actions/${encodeURIComponent(action.replace(/_/g, '-'))}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action_date: actionDate })
        });
        show(data);
        await loadCases();
      } catch (e) {
        show(e);
      }
    }

    document.getElementById('reloadBtn').addEventListener('click', () => { void loadCases(); });
    tableBody.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-case-id][data-action-key]');
      if (!button) return;
      void applyAction(button.dataset.caseId || '', button.dataset.actionKey || '');
    });
    window.addEventListener('DOMContentLoaded', () => { void loadCases(); });
  </script>
</body>
</html>
"""


@router.get("/steward", response_class=HTMLResponse)
async def steward_page(request: Request):
    gate = await require_external_steward_page_access(request, next_path="/steward")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(_render_steward_page())


@router.get("/steward/cases", response_model=ExternalStewardCaseListResponse)
async def steward_cases(request: Request):
    user = await require_authenticated_external_steward(request)
    rows = await _external_portal_rows(request, user)
    return ExternalStewardCaseListResponse(
        rows=rows,
        viewer=ExternalStewardViewerContext(
            email=user.email,
            display_name=user.display_name,
            auth_source=user.auth_source,
        ),
        count=len(rows),
    )


async def _external_action_response(request: Request, *, case_id: str, action: str, action_date: str) -> ExternalStewardActionResponse:
    row = await _load_officer_case_row(request.app.state.db, cfg=request.app.state.cfg, case_id=case_id)
    return ExternalStewardActionResponse(
        case_id=row.case_id,
        display_grievance=row.display_grievance,
        action=action,
        action_date=action_date,
        officer_status=row.officer_status,
        second_level_request_sent_date=row.second_level_request_sent_date,
        third_level_request_sent_date=row.third_level_request_sent_date,
        fourth_level_request_sent_date=row.fourth_level_request_sent_date,
        officer_closed_at_utc=row.officer_closed_at_utc,
        officer_closed_by=row.officer_closed_by,
    )


@router.post("/steward/cases/{case_id}/actions/settlement-complete", response_model=ExternalStewardActionResponse)
async def steward_action_settlement_complete(case_id: str, body: ExternalStewardActionRequest, request: Request):
    user, case_row = await _require_assigned_case(request, case_id=case_id)
    action_date = _normalize_action_date(request.app.state.cfg, body.action_date)
    now = utcnow()
    if case_row.officer_status != "closed":
        await request.app.state.db.exec(
            """
            UPDATE cases
            SET officer_status='closed',
                officer_closed_at_utc=COALESCE(officer_closed_at_utc, ?),
                officer_closed_by=COALESCE(officer_closed_by, ?)
            WHERE id=?
            """,
            (now, str(user.display_name or user.email), case_id),
        )
    await request.app.state.db.add_event(
        case_id,
        None,
        "external_steward_action_settlement_complete",
        _steward_actor_details(user, action=_ACTION_SETTLEMENT_COMPLETE, action_date=action_date),
    )
    return await _external_action_response(
        request,
        case_id=case_id,
        action=_ACTION_SETTLEMENT_COMPLETE,
        action_date=action_date,
    )


@router.post("/steward/cases/{case_id}/actions/sent-second-level", response_model=ExternalStewardActionResponse)
async def steward_action_sent_second_level(case_id: str, body: ExternalStewardActionRequest, request: Request):
    user, _ = await _require_assigned_case(request, case_id=case_id)
    action_date = _normalize_action_date(request.app.state.cfg, body.action_date)
    await request.app.state.db.exec(
        "UPDATE cases SET tracking_second_level_request_sent_date=? WHERE id=?",
        (action_date, case_id),
    )
    await request.app.state.db.add_event(
        case_id,
        None,
        "external_steward_action_sent_second_level",
        _steward_actor_details(user, action=_ACTION_SENT_SECOND_LEVEL, action_date=action_date),
    )
    return await _external_action_response(request, case_id=case_id, action=_ACTION_SENT_SECOND_LEVEL, action_date=action_date)


@router.post("/steward/cases/{case_id}/actions/sent-third-level", response_model=ExternalStewardActionResponse)
async def steward_action_sent_third_level(case_id: str, body: ExternalStewardActionRequest, request: Request):
    user, _ = await _require_assigned_case(request, case_id=case_id)
    action_date = _normalize_action_date(request.app.state.cfg, body.action_date)
    await request.app.state.db.exec(
        "UPDATE cases SET tracking_third_level_request_sent_date=? WHERE id=?",
        (action_date, case_id),
    )
    await request.app.state.db.add_event(
        case_id,
        None,
        "external_steward_action_sent_third_level",
        _steward_actor_details(user, action=_ACTION_SENT_THIRD_LEVEL, action_date=action_date),
    )
    return await _external_action_response(request, case_id=case_id, action=_ACTION_SENT_THIRD_LEVEL, action_date=action_date)


@router.post("/steward/cases/{case_id}/actions/sent-fourth-level", response_model=ExternalStewardActionResponse)
async def steward_action_sent_fourth_level(case_id: str, body: ExternalStewardActionRequest, request: Request):
    user, _ = await _require_assigned_case(request, case_id=case_id)
    action_date = _normalize_action_date(request.app.state.cfg, body.action_date)
    await request.app.state.db.exec(
        "UPDATE cases SET tracking_fourth_level_request_sent_date=? WHERE id=?",
        (action_date, case_id),
    )
    await request.app.state.db.add_event(
        case_id,
        None,
        "external_steward_action_sent_fourth_level",
        _steward_actor_details(user, action=_ACTION_SENT_FOURTH_LEVEL, action_date=action_date),
    )
    return await _external_action_response(request, case_id=case_id, action=_ACTION_SENT_FOURTH_LEVEL, action_date=action_date)


@router.get("/officers/external-stewards", response_model=ExternalStewardUserListResponse)
async def external_steward_users(request: Request):
    await require_admin_user(request)
    rows = await _load_external_steward_users(request.app.state.db)
    return ExternalStewardUserListResponse(rows=rows)


@router.post("/officers/external-stewards", response_model=ExternalStewardUserRow)
async def create_external_steward_user(body: ExternalStewardUserCreateRequest, request: Request):
    admin = await require_admin_user(request)
    return await _upsert_external_steward_user(
        request.app.state.db,
        email=body.email,
        display_name=body.display_name,
        invited_by=actor_identity(admin, fallback="admin"),
    )


@router.patch("/officers/external-stewards/{user_id}", response_model=ExternalStewardUserRow)
async def update_external_steward_user(user_id: int, body: ExternalStewardUserUpdateRequest, request: Request):
    admin = await require_admin_user(request)
    return await _update_external_steward_status(
        request.app.state.db,
        user_id=user_id,
        status=body.status,
        updated_by=actor_identity(admin, fallback="admin"),
    )


@router.get("/officers/cases/{case_id}/external-stewards", response_model=ExternalStewardCaseAssignmentListResponse)
async def case_external_stewards(case_id: str, request: Request):
    await require_admin_user(request)
    case_row = await _load_officer_case_row(request.app.state.db, cfg=request.app.state.cfg, case_id=case_id)
    rows = await _load_case_assignments(request.app.state.db, case_id=case_id)
    return ExternalStewardCaseAssignmentListResponse(
        case_id=case_id,
        display_grievance=case_row.display_grievance,
        rows=rows,
    )


@router.post("/officers/cases/{case_id}/external-stewards", response_model=ExternalStewardCaseAssignmentRow)
async def assign_case_external_steward(
    case_id: str,
    body: ExternalStewardCaseAssignmentCreateRequest,
    request: Request,
):
    admin = await require_admin_user(request)
    await _load_officer_case_row(request.app.state.db, cfg=request.app.state.cfg, case_id=case_id)
    return await _assign_external_steward_to_case(
        request.app.state.db,
        case_id=case_id,
        external_steward_user_id=body.external_steward_user_id,
        assigned_by=actor_identity(admin, fallback="admin"),
    )


@router.delete("/officers/cases/{case_id}/external-stewards/{assignment_id}", response_model=ExternalStewardCaseAssignmentRow)
async def remove_case_external_steward(case_id: str, assignment_id: int, request: Request):
    admin = await require_admin_user(request)
    await _load_officer_case_row(request.app.state.db, cfg=request.app.state.cfg, case_id=case_id)
    return await _remove_external_steward_assignment(
        request.app.state.db,
        case_id=case_id,
        assignment_id=assignment_id,
        removed_by=actor_identity(admin, fallback="admin"),
    )
