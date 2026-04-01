from __future__ import annotations

import json
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core.ids import new_case_id, new_grievance_id, normalize_grievance_id
from ..db.db import Db, utcnow
from ..services.contract_timeline import parse_incident_date
from .admin_common import parse_json_safely, require_local_access
from .models import (
    OfficerCaseBulkDeleteRequest,
    OfficerCaseBulkDeleteResponse,
    OfficerCaseBulkUpdateRequest,
    OfficerCaseBulkUpdateResponse,
    OfficerCaseDeleteResponse,
    OfficerCaseCreateRequest,
    OfficerCaseListResponse,
    OfficerCaseRow,
    OfficerCaseUpdateRequest,
)

router = APIRouter()

_OFFICER_STATUS_VALUES = {"open", "in_progress", "waiting", "closed"}
_PAPER_SOURCE = "paper_manual"
_DIGITAL_SOURCE = "digital_intake"
_MANUAL_TRACKING_STATUS = "manual_tracking"
_FINAL_WORKFLOW_STATUSES = {"approved", "rejected", "uploaded"}

_CASE_SELECT_SQL = """
    SELECT id, grievance_id, grievance_number, member_name, member_email,
           created_at_utc, status, approval_status, intake_payload_json,
           officer_status, officer_assignee, officer_notes, officer_source,
           officer_closed_at_utc, officer_closed_by,
           tracking_department, tracking_steward, tracking_occurrence_date,
           tracking_issue_summary, tracking_first_level_request_sent_date,
           tracking_second_level_request_sent_date
    FROM cases
"""


def _normalize_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_member_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="member_name is required")
    return text


def _normalize_date_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = parse_incident_date(text)
    return parsed.isoformat() if parsed else text


def _normalize_officer_status(value: object, *, default: str = "open") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text not in _OFFICER_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="invalid officer_status")
    return text


def _actor_label(value: object, *, fallback: str = "officer-ui") -> str:
    text = _normalize_optional_text(value)
    return text or fallback


def _payload_dict(raw: object) -> dict[str, object]:
    parsed = parse_json_safely(raw)
    return parsed if isinstance(parsed, dict) else {}


def _template_data(payload: dict[str, object]) -> dict[str, object]:
    raw = payload.get("template_data")
    return raw if isinstance(raw, dict) else {}


def _payload_pick(payload: dict[str, object], *keys: str) -> str | None:
    template_data = _template_data(payload)
    for key in keys:
        for source in (payload, template_data):
            raw = source.get(key)
            text = _normalize_optional_text(raw)
            if text:
                return text
    return None


def _payload_member_name(payload: dict[str, object]) -> str | None:
    direct = _payload_pick(payload, "member_name", "grievant_name", "grievant_full_name")
    if direct:
        return direct
    first = _payload_pick(payload, "grievant_firstname")
    last = _payload_pick(payload, "grievant_lastname")
    joined = " ".join(part for part in (first, last) if part)
    return joined or None


def _payload_document_signers(payload: dict[str, object]) -> list[str]:
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        return []
    for item in raw_documents:
        if not isinstance(item, dict):
            continue
        signers = item.get("signers")
        if not isinstance(signers, list):
            continue
        cleaned = [str(value or "").strip() for value in signers if str(value or "").strip()]
        if cleaned:
            return cleaned
    return []


def _fallback_steward(payload: dict[str, object]) -> str | None:
    steward = _payload_pick(
        payload,
        "steward",
        "steward_name",
        "union_rep_name",
        "union_representative",
        "q5_union_rep_name_attuid",
    )
    if steward:
        return steward
    signers = _payload_document_signers(payload)
    if len(signers) >= 2:
        return signers[1]
    return signers[0] if signers else None


def _effective_officer_source(raw_source: object, workflow_status: object) -> str:
    source = _normalize_optional_text(raw_source)
    if source:
        return source
    return _PAPER_SOURCE if str(workflow_status or "").strip() == _MANUAL_TRACKING_STATUS else _DIGITAL_SOURCE


def _effective_officer_status(raw_status: object, workflow_status: object) -> str:
    status = _normalize_optional_text(raw_status)
    if status:
        lowered = status.lower()
        return lowered if lowered in _OFFICER_STATUS_VALUES else "open"
    workflow = str(workflow_status or "").strip().lower()
    return "closed" if workflow in _FINAL_WORKFLOW_STATUSES else "open"


def _build_officer_case_row(row: tuple[object, ...]) -> OfficerCaseRow:
    payload = _payload_dict(row[8])
    grievance_id = str(row[1] or "").strip()
    grievance_number = _normalize_optional_text(row[2])
    member_name = _normalize_optional_text(row[3]) or _payload_member_name(payload) or "Unknown"
    member_email = _normalize_optional_text(row[4]) or _payload_pick(payload, "member_email", "grievant_email")
    workflow_status = str(row[6] or "").strip()
    department = _normalize_optional_text(row[15]) or _payload_pick(
        payload,
        "department",
        "q2_department",
        "q2a_other_department",
    )
    steward = _normalize_optional_text(row[16]) or _fallback_steward(payload)
    occurrence_date = _normalize_optional_text(row[17]) or _normalize_date_text(
        _payload_pick(payload, "incident_date", "q1_occurred_date", "date_grievance_occurred")
    )
    issue_summary = _normalize_optional_text(row[18]) or _payload_pick(
        payload,
        "issue_summary",
        "issue_text",
        "issue_contract_section",
        "q3_union_statement",
        "narrative",
    )
    first_level_request_sent_date = _normalize_optional_text(row[19]) or _normalize_date_text(
        _payload_pick(payload, "first_level_request_sent_date", "date_sent_first_level_request")
    )
    second_level_request_sent_date = _normalize_optional_text(row[20]) or _normalize_date_text(
        _payload_pick(payload, "second_level_request_sent_date", "date_sent_second_level_request")
    )

    return OfficerCaseRow(
        case_id=str(row[0]),
        grievance_id=grievance_id,
        grievance_number=grievance_number,
        display_grievance=grievance_number or grievance_id,
        member_name=member_name,
        member_email=member_email,
        department=department,
        steward=steward,
        occurrence_date=occurrence_date,
        issue_summary=issue_summary,
        first_level_request_sent_date=first_level_request_sent_date,
        second_level_request_sent_date=second_level_request_sent_date,
        officer_assignee=_normalize_optional_text(row[10]),
        officer_notes=_normalize_optional_text(row[11]),
        officer_status=_effective_officer_status(row[9], workflow_status),
        workflow_status=workflow_status,
        approval_status=str(row[7] or "").strip(),
        officer_source=_effective_officer_source(row[12], workflow_status),
        officer_closed_at_utc=_normalize_optional_text(row[13]),
        officer_closed_by=_normalize_optional_text(row[14]),
        created_at_utc=str(row[5] or "").strip(),
    )


def _case_matches_filters(
    row: OfficerCaseRow,
    *,
    search: str | None,
    assignee: str | None,
    officer_status: str | None,
    source: str | None,
) -> bool:
    search_text = str(search or "").strip().lower()
    assignee_text = str(assignee or "").strip().lower()
    status_text = str(officer_status or "").strip().lower()
    source_text = str(source or "").strip().lower()

    if assignee_text and str(row.officer_assignee or "").strip().lower() != assignee_text:
        return False
    if status_text and row.officer_status != status_text:
        return False
    if source_text and row.officer_source != source_text:
        return False
    if not search_text:
        return True

    haystack = " ".join(
        value
        for value in (
            row.case_id,
            row.display_grievance,
            row.grievance_id,
            row.grievance_number or "",
            row.member_name,
            row.member_email or "",
            row.department or "",
            row.steward or "",
            row.issue_summary or "",
            row.officer_assignee or "",
            row.officer_status,
            row.workflow_status,
            row.officer_source,
        )
        if value
    ).lower()
    return search_text in haystack


async def _load_officer_case_rows(db: Db) -> list[OfficerCaseRow]:
    rows = await db.fetchall(f"{_CASE_SELECT_SQL} ORDER BY created_at_utc DESC, id DESC")
    return [_build_officer_case_row(row) for row in rows]


async def _load_officer_case_row(db: Db, case_id: str) -> OfficerCaseRow:
    row = await db.fetchone(f"{_CASE_SELECT_SQL} WHERE id=?", (case_id,))
    if not row:
        raise HTTPException(status_code=404, detail="case_id not found")
    return _build_officer_case_row(row)


async def _load_officer_case_rows_by_id(db: Db, case_ids: list[str]) -> dict[str, tuple[object, ...]]:
    if not case_ids:
        return {}
    placeholders = _sql_placeholders(case_ids)
    rows = await db.fetchall(f"{_CASE_SELECT_SQL} WHERE id IN ({placeholders})", tuple(case_ids))
    return {str(row[0]): row for row in rows}


def _case_update_fields(body: OfficerCaseUpdateRequest, *, current_row: tuple[object, ...]) -> tuple[dict[str, object], str]:
    fields = set(body.model_fields_set)
    fields.discard("updated_by")
    fields.discard("case_ids")

    current_effective_status = _effective_officer_status(current_row[9], current_row[6])
    fallback_actor = _normalize_optional_text(current_row[10]) or "officer-ui"
    updated_by = _actor_label(body.updated_by, fallback=fallback_actor)

    updates: dict[str, object] = {}
    if "grievance_number" in fields:
        updates["grievance_number"] = _normalize_optional_text(body.grievance_number)
    if "member_name" in fields:
        updates["member_name"] = _normalize_member_name(body.member_name)
    if "member_email" in fields:
        updates["member_email"] = _normalize_optional_text(body.member_email)
    if "department" in fields:
        updates["tracking_department"] = _normalize_optional_text(body.department)
    if "steward" in fields:
        updates["tracking_steward"] = _normalize_optional_text(body.steward)
    if "occurrence_date" in fields:
        updates["tracking_occurrence_date"] = _normalize_date_text(body.occurrence_date)
    if "issue_summary" in fields:
        updates["tracking_issue_summary"] = _normalize_optional_text(body.issue_summary)
    if "first_level_request_sent_date" in fields:
        updates["tracking_first_level_request_sent_date"] = _normalize_date_text(body.first_level_request_sent_date)
    if "second_level_request_sent_date" in fields:
        updates["tracking_second_level_request_sent_date"] = _normalize_date_text(body.second_level_request_sent_date)
    if "officer_assignee" in fields:
        updates["officer_assignee"] = _normalize_optional_text(body.officer_assignee)
    if "officer_notes" in fields:
        updates["officer_notes"] = _normalize_optional_text(body.officer_notes)
    if "officer_status" in fields:
        next_status = _normalize_officer_status(body.officer_status, default="open")
        updates["officer_status"] = next_status
        if next_status == "closed" and current_effective_status != "closed":
            updates["officer_closed_at_utc"] = utcnow()
            updates["officer_closed_by"] = updated_by
        elif next_status != "closed" and current_effective_status == "closed":
            updates["officer_closed_at_utc"] = None
            updates["officer_closed_by"] = None

    return updates, updated_by


def _sql_placeholders(values: list[object]) -> str:
    return ", ".join("?" for _ in values)


async def _delete_cases_with_related_rows(db: Db, case_ids: list[str]) -> dict[str, int]:
    counts = {
        "cases": 0,
        "documents": 0,
        "document_stages": 0,
        "document_stage_artifacts": 0,
        "document_stage_field_values": 0,
        "events": 0,
        "outbound_emails": 0,
    }
    if not case_ids:
        return counts

    case_placeholders = _sql_placeholders(case_ids)
    case_params = tuple(case_ids)

    async with aiosqlite.connect(db.db_path) as con:
        await con.execute("BEGIN IMMEDIATE")

        cur = await con.execute(
            f"SELECT id FROM documents WHERE case_id IN ({case_placeholders})",
            case_params,
        )
        doc_ids = [str(row[0]) for row in await cur.fetchall()]

        stage_ids: list[int] = []
        if doc_ids:
            doc_placeholders = _sql_placeholders(doc_ids)
            cur = await con.execute(
                f"""
                SELECT id
                FROM document_stages
                WHERE case_id IN ({case_placeholders}) OR document_id IN ({doc_placeholders})
                """,
                case_params + tuple(doc_ids),
            )
            stage_ids = [int(row[0]) for row in await cur.fetchall()]
        else:
            cur = await con.execute(
                f"SELECT id FROM document_stages WHERE case_id IN ({case_placeholders})",
                case_params,
            )
            stage_ids = [int(row[0]) for row in await cur.fetchall()]

        if stage_ids:
            stage_placeholders = _sql_placeholders(stage_ids)
            cur = await con.execute(
                f"DELETE FROM document_stage_artifacts WHERE document_stage_id IN ({stage_placeholders})",
                tuple(stage_ids),
            )
            counts["document_stage_artifacts"] = max(cur.rowcount, 0)
            cur = await con.execute(
                f"DELETE FROM document_stage_field_values WHERE document_stage_id IN ({stage_placeholders})",
                tuple(stage_ids),
            )
            counts["document_stage_field_values"] = max(cur.rowcount, 0)

        cur = await con.execute(
            f"DELETE FROM document_stages WHERE case_id IN ({case_placeholders})",
            case_params,
        )
        counts["document_stages"] = max(cur.rowcount, 0)

        if doc_ids:
            doc_placeholders = _sql_placeholders(doc_ids)
            cur = await con.execute(
                f"DELETE FROM events WHERE case_id IN ({case_placeholders}) OR document_id IN ({doc_placeholders})",
                case_params + tuple(doc_ids),
            )
        else:
            cur = await con.execute(
                f"DELETE FROM events WHERE case_id IN ({case_placeholders})",
                case_params,
            )
        counts["events"] = max(cur.rowcount, 0)

        cur = await con.execute(
            f"DELETE FROM outbound_emails WHERE case_id IN ({case_placeholders})",
            case_params,
        )
        counts["outbound_emails"] = max(cur.rowcount, 0)

        cur = await con.execute(
            f"DELETE FROM documents WHERE case_id IN ({case_placeholders})",
            case_params,
        )
        counts["documents"] = max(cur.rowcount, 0)

        cur = await con.execute(
            f"DELETE FROM cases WHERE id IN ({case_placeholders})",
            case_params,
        )
        counts["cases"] = max(cur.rowcount, 0)

        await con.commit()

    return counts


def _split_member_name(member_name: str) -> tuple[str, str]:
    parts = [part for part in member_name.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _build_manual_payload_snapshot(
    body: OfficerCaseCreateRequest,
    *,
    request_id: str,
    grievance_id: str,
    grievance_number: str | None,
    member_name: str,
    officer_status: str,
) -> str:
    first_name, last_name = _split_member_name(member_name)
    snapshot = {
        "request_id": request_id,
        "source": _PAPER_SOURCE,
        "grievance_id": grievance_id,
        "grievance_number": grievance_number or "",
        "contract": _normalize_optional_text(body.contract) or "",
        "member_name": member_name,
        "grievant_name": member_name,
        "grievant_firstname": first_name,
        "grievant_lastname": last_name,
        "grievant_email": _normalize_optional_text(body.member_email) or "",
        "narrative": _normalize_optional_text(body.issue_summary) or "",
        "documents": [],
        "template_data": {
            "department": _normalize_optional_text(body.department) or "",
            "steward": _normalize_optional_text(body.steward) or "",
            "first_level_request_sent_date": _normalize_date_text(body.first_level_request_sent_date) or "",
            "second_level_request_sent_date": _normalize_date_text(body.second_level_request_sent_date) or "",
            "officer_status": officer_status,
        },
    }

    occurrence_date = _normalize_date_text(body.occurrence_date)
    if occurrence_date:
        snapshot["incident_date"] = occurrence_date

    return json.dumps(snapshot, ensure_ascii=False)


@router.get("/officers", response_class=HTMLResponse)
async def officers_page(request: Request):
    require_local_access(request)
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Officer Grievance Tracker</title>
  <style>
    :root {
      --sheet-green: #95cf46;
      --sheet-blue: #4f81bd;
      --sheet-border: #cfd5dc;
      --sheet-bg: #f7f8fa;
      --sheet-closed: #efefef;
      --sheet-text: #1f2933;
    }
    body {
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      margin: 20px;
      color: var(--sheet-text);
      background: linear-gradient(180deg, #ffffff 0%, #f4f7f9 100%);
    }
    h1, h2 { margin: 0 0 12px; }
    .panel {
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #dde4ea;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.05);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(180px, 1fr));
      gap: 12px;
    }
    .grid-wide {
      display: grid;
      grid-template-columns: repeat(2, minmax(280px, 1fr));
      gap: 12px;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 13px;
      font-weight: 600;
    }
    input, select, textarea {
      border: 1px solid #bfc8d2;
      border-radius: 8px;
      padding: 9px 10px;
      font: inherit;
      background: white;
    }
    textarea {
      min-height: 86px;
      resize: vertical;
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    button {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      background: #1f4d7a;
      color: white;
    }
    button.danger {
      background: #a11d2d;
    }
    button.secondary {
      background: #e6edf3;
      color: #203040;
    }
    .summary {
      margin: 8px 0 0;
      font-size: 13px;
      color: #4a5a68;
    }
    .table-wrap {
      overflow-x: auto;
      border-radius: 12px;
      border: 1px solid var(--sheet-border);
      background: white;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1500px;
    }
    th, td {
      border: 1px solid var(--sheet-border);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }
    th.select-col,
    td.select-col {
      position: sticky;
      left: 0;
      z-index: 2;
      width: 44px;
      min-width: 44px;
      text-align: center;
      box-shadow: 6px 0 10px rgba(15, 23, 42, 0.04);
    }
    th.actions-col,
    td.actions-col {
      position: sticky;
      right: 0;
      z-index: 2;
      min-width: 140px;
      box-shadow: -6px 0 10px rgba(15, 23, 42, 0.06);
    }
    th.actions-col {
      z-index: 3;
    }
    th.select-col {
      z-index: 4;
    }
    th.main {
      background: var(--sheet-green);
      color: white;
    }
    th.request {
      background: var(--sheet-blue);
      color: white;
    }
    td.actions-col {
      background: white;
    }
    td.select-col {
      background: white;
    }
    tr.closed-row td {
      background: var(--sheet-closed);
    }
    tr.closed-row td.select-col {
      background: var(--sheet-closed);
    }
    tr.closed-row td.actions-col {
      background: var(--sheet-closed);
    }
    .badge {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      background: #ebf2f7;
    }
    .badge.closed { background: #d7dde3; }
    .badge.waiting { background: #ffe8b5; }
    .badge.in_progress { background: #d8f1da; }
    .muted { color: #64748b; font-size: 12px; }
    .row-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .selection-summary {
      font-weight: 600;
      color: #203040;
    }
    .split {
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
      align-items: start;
    }
    pre {
      background: #101923;
      color: #e2e8f0;
      border-radius: 12px;
      padding: 12px;
      overflow: auto;
      max-height: 320px;
      margin: 0;
    }
    @media (max-width: 1200px) {
      .grid, .grid-wide, .split {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <h1>Officer Grievance Tracker</h1>

  <div class="panel">
    <h2>Filters</h2>
    <div class="grid">
      <label>Search
        <input id="filterSearch" placeholder="Grievance, name, department, issue..." />
      </label>
      <label>Assigned To
        <select id="filterAssignee"></select>
      </label>
      <label>Officer Status
        <select id="filterStatus">
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="in_progress">In Progress</option>
          <option value="waiting">Waiting</option>
          <option value="closed">Closed</option>
        </select>
      </label>
      <label>Source
        <select id="filterSource">
          <option value="">All sources</option>
          <option value="digital_intake">Digital intake</option>
          <option value="paper_manual">Paper manual</option>
        </select>
      </label>
    </div>
    <div class="actions">
      <button id="reloadBtn" type="button">Load Tracker</button>
      <button id="clearFiltersBtn" class="secondary" type="button">Clear Filters</button>
    </div>
    <div id="tableSummary" class="summary">Tracker not loaded yet.</div>
  </div>

  <div class="panel">
    <h2>Bulk Update</h2>
    <div id="bulkSummary" class="summary selection-summary">0 cases selected.</div>
    <div class="summary">Only filled fields are applied. Leave a field blank to keep current values.</div>
    <div class="grid" style="margin-top:12px;">
      <label>Officer Status
        <select id="bulkOfficerStatus">
          <option value="">Keep current status</option>
          <option value="open">Open</option>
          <option value="in_progress">In Progress</option>
          <option value="waiting">Waiting</option>
          <option value="closed">Closed</option>
        </select>
      </label>
      <label>Date Sent 1st Level Request
        <input id="bulkFirstLevelDate" type="date" />
      </label>
      <label>Date Sent 2nd Level Request
        <input id="bulkSecondLevelDate" type="date" />
      </label>
      <label>Assign To Roster
        <select id="bulkAssigneeSelect"></select>
      </label>
      <label>Manual Assignee Override
        <input id="bulkAssigneeManual" placeholder="Type any name/email" />
      </label>
      <label>Updated By
        <input id="bulkUpdatedBy" placeholder="Optional actor name" />
      </label>
    </div>
    <div class="grid-wide" style="margin-top:12px;">
      <label>Notes
        <textarea id="bulkOfficerNotes" placeholder="Optional bulk note update"></textarea>
      </label>
    </div>
    <div class="actions">
      <button id="applyBulkBtn" type="button">Apply To Checked Rows</button>
      <button id="deleteBulkBtn" class="danger" type="button">Delete Checked Rows</button>
      <button id="clearBulkSelectionBtn" class="secondary" type="button">Clear Checked Rows</button>
    </div>
  </div>

  <div class="split">
    <div class="panel">
      <h2>Manual Paper Entry</h2>
      <div class="grid">
        <label>Grievance Number
          <input id="createGrievanceNumber" placeholder="Optional display number" />
        </label>
        <label>Grievance ID
          <input id="createGrievanceId" placeholder="Optional internal/reference id" />
        </label>
        <label>Member Name
          <input id="createMemberName" placeholder="Required" />
        </label>
        <label>Member Email
          <input id="createMemberEmail" placeholder="Optional" />
        </label>
        <label>Department
          <input id="createDepartment" />
        </label>
        <label>Steward
          <input id="createSteward" />
        </label>
        <label>Date of Occurrence
          <input id="createOccurrenceDate" type="date" />
        </label>
        <label>Officer Status
          <select id="createOfficerStatus">
            <option value="open">Open</option>
            <option value="in_progress">In Progress</option>
            <option value="waiting">Waiting</option>
            <option value="closed">Closed</option>
          </select>
        </label>
        <label>Date Sent 1st Level Request
          <input id="createFirstLevelDate" type="date" />
        </label>
        <label>Date Sent 2nd Level Request
          <input id="createSecondLevelDate" type="date" />
        </label>
        <label>Assign To Roster
          <select id="createAssigneeSelect"></select>
        </label>
        <label>Manual Assignee Override
          <input id="createAssigneeManual" placeholder="Type any name/email" />
        </label>
        <label>Updated By
          <input id="createUpdatedBy" placeholder="Optional actor name" />
        </label>
      </div>
      <div class="grid-wide" style="margin-top:12px;">
        <label>Issue
          <textarea id="createIssueSummary" placeholder="Short grievance summary"></textarea>
        </label>
        <label>Notes
          <textarea id="createOfficerNotes" placeholder="Officer-only notes"></textarea>
        </label>
      </div>
      <div class="actions">
        <button id="createBtn" type="button">Create Paper Grievance</button>
      </div>
    </div>

    <div class="panel">
      <h2>Edit Selected Case</h2>
      <div id="editHint" class="summary">Select a table row to edit.</div>
      <input id="editCaseId" type="hidden" />
      <div class="grid">
        <label>Case ID
          <input id="editCaseIdDisplay" disabled />
        </label>
        <label>Workflow Status
          <input id="editWorkflowStatus" disabled />
        </label>
        <label>Source
          <input id="editSource" disabled />
        </label>
        <label>Grievance Number
          <input id="editGrievanceNumber" />
        </label>
        <label>Member Name
          <input id="editMemberName" />
        </label>
        <label>Member Email
          <input id="editMemberEmail" />
        </label>
        <label>Department
          <input id="editDepartment" />
        </label>
        <label>Steward
          <input id="editSteward" />
        </label>
        <label>Date of Occurrence
          <input id="editOccurrenceDate" type="date" />
        </label>
        <label>Officer Status
          <select id="editOfficerStatus">
            <option value="open">Open</option>
            <option value="in_progress">In Progress</option>
            <option value="waiting">Waiting</option>
            <option value="closed">Closed</option>
          </select>
        </label>
        <label>Date Sent 1st Level Request
          <input id="editFirstLevelDate" type="date" />
        </label>
        <label>Date Sent 2nd Level Request
          <input id="editSecondLevelDate" type="date" />
        </label>
        <label>Assign To Roster
          <select id="editAssigneeSelect"></select>
        </label>
        <label>Manual Assignee Override
          <input id="editAssigneeManual" placeholder="Type any name/email" />
        </label>
        <label>Updated By
          <input id="editUpdatedBy" placeholder="Optional actor name" />
        </label>
      </div>
      <div class="grid-wide" style="margin-top:12px;">
        <label>Issue
          <textarea id="editIssueSummary"></textarea>
        </label>
        <label>Notes
          <textarea id="editOfficerNotes"></textarea>
        </label>
      </div>
      <div class="actions">
        <button id="saveEditBtn" type="button">Save Edits</button>
        <button id="clearEditBtn" class="secondary" type="button">Clear Selection</button>
      </div>
      <div id="editMeta" class="summary"></div>
    </div>
  </div>

  <div class="panel">
    <h2>Tracker Table</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="main select-col"><input id="selectAllRows" type="checkbox" aria-label="Select all rows" /></th>
            <th class="main">Grievance Number</th>
            <th class="main">Department</th>
            <th class="main">Name</th>
            <th class="main">Steward</th>
            <th class="main">Date of Occurrence</th>
            <th class="main">Issue</th>
            <th class="request">Date Sent 1st Level Request</th>
            <th class="request">Date Sent 2nd Level Request</th>
            <th class="main">Assigned To</th>
            <th class="main">Officer Status</th>
            <th class="main">Workflow Status</th>
            <th class="main">Source</th>
            <th class="main actions-col">Actions</th>
          </tr>
        </thead>
        <tbody id="tableBody">
          <tr><td colspan="14">No cases loaded.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>Last Response</h2>
    <pre id="out">Ready.</pre>
  </div>

  <script>
    const out = document.getElementById('out');
    const tableBody = document.getElementById('tableBody');
    const tableSummary = document.getElementById('tableSummary');
    const filterAssignee = document.getElementById('filterAssignee');
    const createAssigneeSelect = document.getElementById('createAssigneeSelect');
    const editAssigneeSelect = document.getElementById('editAssigneeSelect');
    const bulkAssigneeSelect = document.getElementById('bulkAssigneeSelect');
    const bulkSummary = document.getElementById('bulkSummary');
    const selectAllRows = document.getElementById('selectAllRows');
    const currentRows = new Map();
    const selectedCaseIds = new Set();
    let currentRoster = [];

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
      const text = await res.text();
      let data = text;
      try { data = JSON.parse(text); } catch {}
      if (!res.ok) throw { status: res.status, data };
      return data;
    }

    function valueOf(id) {
      return document.getElementById(id).value.trim();
    }

    function nullableValue(id) {
      const value = valueOf(id);
      return value || null;
    }

    function assigneeValue(selectId, manualId) {
      const manual = valueOf(manualId);
      if (manual) return manual;
      const selected = valueOf(selectId);
      return selected || null;
    }

    function labelForStatus(value) {
      if (value === 'in_progress') return 'In Progress';
      if (value === 'closed') return 'Closed';
      if (value === 'waiting') return 'Waiting';
      return 'Open';
    }

    function labelForSource(value) {
      return value === 'paper_manual' ? 'Paper Manual' : 'Digital Intake';
    }

    function populateAssigneeOptions(selectEl, options, selectedValue, emptyLabel) {
      const seen = new Set();
      const values = [];
      for (const option of options) {
        const text = String(option || '').trim();
        if (!text || seen.has(text.toLowerCase())) continue;
        seen.add(text.toLowerCase());
        values.push(text);
      }
      selectEl.innerHTML = `<option value="">${esc(emptyLabel)}</option>` + values
        .sort((a, b) => a.localeCompare(b))
        .map((option) => `<option value="${esc(option)}">${esc(option)}</option>`)
        .join('');
      if (selectedValue) selectEl.value = selectedValue;
    }

    function refreshRosterOptions(rows, selectedFilter) {
      const derived = [];
      for (const row of rows) {
        if (row.officer_assignee) derived.push(row.officer_assignee);
      }
      const options = [...currentRoster, ...derived];
      populateAssigneeOptions(filterAssignee, options, selectedFilter, 'All assignees');
      populateAssigneeOptions(createAssigneeSelect, options, valueOf('createAssigneeSelect'), 'Roster assignee');
      populateAssigneeOptions(editAssigneeSelect, options, valueOf('editAssigneeSelect'), 'Roster assignee');
      populateAssigneeOptions(bulkAssigneeSelect, options, valueOf('bulkAssigneeSelect'), 'Keep current assignee');
    }

    function updateSelectionUi() {
      const visibleCaseIds = [...currentRows.keys()];
      const selectedVisible = visibleCaseIds.filter((caseId) => selectedCaseIds.has(caseId));
      bulkSummary.textContent = `${selectedVisible.length} case(s) checked.`;
      if (!visibleCaseIds.length) {
        selectAllRows.checked = false;
        selectAllRows.indeterminate = false;
        return;
      }
      selectAllRows.checked = selectedVisible.length > 0 && selectedVisible.length === visibleCaseIds.length;
      selectAllRows.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleCaseIds.length;
    }

    function clearBulkSelection() {
      selectedCaseIds.clear();
      updateSelectionUi();
      for (const checkbox of tableBody.querySelectorAll('input[data-select-case-id]')) {
        checkbox.checked = false;
      }
    }

    function resetBulkForm() {
      for (const id of [
        'bulkFirstLevelDate',
        'bulkSecondLevelDate',
        'bulkAssigneeManual',
        'bulkUpdatedBy',
        'bulkOfficerNotes'
      ]) {
        document.getElementById(id).value = '';
      }
      document.getElementById('bulkOfficerStatus').value = '';
      document.getElementById('bulkAssigneeSelect').value = '';
    }

    function renderRows(rows) {
      currentRows.clear();
      for (const row of rows) currentRows.set(row.case_id, row);
      for (const caseId of [...selectedCaseIds]) {
        if (!currentRows.has(caseId)) selectedCaseIds.delete(caseId);
      }
      const selectedFilter = valueOf('filterAssignee');
      refreshRosterOptions(rows, selectedFilter);
      updateSelectionUi();

      tableSummary.textContent = `${rows.length} case(s) loaded.`;
      if (!rows.length) {
        tableBody.innerHTML = '<tr><td colspan="14">No matching grievances found.</td></tr>';
        return;
      }

      tableBody.innerHTML = rows.map((row) => `
        <tr class="${row.officer_status === 'closed' ? 'closed-row' : ''}">
          <td class="select-col">
            <input type="checkbox" data-select-case-id="${esc(row.case_id)}" ${selectedCaseIds.has(row.case_id) ? 'checked' : ''} />
          </td>
          <td>${esc(row.display_grievance)}<div class="muted">${esc(row.case_id)}</div></td>
          <td>${esc(row.department || '')}</td>
          <td>${esc(row.member_name || '')}</td>
          <td>${esc(row.steward || '')}</td>
          <td>${esc(row.occurrence_date || '')}</td>
          <td>${esc(row.issue_summary || '')}</td>
          <td>${esc(row.first_level_request_sent_date || '')}</td>
          <td>${esc(row.second_level_request_sent_date || '')}</td>
          <td>${esc(row.officer_assignee || '')}</td>
          <td><span class="badge ${esc(row.officer_status)}">${esc(labelForStatus(row.officer_status))}</span></td>
          <td>${esc(row.workflow_status || '')}</td>
          <td>${esc(labelForSource(row.officer_source))}</td>
          <td class="actions-col">
            <div class="row-actions">
              <button type="button" data-action="edit" data-case-id="${esc(row.case_id)}">Edit</button>
              <button type="button" class="danger" data-action="delete" data-case-id="${esc(row.case_id)}">Delete</button>
            </div>
          </td>
        </tr>
      `).join('');
      updateSelectionUi();
    }

    function queryString() {
      const params = new URLSearchParams();
      const search = valueOf('filterSearch');
      const assignee = valueOf('filterAssignee');
      const officerStatus = valueOf('filterStatus');
      const source = valueOf('filterSource');
      if (search) params.set('search', search);
      if (assignee) params.set('assignee', assignee);
      if (officerStatus) params.set('officer_status', officerStatus);
      if (source) params.set('source', source);
      const encoded = params.toString();
      return encoded ? `?${encoded}` : '';
    }

    async function loadCases() {
      try {
        const data = await call(`/officers/cases${queryString()}`);
        currentRoster = Array.isArray(data.roster) ? data.roster : [];
        renderRows(Array.isArray(data.rows) ? data.rows : []);
        show(data);
      } catch (e) {
        show(e);
      }
    }

    function resetCreateForm() {
      for (const id of [
        'createGrievanceNumber', 'createGrievanceId', 'createMemberName', 'createMemberEmail',
        'createDepartment', 'createSteward', 'createOccurrenceDate', 'createFirstLevelDate',
        'createSecondLevelDate', 'createAssigneeManual', 'createUpdatedBy',
        'createIssueSummary', 'createOfficerNotes'
      ]) {
        document.getElementById(id).value = '';
      }
      document.getElementById('createOfficerStatus').value = 'open';
      document.getElementById('createAssigneeSelect').value = '';
    }

    async function createCase() {
      const payload = {
        grievance_number: nullableValue('createGrievanceNumber'),
        grievance_id: nullableValue('createGrievanceId'),
        member_name: valueOf('createMemberName'),
        member_email: nullableValue('createMemberEmail'),
        department: nullableValue('createDepartment'),
        steward: nullableValue('createSteward'),
        occurrence_date: nullableValue('createOccurrenceDate'),
        issue_summary: nullableValue('createIssueSummary'),
        first_level_request_sent_date: nullableValue('createFirstLevelDate'),
        second_level_request_sent_date: nullableValue('createSecondLevelDate'),
        officer_assignee: assigneeValue('createAssigneeSelect', 'createAssigneeManual'),
        officer_notes: nullableValue('createOfficerNotes'),
        officer_status: valueOf('createOfficerStatus') || 'open',
        updated_by: nullableValue('createUpdatedBy')
      };
      try {
        const data = await call('/officers/cases', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        resetCreateForm();
        show(data);
        await loadCases();
      } catch (e) {
        show(e);
      }
    }

    async function deleteCase(caseId) {
      const row = currentRows.get(caseId);
      if (!row) {
        return show({ error: 'Case not found in current table view.' });
      }
      const confirmed = window.confirm(
        `Permanently delete ${row.display_grievance} and its related records?`
      );
      if (!confirmed) return;
      try {
        const data = await call(`/officers/cases/${encodeURIComponent(caseId)}`, {
          method: 'DELETE'
        });
        selectedCaseIds.delete(caseId);
        if (valueOf('editCaseId') === caseId) clearEditSelection();
        show(data);
        await loadCases();
      } catch (e) {
        show(e);
      }
    }

    async function applyBulkUpdate() {
      const caseIds = [...selectedCaseIds].filter((caseId) => currentRows.has(caseId));
      if (!caseIds.length) {
        return show({ error: 'Check at least one case first.' });
      }
      const payload = { case_ids: caseIds };
      const officerStatus = valueOf('bulkOfficerStatus');
      const firstLevelDate = nullableValue('bulkFirstLevelDate');
      const secondLevelDate = nullableValue('bulkSecondLevelDate');
      const assignee = assigneeValue('bulkAssigneeSelect', 'bulkAssigneeManual');
      const officerNotes = nullableValue('bulkOfficerNotes');
      const updatedBy = nullableValue('bulkUpdatedBy');

      if (officerStatus) payload.officer_status = officerStatus;
      if (firstLevelDate) payload.first_level_request_sent_date = firstLevelDate;
      if (secondLevelDate) payload.second_level_request_sent_date = secondLevelDate;
      if (assignee) payload.officer_assignee = assignee;
      if (officerNotes) payload.officer_notes = officerNotes;
      if (updatedBy) payload.updated_by = updatedBy;

      if (Object.keys(payload).length === 1) {
        return show({ error: 'Set at least one bulk field before applying.' });
      }

      try {
        const data = await call('/officers/cases/bulk', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        resetBulkForm();
        clearBulkSelection();
        show(data);
        await loadCases();
      } catch (e) {
        show(e);
      }
    }

    async function deleteSelectedCases() {
      const caseIds = [...selectedCaseIds].filter((caseId) => currentRows.has(caseId));
      if (!caseIds.length) {
        return show({ error: 'Check at least one case first.' });
      }
      const confirmed = window.confirm(
        `Permanently delete ${caseIds.length} checked case(s) and their related records?`
      );
      if (!confirmed) return;
      try {
        const data = await call('/officers/cases/bulk-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ case_ids: caseIds })
        });
        if (caseIds.includes(valueOf('editCaseId'))) clearEditSelection();
        clearBulkSelection();
        show(data);
        await loadCases();
      } catch (e) {
        show(e);
      }
    }

    function clearEditSelection() {
      for (const id of [
        'editCaseId', 'editCaseIdDisplay', 'editWorkflowStatus', 'editSource', 'editGrievanceNumber',
        'editMemberName', 'editMemberEmail', 'editDepartment', 'editSteward', 'editOccurrenceDate',
        'editFirstLevelDate', 'editSecondLevelDate', 'editAssigneeManual', 'editUpdatedBy',
        'editIssueSummary', 'editOfficerNotes'
      ]) {
        document.getElementById(id).value = '';
      }
      document.getElementById('editOfficerStatus').value = 'open';
      document.getElementById('editAssigneeSelect').value = '';
      document.getElementById('editHint').textContent = 'Select a table row to edit.';
      document.getElementById('editMeta').textContent = '';
    }

    function startEdit(caseId) {
      const row = currentRows.get(caseId);
      if (!row) return;
      document.getElementById('editCaseId').value = row.case_id || '';
      document.getElementById('editCaseIdDisplay').value = row.case_id || '';
      document.getElementById('editWorkflowStatus').value = row.workflow_status || '';
      document.getElementById('editSource').value = labelForSource(row.officer_source || '');
      document.getElementById('editGrievanceNumber').value = row.grievance_number || '';
      document.getElementById('editMemberName').value = row.member_name || '';
      document.getElementById('editMemberEmail').value = row.member_email || '';
      document.getElementById('editDepartment').value = row.department || '';
      document.getElementById('editSteward').value = row.steward || '';
      document.getElementById('editOccurrenceDate').value = row.occurrence_date || '';
      document.getElementById('editOfficerStatus').value = row.officer_status || 'open';
      document.getElementById('editFirstLevelDate').value = row.first_level_request_sent_date || '';
      document.getElementById('editSecondLevelDate').value = row.second_level_request_sent_date || '';
      document.getElementById('editAssigneeManual').value = '';
      document.getElementById('editAssigneeSelect').value = row.officer_assignee || '';
      document.getElementById('editIssueSummary').value = row.issue_summary || '';
      document.getElementById('editOfficerNotes').value = row.officer_notes || '';
      document.getElementById('editHint').textContent = `Editing ${row.display_grievance}`;
      document.getElementById('editMeta').textContent = row.officer_closed_at_utc
        ? `Closed ${row.officer_closed_at_utc}${row.officer_closed_by ? ` by ${row.officer_closed_by}` : ''}`
        : '';
    }

    async function saveEdit() {
      const caseId = valueOf('editCaseId');
      if (!caseId) {
        return show({ error: 'Select a case first.' });
      }
      const payload = {
        grievance_number: nullableValue('editGrievanceNumber'),
        member_name: nullableValue('editMemberName'),
        member_email: nullableValue('editMemberEmail'),
        department: nullableValue('editDepartment'),
        steward: nullableValue('editSteward'),
        occurrence_date: nullableValue('editOccurrenceDate'),
        issue_summary: nullableValue('editIssueSummary'),
        first_level_request_sent_date: nullableValue('editFirstLevelDate'),
        second_level_request_sent_date: nullableValue('editSecondLevelDate'),
        officer_assignee: assigneeValue('editAssigneeSelect', 'editAssigneeManual'),
        officer_notes: nullableValue('editOfficerNotes'),
        officer_status: valueOf('editOfficerStatus') || 'open',
        updated_by: nullableValue('editUpdatedBy')
      };
      try {
        const data = await call(`/officers/cases/${encodeURIComponent(caseId)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        show(data);
        await loadCases();
        startEdit(caseId);
      } catch (e) {
        show(e);
      }
    }

    tableBody.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-case-id][data-action]');
      if (!button) return;
      const caseId = button.dataset.caseId || '';
      if (button.dataset.action === 'delete') {
        void deleteCase(caseId);
        return;
      }
      startEdit(caseId);
    });
    tableBody.addEventListener('change', (event) => {
      const checkbox = event.target.closest('input[data-select-case-id]');
      if (!checkbox) return;
      const caseId = checkbox.dataset.selectCaseId || '';
      if (!caseId) return;
      if (checkbox.checked) selectedCaseIds.add(caseId);
      else selectedCaseIds.delete(caseId);
      updateSelectionUi();
    });

    document.getElementById('reloadBtn').addEventListener('click', () => { void loadCases(); });
    document.getElementById('clearFiltersBtn').addEventListener('click', () => {
      document.getElementById('filterSearch').value = '';
      document.getElementById('filterAssignee').value = '';
      document.getElementById('filterStatus').value = '';
      document.getElementById('filterSource').value = '';
      void loadCases();
    });
    document.getElementById('applyBulkBtn').addEventListener('click', () => { void applyBulkUpdate(); });
    document.getElementById('deleteBulkBtn').addEventListener('click', () => { void deleteSelectedCases(); });
    document.getElementById('clearBulkSelectionBtn').addEventListener('click', clearBulkSelection);
    document.getElementById('createBtn').addEventListener('click', () => { void createCase(); });
    document.getElementById('saveEditBtn').addEventListener('click', () => { void saveEdit(); });
    document.getElementById('clearEditBtn').addEventListener('click', clearEditSelection);
    selectAllRows.addEventListener('change', () => {
      if (selectAllRows.checked) {
        for (const caseId of currentRows.keys()) selectedCaseIds.add(caseId);
      } else {
        for (const caseId of currentRows.keys()) selectedCaseIds.delete(caseId);
      }
      for (const checkbox of tableBody.querySelectorAll('input[data-select-case-id]')) {
        checkbox.checked = selectAllRows.checked;
      }
      updateSelectionUi();
    });
    window.addEventListener('DOMContentLoaded', () => {
      populateAssigneeOptions(filterAssignee, [], '', 'All assignees');
      populateAssigneeOptions(createAssigneeSelect, [], '', 'Roster assignee');
      populateAssigneeOptions(editAssigneeSelect, [], '', 'Roster assignee');
      populateAssigneeOptions(bulkAssigneeSelect, [], '', 'Keep current assignee');
      updateSelectionUi();
      void loadCases();
    });
  </script>
</body>
</html>
"""


@router.get("/officers/cases", response_model=OfficerCaseListResponse)
async def officer_cases(
    request: Request,
    search: str | None = None,
    assignee: str | None = None,
    officer_status: str | None = None,
    source: str | None = None,
):
    require_local_access(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    rows = await _load_officer_case_rows(db)
    filtered = [
        row
        for row in rows
        if _case_matches_filters(
            row,
            search=search,
            assignee=assignee,
            officer_status=officer_status,
            source=source,
        )
    ]
    return OfficerCaseListResponse(
        rows=filtered,
        roster=list(cfg.officer_tracking.roster),
        count=len(filtered),
    )


@router.delete("/officers/cases/{case_id}", response_model=OfficerCaseDeleteResponse)
async def delete_officer_case(case_id: str, request: Request):
    require_local_access(request)
    db: Db = request.app.state.db

    case_row = await _load_officer_case_row(db, case_id)
    deleted = await _delete_cases_with_related_rows(db, [case_id])
    if deleted["cases"] == 0:
        raise HTTPException(status_code=404, detail="case_id not found")

    return OfficerCaseDeleteResponse(
        case_id=case_row.case_id,
        grievance_id=case_row.grievance_id,
        grievance_number=case_row.grievance_number,
        display_grievance=case_row.display_grievance,
        deleted_case_count=deleted["cases"],
        deleted_document_count=deleted["documents"],
        deleted_document_stage_count=deleted["document_stages"],
        deleted_stage_artifact_count=deleted["document_stage_artifacts"],
        deleted_stage_field_value_count=deleted["document_stage_field_values"],
        deleted_event_count=deleted["events"],
        deleted_outbound_email_count=deleted["outbound_emails"],
    )


@router.patch("/officers/cases/bulk", response_model=OfficerCaseBulkUpdateResponse)
async def bulk_update_officer_cases(body: OfficerCaseBulkUpdateRequest, request: Request):
    require_local_access(request)
    db: Db = request.app.state.db

    case_ids = [str(case_id or "").strip() for case_id in body.case_ids if str(case_id or "").strip()]
    deduped_case_ids = list(dict.fromkeys(case_ids))
    if not deduped_case_ids:
        raise HTTPException(status_code=400, detail="case_ids is required")

    requested_fields = sorted(field for field in body.model_fields_set if field not in {"case_ids", "updated_by"})
    if not requested_fields:
        raise HTTPException(status_code=400, detail="no bulk changes supplied")

    rows_by_id = await _load_officer_case_rows_by_id(db, deduped_case_ids)
    missing_case_ids = [case_id for case_id in deduped_case_ids if case_id not in rows_by_id]
    if missing_case_ids:
        raise HTTPException(status_code=404, detail=f"case_id not found: {', '.join(missing_case_ids)}")

    updated_case_ids: list[str] = []
    for case_id in deduped_case_ids:
        current_row = rows_by_id[case_id]
        updates, updated_by = _case_update_fields(body, current_row=current_row)
        if not updates:
            continue
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = tuple(updates.values()) + (case_id,)
        await db.exec(f"UPDATE cases SET {assignments} WHERE id=?", params)
        await db.add_event(
            case_id,
            None,
            "officer_case_updated",
            {
                "updated_by": updated_by,
                "changes": updates,
                "bulk_update": True,
            },
        )
        updated_case_ids.append(case_id)

    return OfficerCaseBulkUpdateResponse(
        selected_case_count=len(deduped_case_ids),
        updated_case_count=len(updated_case_ids),
        case_ids=updated_case_ids,
        changed_fields=requested_fields,
    )


@router.post("/officers/cases/bulk-delete", response_model=OfficerCaseBulkDeleteResponse)
async def bulk_delete_officer_cases(body: OfficerCaseBulkDeleteRequest, request: Request):
    require_local_access(request)
    db: Db = request.app.state.db

    case_ids = [str(case_id or "").strip() for case_id in body.case_ids if str(case_id or "").strip()]
    deduped_case_ids = list(dict.fromkeys(case_ids))
    if not deduped_case_ids:
        raise HTTPException(status_code=400, detail="case_ids is required")

    rows_by_id = await _load_officer_case_rows_by_id(db, deduped_case_ids)
    missing_case_ids = [case_id for case_id in deduped_case_ids if case_id not in rows_by_id]
    if missing_case_ids:
        raise HTTPException(status_code=404, detail=f"case_id not found: {', '.join(missing_case_ids)}")

    deleted = await _delete_cases_with_related_rows(db, deduped_case_ids)
    return OfficerCaseBulkDeleteResponse(
        selected_case_count=len(deduped_case_ids),
        deleted_case_count=deleted["cases"],
        deleted_case_ids=deduped_case_ids,
        deleted_document_count=deleted["documents"],
        deleted_document_stage_count=deleted["document_stages"],
        deleted_stage_artifact_count=deleted["document_stage_artifacts"],
        deleted_stage_field_value_count=deleted["document_stage_field_values"],
        deleted_event_count=deleted["events"],
        deleted_outbound_email_count=deleted["outbound_emails"],
    )


@router.post("/officers/cases", response_model=OfficerCaseRow)
async def create_officer_case(body: OfficerCaseCreateRequest, request: Request):
    require_local_access(request)
    db: Db = request.app.state.db

    case_id = new_case_id()
    grievance_id = normalize_grievance_id(body.grievance_id) or new_grievance_id()
    grievance_number = _normalize_optional_text(body.grievance_number)
    member_name = _normalize_member_name(body.member_name)
    member_email = _normalize_optional_text(body.member_email)
    department = _normalize_optional_text(body.department)
    steward = _normalize_optional_text(body.steward)
    occurrence_date = _normalize_date_text(body.occurrence_date)
    issue_summary = _normalize_optional_text(body.issue_summary)
    first_level_request_sent_date = _normalize_date_text(body.first_level_request_sent_date)
    second_level_request_sent_date = _normalize_date_text(body.second_level_request_sent_date)
    officer_assignee = _normalize_optional_text(body.officer_assignee)
    officer_notes = _normalize_optional_text(body.officer_notes)
    officer_status = _normalize_officer_status(body.officer_status, default="open")
    updated_by = _actor_label(body.updated_by, fallback=officer_assignee or "officer-ui")
    request_id = f"officer-manual-{time.time_ns()}"
    closed_at_utc = utcnow() if officer_status == "closed" else None
    closed_by = updated_by if officer_status == "closed" else None

    await db.exec(
        """INSERT INTO cases(
             id, grievance_id, created_at_utc, status, approval_status,
             grievance_number, member_name, member_email, intake_request_id, intake_payload_json,
             sharepoint_case_folder, sharepoint_case_web_url,
             officer_status, officer_assignee, officer_notes, officer_source,
             officer_closed_at_utc, officer_closed_by,
             tracking_department, tracking_steward, tracking_occurrence_date,
             tracking_issue_summary, tracking_first_level_request_sent_date,
             tracking_second_level_request_sent_date
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            case_id,
            grievance_id,
            utcnow(),
            _MANUAL_TRACKING_STATUS,
            "pending",
            grievance_number,
            member_name,
            member_email,
            request_id,
            _build_manual_payload_snapshot(
                body,
                request_id=request_id,
                grievance_id=grievance_id,
                grievance_number=grievance_number,
                member_name=member_name,
                officer_status=officer_status,
            ),
            None,
            None,
            officer_status,
            officer_assignee,
            officer_notes,
            _PAPER_SOURCE,
            closed_at_utc,
            closed_by,
            department,
            steward,
            occurrence_date,
            issue_summary,
            first_level_request_sent_date,
            second_level_request_sent_date,
        ),
    )
    await db.add_event(
        case_id,
        None,
        "officer_case_created",
        {
            "source": _PAPER_SOURCE,
            "workflow_status": _MANUAL_TRACKING_STATUS,
            "officer_status": officer_status,
            "officer_assignee": officer_assignee,
            "updated_by": updated_by,
            "grievance_number": grievance_number,
            "request_id": request_id,
        },
    )
    return await _load_officer_case_row(db, case_id)


@router.patch("/officers/cases/{case_id}", response_model=OfficerCaseRow)
async def update_officer_case(case_id: str, body: OfficerCaseUpdateRequest, request: Request):
    require_local_access(request)
    db: Db = request.app.state.db

    current_row = await db.fetchone(f"{_CASE_SELECT_SQL} WHERE id=?", (case_id,))
    if not current_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    fields = body.model_fields_set
    if not fields:
        return _build_officer_case_row(current_row)

    updates, updated_by = _case_update_fields(body, current_row=current_row)
    if not updates:
        return _build_officer_case_row(current_row)

    assignments = ", ".join(f"{column}=?" for column in updates)
    params = tuple(updates.values()) + (case_id,)
    await db.exec(f"UPDATE cases SET {assignments} WHERE id=?", params)
    await db.add_event(
        case_id,
        None,
        "officer_case_updated",
        {
            "updated_by": updated_by,
            "changes": updates,
        },
    )
    return await _load_officer_case_row(db, case_id)
