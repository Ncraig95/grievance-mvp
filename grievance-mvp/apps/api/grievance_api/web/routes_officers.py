from __future__ import annotations

import json
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.ids import new_case_id, new_grievance_id, normalize_grievance_id
from ..db.db import Db, utcnow
from ..services.contract_timeline import parse_incident_date
from .admin_common import parse_json_safely
from .models import (
    ChiefStewardAssignmentCreateRequest,
    ChiefStewardAssignmentListResponse,
    ChiefStewardAssignmentRow,
    DirectoryUserRow,
    DirectoryUserSearchResponse,
    OfficerCaseBulkDeleteRequest,
    OfficerCaseBulkDeleteResponse,
    OfficerCaseBulkUpdateRequest,
    OfficerCaseBulkUpdateResponse,
    OfficerCaseCreateRequest,
    OfficerCaseDeleteResponse,
    OfficerCaseEventRow,
    OfficerCaseEventsResponse,
    OfficerCaseListResponse,
    OfficerCaseRow,
    OfficerCaseUpdateRequest,
    OfficerViewerContext,
)
from .officer_auth import (
    OfficerUserContext,
    actor_identity,
    audit_actor_details,
    normalize_scope_key,
    officer_auth_enabled,
    require_admin_user,
    require_authenticated_officer,
    require_case_edit_access,
    require_officer_page_access,
    resolve_contract_scope,
    user_can_view_case,
)

router = APIRouter()

_OFFICER_STATUS_VALUES = {
    "open",
    "in_progress",
    "waiting",
    "closed",
    "open_at_state",
    "open_at_national",
}
_PAPER_SOURCE = "paper_manual"
_DIGITAL_SOURCE = "digital_intake"
_MANUAL_TRACKING_STATUS = "manual_tracking"
_FINAL_WORKFLOW_STATUSES = {"approved", "rejected", "uploaded"}

_CASE_SELECT_SQL = """
    SELECT id, grievance_id, grievance_number, member_name, member_email,
           created_at_utc, status, approval_status, intake_payload_json,
           officer_status, officer_assignee, officer_notes, officer_source,
           officer_closed_at_utc, officer_closed_by, tracking_contract,
           tracking_department, tracking_steward, tracking_occurrence_date,
           tracking_issue_summary, tracking_first_level_request_sent_date,
           tracking_second_level_request_sent_date,
           tracking_third_level_request_sent_date, tracking_fourth_level_request_sent_date
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


def _build_viewer_model(user: OfficerUserContext) -> OfficerViewerContext:
    return OfficerViewerContext(
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        contract_scopes=list(user.contract_scopes),
        auth_enabled=user.auth_enabled,
        can_create=user.can_create,
        can_edit=user.can_edit,
        can_delete=user.can_delete,
        can_bulk_edit=user.can_bulk_edit,
        can_bulk_delete=user.can_bulk_delete,
        can_view_audit=user.can_view_audit,
        can_manage_chief_assignments=user.can_manage_chief_assignments,
    )


def _build_officer_case_row(cfg, row: tuple[object, ...]) -> OfficerCaseRow:  # noqa: ANN001
    payload = _payload_dict(row[8])
    grievance_id = str(row[1] or "").strip()
    grievance_number = _normalize_optional_text(row[2])
    member_name = _normalize_optional_text(row[3]) or _payload_member_name(payload) or "Unknown"
    member_email = _normalize_optional_text(row[4]) or _payload_pick(payload, "member_email", "grievant_email")
    workflow_status = str(row[6] or "").strip()
    contract = _normalize_optional_text(row[15]) or _payload_pick(payload, "contract")
    contract_scope = resolve_contract_scope(cfg, contract)
    department = _normalize_optional_text(row[16]) or _payload_pick(
        payload,
        "department",
        "q2_department",
        "q2a_other_department",
    )
    steward = _normalize_optional_text(row[17]) or _fallback_steward(payload)
    occurrence_date = _normalize_optional_text(row[18]) or _normalize_date_text(
        _payload_pick(payload, "incident_date", "q1_occurred_date", "date_grievance_occurred")
    )
    issue_summary = _normalize_optional_text(row[19]) or _payload_pick(
        payload,
        "issue_summary",
        "issue_text",
        "issue_contract_section",
        "q3_union_statement",
        "narrative",
    )
    first_level_request_sent_date = _normalize_optional_text(row[20]) or _normalize_date_text(
        _payload_pick(payload, "first_level_request_sent_date", "date_sent_first_level_request")
    )
    second_level_request_sent_date = _normalize_optional_text(row[21]) or _normalize_date_text(
        _payload_pick(payload, "second_level_request_sent_date", "date_sent_second_level_request")
    )
    third_level_request_sent_date = _normalize_optional_text(row[22]) or _normalize_date_text(
        _payload_pick(payload, "third_level_request_sent_date", "date_sent_third_level_request")
    )
    fourth_level_request_sent_date = _normalize_optional_text(row[23]) or _normalize_date_text(
        _payload_pick(payload, "fourth_level_request_sent_date", "date_sent_fourth_level_request")
    )

    return OfficerCaseRow(
        case_id=str(row[0]),
        grievance_id=grievance_id,
        grievance_number=grievance_number,
        display_grievance=grievance_number or grievance_id,
        contract=contract,
        contract_scope=contract_scope,
        member_name=member_name,
        member_email=member_email,
        department=department,
        steward=steward,
        occurrence_date=occurrence_date,
        issue_summary=issue_summary,
        first_level_request_sent_date=first_level_request_sent_date,
        second_level_request_sent_date=second_level_request_sent_date,
        third_level_request_sent_date=third_level_request_sent_date,
        fourth_level_request_sent_date=fourth_level_request_sent_date,
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
    contract_scope: str | None,
) -> bool:
    search_text = str(search or "").strip().lower()
    assignee_text = str(assignee or "").strip().lower()
    status_text = str(officer_status or "").strip().lower()
    source_text = str(source or "").strip().lower()
    contract_scope_text = normalize_scope_key(contract_scope)

    if assignee_text and str(row.officer_assignee or "").strip().lower() != assignee_text:
        return False
    if status_text and row.officer_status != status_text:
        return False
    if source_text and row.officer_source != source_text:
        return False
    if contract_scope_text and normalize_scope_key(row.contract_scope) != contract_scope_text:
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
            row.contract or "",
            row.contract_scope or "",
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


async def _load_officer_case_rows(db: Db, *, cfg) -> list[OfficerCaseRow]:  # noqa: ANN001
    rows = await db.fetchall(f"{_CASE_SELECT_SQL} ORDER BY created_at_utc DESC, id DESC")
    return [_build_officer_case_row(cfg, row) for row in rows]


async def _load_officer_case_row(db: Db, *, cfg, case_id: str) -> OfficerCaseRow:  # noqa: ANN001
    row = await db.fetchone(f"{_CASE_SELECT_SQL} WHERE id=?", (case_id,))
    if not row:
        raise HTTPException(status_code=404, detail="case_id not found")
    return _build_officer_case_row(cfg, row)


async def _load_officer_case_rows_by_id(db: Db, case_ids: list[str]) -> dict[str, tuple[object, ...]]:
    if not case_ids:
        return {}
    placeholders = _sql_placeholders(case_ids)
    rows = await db.fetchall(f"{_CASE_SELECT_SQL} WHERE id IN ({placeholders})", tuple(case_ids))
    return {str(row[0]): row for row in rows}


def _case_update_fields(
    body: OfficerCaseUpdateRequest,
    *,
    current_row: tuple[object, ...],
    user: OfficerUserContext,
) -> tuple[dict[str, object], str]:
    fields = set(body.model_fields_set)
    fields.discard("updated_by")
    fields.discard("case_ids")

    current_effective_status = _effective_officer_status(current_row[9], current_row[6])
    fallback_actor = _normalize_optional_text(current_row[10]) or "officer-ui"
    updated_by = actor_identity(user, fallback=fallback_actor)

    updates: dict[str, object] = {}
    if "grievance_number" in fields:
        updates["grievance_number"] = _normalize_optional_text(body.grievance_number)
    if "member_name" in fields:
        updates["member_name"] = _normalize_member_name(body.member_name)
    if "member_email" in fields:
        updates["member_email"] = _normalize_optional_text(body.member_email)
    if "contract" in fields:
        updates["tracking_contract"] = _normalize_optional_text(body.contract)
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
    if "third_level_request_sent_date" in fields:
        updates["tracking_third_level_request_sent_date"] = _normalize_date_text(body.third_level_request_sent_date)
    if "fourth_level_request_sent_date" in fields:
        updates["tracking_fourth_level_request_sent_date"] = _normalize_date_text(body.fourth_level_request_sent_date)
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
            "contract": _normalize_optional_text(body.contract) or "",
            "department": _normalize_optional_text(body.department) or "",
            "steward": _normalize_optional_text(body.steward) or "",
            "first_level_request_sent_date": _normalize_date_text(body.first_level_request_sent_date) or "",
            "second_level_request_sent_date": _normalize_date_text(body.second_level_request_sent_date) or "",
            "third_level_request_sent_date": _normalize_date_text(body.third_level_request_sent_date) or "",
            "fourth_level_request_sent_date": _normalize_date_text(body.fourth_level_request_sent_date) or "",
            "officer_status": officer_status,
        },
    }

    occurrence_date = _normalize_date_text(body.occurrence_date)
    if occurrence_date:
        snapshot["incident_date"] = occurrence_date

    return json.dumps(snapshot, ensure_ascii=False)


def _user_can_select_rows(user: OfficerUserContext) -> bool:
    return user.can_bulk_edit or user.can_bulk_delete


def _configured_contract_scopes(cfg) -> list[str]:  # noqa: ANN001
    return sorted(str(scope_key).strip() for scope_key in cfg.officer_auth.chief_steward_contract_scopes if str(scope_key).strip())


def _normalize_contract_scope_input(cfg, value: object) -> str:  # noqa: ANN001
    normalized = normalize_scope_key(value)
    if not normalized or normalized not in cfg.officer_auth.chief_steward_contract_scopes:
        raise HTTPException(status_code=400, detail="invalid contract_scope")
    return normalized


def _normalize_email(value: object, *, field_name: str = "principal_email") -> str:
    text = str(value or "").strip().lower()
    if not text or "@" not in text:
        raise HTTPException(status_code=400, detail=f"{field_name} must be a valid email address")
    return text


async def _load_chief_steward_assignments(db: Db) -> list[ChiefStewardAssignmentRow]:
    rows = await db.fetchall(
        """
        SELECT id, principal_id, principal_email, principal_display_name,
               contract_scope, created_at_utc, updated_at_utc, assigned_by
        FROM chief_steward_assignments
        ORDER BY lower(principal_display_name), lower(principal_email), contract_scope, id
        """
    )
    return [
        ChiefStewardAssignmentRow(
            assignment_id=int(row[0]),
            principal_id=_normalize_optional_text(row[1]),
            principal_email=str(row[2] or ""),
            principal_display_name=_normalize_optional_text(row[3]),
            contract_scope=str(row[4] or ""),
            created_at_utc=str(row[5] or ""),
            updated_at_utc=str(row[6] or ""),
            assigned_by=str(row[7] or ""),
        )
        for row in rows
    ]


def _directory_user_row(result: object) -> DirectoryUserRow:
    if isinstance(result, dict):
        principal_id = str(result.get("id") or result.get("principal_id") or "").strip()
        display_name = _normalize_optional_text(result.get("display_name") or result.get("displayName"))
        email = _normalize_optional_text(result.get("email") or result.get("mail"))
        user_principal_name = _normalize_optional_text(
            result.get("user_principal_name") or result.get("userPrincipalName")
        )
        match_source = _normalize_optional_text(result.get("match_source")) or "directory"
    else:
        principal_id = str(getattr(result, "id", "") or getattr(result, "principal_id", "") or "").strip()
        display_name = _normalize_optional_text(
            getattr(result, "display_name", None) or getattr(result, "displayName", None)
        )
        email = _normalize_optional_text(getattr(result, "email", None) or getattr(result, "mail", None))
        user_principal_name = _normalize_optional_text(
            getattr(result, "user_principal_name", None) or getattr(result, "userPrincipalName", None)
        )
        match_source = _normalize_optional_text(getattr(result, "match_source", None)) or "directory"
    return DirectoryUserRow(
        principal_id=principal_id or None,
        display_name=display_name,
        email=email,
        user_principal_name=user_principal_name,
        match_source=match_source,
    )


def _directory_match_score(row: DirectoryUserRow, *, query: str) -> int:
    normalized_query = str(query or "").strip().lower()
    if not normalized_query:
        return 0
    values = [
        str(row.display_name or "").strip().lower(),
        str(row.email or "").strip().lower(),
        str(row.user_principal_name or "").strip().lower(),
    ]
    if normalized_query in {value for value in values if value}:
        return 300
    if any(value.startswith(normalized_query) for value in values if value):
        return 200
    if any(normalized_query in value for value in values if value):
        return 100
    tokens = [token for token in normalized_query.split() if token]
    haystack = " ".join(value for value in values if value)
    if tokens and all(token in haystack for token in tokens):
        return 50
    return 0


def _merge_directory_user_rows(
    primary_rows: list[DirectoryUserRow],
    secondary_rows: list[DirectoryUserRow],
    *,
    query: str,
    limit: int,
) -> list[DirectoryUserRow]:
    merged: dict[str, DirectoryUserRow] = {}

    def _merge_row(row: DirectoryUserRow) -> None:
        key = (
            str(row.principal_id or "").strip().lower()
            or str(row.email or "").strip().lower()
            or str(row.user_principal_name or "").strip().lower()
        )
        if not key:
            return
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            return
        merged[key] = DirectoryUserRow(
            principal_id=existing.principal_id or row.principal_id,
            display_name=existing.display_name or row.display_name,
            email=existing.email or row.email,
            user_principal_name=existing.user_principal_name or row.user_principal_name,
            match_source=existing.match_source if existing.match_source == "directory" else row.match_source,
        )

    for row in primary_rows:
        _merge_row(row)
    for row in secondary_rows:
        _merge_row(row)

    ranked = sorted(
        merged.values(),
        key=lambda row: (
            -_directory_match_score(row, query=query),
            0 if row.match_source == "directory" else 1,
            str(row.display_name or "").lower(),
            str(row.email or row.user_principal_name or "").lower(),
        ),
    )
    return ranked[:limit]


async def _local_directory_user_rows(db: Db, *, cfg, query: str, limit: int) -> list[DirectoryUserRow]:  # noqa: ANN001
    normalized_query = str(query or "").strip().lower()
    if len(normalized_query) < 2:
        return []

    merged: dict[str, DirectoryUserRow] = {}

    def _add_row(
        *,
        principal_id: str | None,
        display_name: str | None,
        email: str | None,
        user_principal_name: str | None,
    ) -> None:
        normalized_email = str(email or "").strip().lower() or None
        normalized_upn = str(user_principal_name or "").strip().lower() or None
        normalized_principal_id = str(principal_id or "").strip() or None
        key = normalized_principal_id or normalized_email or normalized_upn
        if not key:
            return
        row = DirectoryUserRow(
            principal_id=normalized_principal_id,
            display_name=_normalize_optional_text(display_name),
            email=normalized_email,
            user_principal_name=normalized_upn,
            match_source="local",
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            return
        merged[key] = DirectoryUserRow(
            principal_id=existing.principal_id or row.principal_id,
            display_name=existing.display_name or row.display_name,
            email=existing.email or row.email,
            user_principal_name=existing.user_principal_name or row.user_principal_name,
            match_source="local",
        )

    for value in getattr(cfg.officer_tracking, "roster", ()) or ():
        email = _normalize_optional_text(value)
        if email and "@" in email:
            _add_row(principal_id=None, display_name=None, email=email, user_principal_name=email)

    chief_rows = await db.fetchall(
        """
        SELECT principal_id, principal_email, principal_display_name
        FROM chief_steward_assignments
        ORDER BY updated_at_utc DESC, id DESC
        """
    )
    for row in chief_rows:
        _add_row(
            principal_id=_normalize_optional_text(row[0]),
            email=_normalize_optional_text(row[1]),
            display_name=_normalize_optional_text(row[2]),
            user_principal_name=_normalize_optional_text(row[1]),
        )

    external_rows = await db.fetchall(
        """
        SELECT auth_subject, email, display_name
        FROM external_steward_users
        ORDER BY updated_at_utc DESC, id DESC
        """
    )
    for row in external_rows:
        _add_row(
            principal_id=None,
            email=_normalize_optional_text(row[1]),
            display_name=_normalize_optional_text(row[2]),
            user_principal_name=_normalize_optional_text(row[1]),
        )

    ranked = [
        row
        for row in sorted(
            merged.values(),
            key=lambda row: (
                -_directory_match_score(row, query=normalized_query),
                str(row.display_name or "").lower(),
                str(row.email or row.user_principal_name or "").lower(),
            ),
        )
        if _directory_match_score(row, query=normalized_query) > 0
    ]
    return ranked[:limit]


async def _upsert_chief_steward_assignment(
    db: Db,
    *,
    cfg,  # noqa: ANN001
    principal_id: str | None,
    principal_email: str,
    principal_display_name: str | None,
    contract_scope: str,
    assigned_by: str,
) -> ChiefStewardAssignmentRow:
    now = utcnow()
    normalized_scope = _normalize_contract_scope_input(cfg, contract_scope)
    normalized_email = _normalize_email(principal_email)
    normalized_name = _normalize_optional_text(principal_display_name)
    normalized_principal_id = _normalize_optional_text(principal_id)
    async with aiosqlite.connect(db.db_path) as con:
        await con.execute("BEGIN IMMEDIATE")
        await con.execute(
            """
            INSERT INTO chief_steward_assignments(
              principal_id, principal_email, principal_display_name,
              contract_scope, created_at_utc, updated_at_utc, assigned_by
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(principal_email, contract_scope) DO UPDATE SET
              principal_id=excluded.principal_id,
              principal_display_name=excluded.principal_display_name,
              updated_at_utc=excluded.updated_at_utc,
              assigned_by=excluded.assigned_by
            """,
            (
                normalized_principal_id,
                normalized_email,
                normalized_name,
                normalized_scope,
                now,
                now,
                assigned_by,
            ),
        )
        cur = await con.execute(
            """
            SELECT id, principal_id, principal_email, principal_display_name,
                   contract_scope, created_at_utc, updated_at_utc, assigned_by
            FROM chief_steward_assignments
            WHERE lower(principal_email)=lower(?) AND contract_scope=?
            """,
            (normalized_email, normalized_scope),
        )
        row = await cur.fetchone()
        await con.commit()
    if not row:
        raise HTTPException(status_code=500, detail="failed to save chief steward assignment")
    return ChiefStewardAssignmentRow(
        assignment_id=int(row[0]),
        principal_id=_normalize_optional_text(row[1]),
        principal_email=str(row[2] or ""),
        principal_display_name=_normalize_optional_text(row[3]),
        contract_scope=str(row[4] or ""),
        created_at_utc=str(row[5] or ""),
        updated_at_utc=str(row[6] or ""),
        assigned_by=str(row[7] or ""),
    )


async def _delete_chief_steward_assignment(db: Db, assignment_id: int) -> ChiefStewardAssignmentRow:
    row = await db.fetchone(
        """
        SELECT id, principal_id, principal_email, principal_display_name,
               contract_scope, created_at_utc, updated_at_utc, assigned_by
        FROM chief_steward_assignments
        WHERE id=?
        """,
        (assignment_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="assignment_id not found")
    await db.exec("DELETE FROM chief_steward_assignments WHERE id=?", (assignment_id,))
    return ChiefStewardAssignmentRow(
        assignment_id=int(row[0]),
        principal_id=_normalize_optional_text(row[1]),
        principal_email=str(row[2] or ""),
        principal_display_name=_normalize_optional_text(row[3]),
        contract_scope=str(row[4] or ""),
        created_at_utc=str(row[5] or ""),
        updated_at_utc=str(row[6] or ""),
        assigned_by=str(row[7] or ""),
    )


def _render_officers_page(user: OfficerUserContext) -> str:
    viewer_payload = json.dumps(_build_viewer_model(user).model_dump(mode="json"), ensure_ascii=False)
    show_selection = _user_can_select_rows(user)
    show_actions = user.can_edit or user.can_delete or user.can_view_audit
    show_bulk_panel = user.can_bulk_edit or user.can_bulk_delete
    show_mutation_split = user.can_create or user.can_edit
    status_options = """
          <option value="open">Open</option>
          <option value="in_progress">In Progress</option>
          <option value="waiting">Waiting</option>
          <option value="open_at_state">Open at State</option>
          <option value="open_at_national">Open at National</option>
          <option value="closed">Closed</option>
    """
    selection_header = (
        '<th class="main select-col"><input id="selectAllRows" type="checkbox" aria-label="Select all rows" /></th>'
        if show_selection
        else ""
    )
    actions_header = '<th class="main actions-col">Actions</th>' if show_actions else ""
    chief_assignment_panel = (
        """
  <div class="panel" id="chiefAssignmentPanel">
    <h2>Chief Steward Contract Assignments</h2>
    <div class="summary">Admins can map chief stewards to contract scopes here without editing config.</div>
    <div class="summary">Search Microsoft Entra to autofill the steward, or type an email manually if needed.</div>
    <div class="summary" id="directorySearchStatus" style="margin-top:8px;"></div>
    <div class="grid" style="margin-top:12px;">
      <label>Directory Search
        <input id="directorySearchInput" placeholder="Search by name or email" />
      </label>
      <label>&nbsp;
        <button id="searchDirectoryBtn" type="button">Search Directory</button>
      </label>
    </div>
    <div class="table-wrap" style="margin-top:12px;">
      <table style="min-width: 900px;">
        <thead>
          <tr>
            <th class="main">Directory User</th>
            <th class="main">Email</th>
            <th class="main">Sign-In</th>
            <th class="main actions-col">Actions</th>
          </tr>
        </thead>
        <tbody id="directoryResultsBody">
          <tr><td colspan="4">Search the directory to pick a steward.</td></tr>
        </tbody>
      </table>
    </div>
    <div class="grid" style="margin-top:12px;">
      <input id="chiefAssignmentPrincipalId" type="hidden" />
      <label>Chief Steward Email
        <input id="chiefAssignmentEmail" placeholder="chief@example.org" />
      </label>
      <label>Display Name
        <input id="chiefAssignmentName" placeholder="Chief Steward Name" />
      </label>
      <label>Contract Scope
        <select id="chiefAssignmentScope"></select>
      </label>
    </div>
    <div class="actions">
      <button id="saveChiefAssignmentBtn" type="button">Assign Chief Steward</button>
    </div>
    <div class="table-wrap" style="margin-top:12px;">
      <table style="min-width: 900px;">
        <thead>
          <tr>
            <th class="main">Chief Steward</th>
            <th class="main">Email</th>
            <th class="main">Contract Scope</th>
            <th class="main">Updated</th>
            <th class="main actions-col">Actions</th>
          </tr>
        </thead>
        <tbody id="chiefAssignmentsBody">
          <tr><td colspan="5">No chief steward assignments loaded.</td></tr>
        </tbody>
      </table>
    </div>
  </div>
"""
        if user.can_manage_chief_assignments
        else ""
    )
    external_steward_panel = (
        """
  <div class="panel" id="externalStewardPanel">
    <h2>External Steward Access</h2>
    <div class="summary">Allowlist outside stewards here. They can only sign in through the steward portal after they are invited.</div>
    <div class="grid" style="margin-top:12px;">
      <label>External Steward Email
        <input id="externalStewardEmail" placeholder="outside@example.org" />
      </label>
      <label>Display Name
        <input id="externalStewardName" placeholder="Outside Steward Name" />
      </label>
    </div>
    <div class="actions">
      <button id="saveExternalStewardBtn" type="button">Allowlist External Steward</button>
    </div>
    <div class="table-wrap" style="margin-top:12px;">
      <table style="min-width: 1100px;">
        <thead>
          <tr>
            <th class="main">Steward</th>
            <th class="main">Email</th>
            <th class="main">Status</th>
            <th class="main">Provider Binding</th>
            <th class="main">Last Login</th>
            <th class="main">Assignments</th>
            <th class="main actions-col">Actions</th>
          </tr>
        </thead>
        <tbody id="externalStewardUsersBody">
          <tr><td colspan="7">No external stewards loaded.</td></tr>
        </tbody>
      </table>
    </div>
  </div>
"""
        if user.can_manage_chief_assignments
        else ""
    )
    case_external_assignment_panel = (
        """
  <div class="panel" id="caseExternalAssignmentPanel">
    <h2>External Steward Access For Selected Case</h2>
    <div id="caseExternalAssignmentHint" class="summary">Select a grievance row first, then assign outside steward access for that case.</div>
    <div class="grid" style="margin-top:12px;">
      <label>Allowlisted External Steward
        <select id="caseExternalStewardSelect"></select>
      </label>
    </div>
    <div class="actions">
      <button id="assignCaseExternalStewardBtn" type="button">Assign To Selected Case</button>
    </div>
    <div class="table-wrap" style="margin-top:12px;">
      <table style="min-width: 900px;">
        <thead>
          <tr>
            <th class="main">Steward</th>
            <th class="main">Email</th>
            <th class="main">Status</th>
            <th class="main">Assigned</th>
            <th class="main actions-col">Actions</th>
          </tr>
        </thead>
        <tbody id="caseExternalAssignmentsBody">
          <tr><td colspan="5">Select a case to load external steward assignments.</td></tr>
        </tbody>
      </table>
    </div>
  </div>
"""
        if user.can_manage_chief_assignments
        else ""
    )
    auth_panel = (
        """
  <div class="panel user-panel">
    <div>
      <div class="summary strong">Signed in</div>
      <div id="viewerLabel"></div>
    </div>
    <form method="post" action="/auth/logout">
      <button type="submit" class="secondary">Sign Out</button>
    </form>
  </div>
"""
        if user.auth_enabled
        else """
  <div class="panel user-panel">
    <div>
      <div class="summary strong">Local Read-Only Mode</div>
      <div>Officer edits stay disabled until Microsoft Entra officer auth is enabled in config.</div>
    </div>
  </div>
"""
    )
    bulk_panel = (
        """
  <div class="panel" id="bulkPanel">
    <h2>Bulk Update</h2>
    <div id="bulkSummary" class="summary selection-summary">0 cases selected.</div>
    <div class="summary">Only filled fields are applied. Leave a field blank to keep current values.</div>
    <div class="grid" style="margin-top:12px;">
      <label>Officer Status
        <select id="bulkOfficerStatus">
          <option value="">Keep current status</option>
{status_options}
        </select>
      </label>
      <label>Date Sent 1st Level Request
        <input id="bulkFirstLevelDate" type="date" />
      </label>
      <label>Date Sent 2nd Level Request
        <input id="bulkSecondLevelDate" type="date" />
      </label>
      <label>Date Sent 3rd Level Request
        <input id="bulkThirdLevelDate" type="date" />
      </label>
      <label>Date Sent 4th Level Request
        <input id="bulkFourthLevelDate" type="date" />
      </label>
      <label>Assign To Roster
        <select id="bulkAssigneeSelect"></select>
      </label>
      <label>Manual Assignee Override
        <input id="bulkAssigneeManual" placeholder="Type any name/email" />
      </label>
    </div>
    <div class="grid-wide" style="margin-top:12px;">
      <label>Notes
        <textarea id="bulkOfficerNotes" placeholder="Optional bulk note update"></textarea>
      </label>
    </div>
    <div class="actions">
      <button id="applyBulkBtn" type="button">Apply To Checked Rows</button>
      """
        + (
            '<button id="deleteBulkBtn" class="danger" type="button">Delete Checked Rows</button>'
            if user.can_bulk_delete
            else ""
        )
        + """
      <button id="clearBulkSelectionBtn" class="secondary" type="button">Clear Checked Rows</button>
    </div>
  </div>
"""
        if show_bulk_panel
        else ""
    )
    mutation_split = (
        f"""
  <div class="split" id="mutationSplit">
    {
      '''
    <div class="panel">
      <h2>Manual Paper Entry</h2>
      <div class="grid">
        <label>Grievance Number
          <input id="createGrievanceNumber" placeholder="Optional display number" />
        </label>
        <label>Grievance ID
          <input id="createGrievanceId" placeholder="Optional internal/reference id" />
        </label>
        <label>Contract / Scope
          <input id="createContract" placeholder="Required for chief-steward scoping" />
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
{status_options}
          </select>
        </label>
        <label>Date Sent 1st Level Request
          <input id="createFirstLevelDate" type="date" />
        </label>
        <label>Date Sent 2nd Level Request
          <input id="createSecondLevelDate" type="date" />
        </label>
        <label>Date Sent 3rd Level Request
          <input id="createThirdLevelDate" type="date" />
        </label>
        <label>Date Sent 4th Level Request
          <input id="createFourthLevelDate" type="date" />
        </label>
        <label>Assign To Roster
          <select id="createAssigneeSelect"></select>
        </label>
        <label>Manual Assignee Override
          <input id="createAssigneeManual" placeholder="Type any name/email" />
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
''' if user.can_create else '<div class="panel"><h2>Edit Access</h2><div class="summary">Chief stewards can edit in-scope rows but cannot create new cases.</div></div>'
    }

    {
      '''
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
        <label>Contract / Scope
          <input id="editContract" ''' + ('' if user.can_delete else 'disabled') + ''' />
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
{status_options}
          </select>
        </label>
        <label>Date Sent 1st Level Request
          <input id="editFirstLevelDate" type="date" />
        </label>
        <label>Date Sent 2nd Level Request
          <input id="editSecondLevelDate" type="date" />
        </label>
        <label>Date Sent 3rd Level Request
          <input id="editThirdLevelDate" type="date" />
        </label>
        <label>Date Sent 4th Level Request
          <input id="editFourthLevelDate" type="date" />
        </label>
        <label>Assign To Roster
          <select id="editAssigneeSelect"></select>
        </label>
        <label>Manual Assignee Override
          <input id="editAssigneeManual" placeholder="Type any name/email" />
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
''' if user.can_edit else ''
    }
  </div>
"""
        if show_mutation_split
        else ""
    )
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Officer Grievance Tracker</title>
  <style>
    :root {{
      --sheet-green: #95cf46;
      --sheet-blue: #4f81bd;
      --sheet-border: #cfd5dc;
      --sheet-bg: #f7f8fa;
      --sheet-closed: #efefef;
      --sheet-text: #1f2933;
    }}
    body {{
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      margin: 20px;
      color: var(--sheet-text);
      background: linear-gradient(180deg, #ffffff 0%, #f4f7f9 100%);
    }}
    h1, h2 {{ margin: 0 0 12px; }}
    .panel {{
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #dde4ea;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.05);
    }}
    .user-panel {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .strong {{
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(180px, 1fr));
      gap: 12px;
    }}
    .grid-wide {{
      display: grid;
      grid-template-columns: repeat(2, minmax(280px, 1fr));
      gap: 12px;
    }}
    label {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 13px;
      font-weight: 600;
    }}
    input, select, textarea {{
      border: 1px solid #bfc8d2;
      border-radius: 8px;
      padding: 9px 10px;
      font: inherit;
      background: white;
    }}
    textarea {{
      min-height: 86px;
      resize: vertical;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    button {{
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      background: #1f4d7a;
      color: white;
    }}
    button.danger {{
      background: #a11d2d;
    }}
    button.secondary {{
      background: #e6edf3;
      color: #203040;
    }}
    .summary {{
      margin: 8px 0 0;
      font-size: 13px;
      color: #4a5a68;
    }}
    .table-wrap {{
      overflow-x: auto;
      border-radius: 12px;
      border: 1px solid var(--sheet-border);
      background: white;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 1600px;
    }}
    th, td {{
      border: 1px solid var(--sheet-border);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th.select-col,
    td.select-col {{
      position: sticky;
      left: 0;
      z-index: 2;
      width: 44px;
      min-width: 44px;
      text-align: center;
      box-shadow: 6px 0 10px rgba(15, 23, 42, 0.04);
      background: white;
    }}
    th.actions-col,
    td.actions-col {{
      position: sticky;
      right: 0;
      z-index: 2;
      min-width: 180px;
      box-shadow: -6px 0 10px rgba(15, 23, 42, 0.06);
      background: white;
    }}
    th.main {{
      background: var(--sheet-green);
      color: white;
    }}
    th.request {{
      background: var(--sheet-blue);
      color: white;
    }}
    tr.closed-row td {{
      background: var(--sheet-closed);
    }}
    tr.closed-row td.select-col,
    tr.closed-row td.actions-col {{
      background: var(--sheet-closed);
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      background: #ebf2f7;
    }}
    .badge.closed {{ background: #d7dde3; }}
    .badge.waiting {{ background: #ffe8b5; }}
    .badge.in_progress {{ background: #d8f1da; }}
    .badge.open_at_state {{ background: #fde68a; }}
    .badge.open_at_national {{ background: #fca5a5; }}
    .muted {{ color: #64748b; font-size: 12px; }}
    .row-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .selection-summary {{
      font-weight: 600;
      color: #203040;
    }}
    .split {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
      align-items: start;
    }}
    pre {{
      background: #101923;
      color: #e2e8f0;
      border-radius: 12px;
      padding: 12px;
      overflow: auto;
      max-height: 320px;
      margin: 0;
    }}
    @media (max-width: 1200px) {{
      .grid, .grid-wide, .split {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <h1>Officer Grievance Tracker</h1>
  {auth_panel}
  {chief_assignment_panel}
  {external_steward_panel}
  {case_external_assignment_panel}

  <div class="panel">
    <h2>Filters</h2>
    <div class="grid">
      <label>Search
        <input id="filterSearch" placeholder="Grievance, contract, name, issue..." />
      </label>
      <label>Contract Scope
        <select id="filterContractScope">
          <option value="">All scopes</option>
        </select>
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
          <option value="open_at_state">Open at State</option>
          <option value="open_at_national">Open at National</option>
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

  {bulk_panel}
  {mutation_split}

  <div class="panel">
    <h2>Tracker Table</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            {selection_header}
            <th class="main">Grievance Number</th>
            <th class="main">Contract / Scope</th>
            <th class="main">Department</th>
            <th class="main">Name</th>
            <th class="main">Steward</th>
            <th class="main">Date of Occurrence</th>
            <th class="main">Issue</th>
            <th class="request">Date Sent 1st Level Request</th>
            <th class="request">Date Sent 2nd Level Request</th>
            <th class="request">Date Sent 3rd Level Request</th>
            <th class="request">Date Sent 4th Level Request</th>
            <th class="main">Assigned To</th>
            <th class="main">Officer Status</th>
            <th class="main">Workflow Status</th>
            <th class="main">Source</th>
            {actions_header}
          </tr>
        </thead>
        <tbody id="tableBody">
          <tr><td colspan="15">No cases loaded.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h2>Last Response</h2>
    <pre id="out">Ready.</pre>
  </div>

  <script>
    const VIEWER = {viewer_payload};
    const ENABLE_SELECTION = {str(show_selection).lower()};
    const SHOW_ACTIONS = {str(show_actions).lower()};
    const EMPTY_COLSPAN = 15 + (ENABLE_SELECTION ? 1 : 0) + (SHOW_ACTIONS ? 1 : 0);
    const out = document.getElementById('out');
    const tableBody = document.getElementById('tableBody');
    const tableSummary = document.getElementById('tableSummary');
    const filterAssignee = document.getElementById('filterAssignee');
    const filterContractScope = document.getElementById('filterContractScope');
    const createAssigneeSelect = document.getElementById('createAssigneeSelect');
    const editAssigneeSelect = document.getElementById('editAssigneeSelect');
    const bulkAssigneeSelect = document.getElementById('bulkAssigneeSelect');
    const bulkSummary = document.getElementById('bulkSummary');
    const selectAllRows = document.getElementById('selectAllRows');
    const chiefAssignmentsBody = document.getElementById('chiefAssignmentsBody');
    const chiefAssignmentScope = document.getElementById('chiefAssignmentScope');
    const directoryResultsBody = document.getElementById('directoryResultsBody');
    const directorySearchStatus = document.getElementById('directorySearchStatus');
    const externalStewardUsersBody = document.getElementById('externalStewardUsersBody');
    const caseExternalAssignmentsBody = document.getElementById('caseExternalAssignmentsBody');
    const caseExternalStewardSelect = document.getElementById('caseExternalStewardSelect');
    const caseExternalAssignmentHint = document.getElementById('caseExternalAssignmentHint');
    const currentRows = new Map();
    const selectedCaseIds = new Set();
    let currentRoster = [];
    let currentExternalStewardUsers = [];

    function esc(value) {{
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function show(data) {{
      out.textContent = JSON.stringify(data, null, 2);
    }}

    async function call(url, opts) {{
      const res = await fetch(url, opts || {{}});
      if (res.status === 401 && VIEWER.auth_enabled) {{
        window.location.href = '/auth/login?next=' + encodeURIComponent('/officers');
        throw new Error('login required');
      }}
      const text = await res.text();
      let data = text;
      try {{ data = JSON.parse(text); }} catch {{}}
      if (!res.ok) throw {{ status: res.status, data }};
      return data;
    }}

    function valueOf(id) {{
      const el = document.getElementById(id);
      return el ? el.value.trim() : '';
    }}

    function nullableValue(id) {{
      const value = valueOf(id);
      return value || null;
    }}

    function assigneeValue(selectId, manualId) {{
      const manual = valueOf(manualId);
      if (manual) return manual;
      const selected = valueOf(selectId);
      return selected || null;
    }}

    function labelForStatus(value) {{
      if (value === 'in_progress') return 'In Progress';
      if (value === 'open_at_state') return 'Open at State';
      if (value === 'open_at_national') return 'Open at National';
      if (value === 'closed') return 'Closed';
      if (value === 'waiting') return 'Waiting';
      return 'Open';
    }}

    function labelForSource(value) {{
      return value === 'paper_manual' ? 'Paper Manual' : 'Digital Intake';
    }}

    function labelForRole(value) {{
      if (value === 'admin') return 'Admin';
      if (value === 'chief_steward') return 'Chief Steward';
      if (value === 'officer') return 'Officer';
      return 'Read Only';
    }}

    function scopeLabel(value) {{
      return String(value || '').replace(/_/g, ' ').replace(/\\b\\w/g, (ch) => ch.toUpperCase());
    }}

    function populateAssigneeOptions(selectEl, options, selectedValue, emptyLabel) {{
      if (!selectEl) return;
      const seen = new Set();
      const values = [];
      for (const option of options) {{
        const text = String(option || '').trim();
        if (!text || seen.has(text.toLowerCase())) continue;
        seen.add(text.toLowerCase());
        values.push(text);
      }}
      selectEl.innerHTML = `<option value="">${{esc(emptyLabel)}}</option>` + values
        .sort((a, b) => a.localeCompare(b))
        .map((option) => `<option value="${{esc(option)}}">${{esc(option)}}</option>`)
        .join('');
      if (selectedValue) selectEl.value = selectedValue;
    }}

    function populateScopeOptions(values, selectedValue) {{
      const seen = new Set();
      const options = [];
      for (const value of values || []) {{
        const text = String(value || '').trim();
        if (!text || seen.has(text.toLowerCase())) continue;
        seen.add(text.toLowerCase());
        options.push(text);
      }}
      filterContractScope.innerHTML = '<option value="">All scopes</option>' + options
        .sort((a, b) => a.localeCompare(b))
        .map((value) => `<option value="${{esc(value)}}">${{esc(scopeLabel(value))}}</option>`)
        .join('');
      if (selectedValue) filterContractScope.value = selectedValue;
    }}

    function populateChiefAssignmentScopeOptions(values, selectedValue) {{
      if (!chiefAssignmentScope) return;
      const seen = new Set();
      const options = [];
      for (const value of values || []) {{
        const text = String(value || '').trim();
        if (!text || seen.has(text.toLowerCase())) continue;
        seen.add(text.toLowerCase());
        options.push(text);
      }}
      if (!options.length) {{
        chiefAssignmentScope.innerHTML = '<option value="">No scopes configured</option>';
        return;
      }}
      chiefAssignmentScope.innerHTML = '<option value="">Select scope</option>' + options
        .sort((a, b) => a.localeCompare(b))
        .map((value) => `<option value="${{esc(value)}}">${{esc(scopeLabel(value))}}</option>`)
        .join('');
      if (selectedValue) chiefAssignmentScope.value = selectedValue;
    }}

    function renderDirectoryResults(rows, search) {{
      if (!directoryResultsBody) return;
      if (!rows.length) {{
        const message = search ? `No directory matches found for "${{esc(search)}}".` : 'Search the directory to pick a steward.';
        directoryResultsBody.innerHTML = `<tr><td colspan="4">${{message}}</td></tr>`;
        return;
      }}
      directoryResultsBody.innerHTML = rows.map((row) => `
        <tr>
          <td>${{esc(row.display_name || 'Unnamed User')}}${{row.match_source === 'local' ? '<div class="muted">Known app user</div>' : ''}}</td>
          <td>${{esc(row.email || '')}}</td>
          <td>${{esc(row.user_principal_name || '')}}</td>
          <td class="actions-col">
            <div class="row-actions">
              <button
                type="button"
                data-directory-principal-id="${{esc(row.principal_id || '')}}"
                data-directory-email="${{esc(row.email || row.user_principal_name || '')}}"
                data-directory-name="${{esc(row.display_name || '')}}"
              >Use</button>
            </div>
          </td>
        </tr>
      `).join('');
    }}

    function applyDirectoryUserSelection(principalId, email, displayName) {{
      const principalIdInput = document.getElementById('chiefAssignmentPrincipalId');
      const emailInput = document.getElementById('chiefAssignmentEmail');
      const nameInput = document.getElementById('chiefAssignmentName');
      if (principalIdInput) principalIdInput.value = principalId || '';
      if (emailInput) emailInput.value = email || '';
      if (nameInput) nameInput.value = displayName || '';
    }}

    function refreshRosterOptions(rows, selectedFilter) {{
      const derived = [];
      for (const row of rows) {{
        if (row.officer_assignee) derived.push(row.officer_assignee);
      }}
      const options = [...currentRoster, ...derived];
      populateAssigneeOptions(filterAssignee, options, selectedFilter, 'All assignees');
      populateAssigneeOptions(createAssigneeSelect, options, valueOf('createAssigneeSelect'), 'Roster assignee');
      populateAssigneeOptions(editAssigneeSelect, options, valueOf('editAssigneeSelect'), 'Roster assignee');
      populateAssigneeOptions(bulkAssigneeSelect, options, valueOf('bulkAssigneeSelect'), 'Keep current assignee');
    }}

    function updateSelectionUi() {{
      if (!ENABLE_SELECTION) return;
      const visibleCaseIds = [...currentRows.keys()];
      const selectedVisible = visibleCaseIds.filter((caseId) => selectedCaseIds.has(caseId));
      if (bulkSummary) bulkSummary.textContent = `${{selectedVisible.length}} case(s) checked.`;
      if (!visibleCaseIds.length) {{
        if (selectAllRows) {{
          selectAllRows.checked = false;
          selectAllRows.indeterminate = false;
        }}
        return;
      }}
      if (selectAllRows) {{
        selectAllRows.checked = selectedVisible.length > 0 && selectedVisible.length === visibleCaseIds.length;
        selectAllRows.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleCaseIds.length;
      }}
    }}

    function clearBulkSelection() {{
      if (!ENABLE_SELECTION) return;
      selectedCaseIds.clear();
      updateSelectionUi();
      for (const checkbox of tableBody.querySelectorAll('input[data-select-case-id]')) {{
        checkbox.checked = false;
      }}
    }}

    function resetBulkForm() {{
      for (const id of [
        'bulkFirstLevelDate', 'bulkSecondLevelDate', 'bulkThirdLevelDate', 'bulkFourthLevelDate',
        'bulkAssigneeManual', 'bulkOfficerNotes'
      ]) {{
        const el = document.getElementById(id);
        if (el) el.value = '';
      }}
      if (document.getElementById('bulkOfficerStatus')) document.getElementById('bulkOfficerStatus').value = '';
      if (document.getElementById('bulkAssigneeSelect')) document.getElementById('bulkAssigneeSelect').value = '';
    }}

    function contractCell(row) {{
      const contract = row.contract || '';
      const scope = row.contract_scope || '';
      if (contract && scope) return `${{esc(contract)}}<div class="muted">${{esc(scopeLabel(scope))}}</div>`;
      if (contract) return esc(contract);
      if (scope) return `<span class="muted">${{esc(scopeLabel(scope))}}</span>`;
      return '';
    }}

    function renderRows(rows) {{
      currentRows.clear();
      for (const row of rows) currentRows.set(row.case_id, row);
      for (const caseId of [...selectedCaseIds]) {{
        if (!currentRows.has(caseId)) selectedCaseIds.delete(caseId);
      }}
      const selectedFilter = valueOf('filterAssignee');
      refreshRosterOptions(rows, selectedFilter);
      updateSelectionUi();

      tableSummary.textContent = `${{rows.length}} case(s) loaded.`;
      if (!rows.length) {{
        tableBody.innerHTML = `<tr><td colspan="${{EMPTY_COLSPAN}}">No matching grievances found.</td></tr>`;
        return;
      }}

      tableBody.innerHTML = rows.map((row) => `
        <tr class="${{row.officer_status === 'closed' ? 'closed-row' : ''}}">
          ${{ENABLE_SELECTION ? `
            <td class="select-col">
              <input type="checkbox" data-select-case-id="${{esc(row.case_id)}}" ${{selectedCaseIds.has(row.case_id) ? 'checked' : ''}} />
            </td>` : ''}}
          <td>${{esc(row.display_grievance)}}<div class="muted">${{esc(row.case_id)}}</div></td>
          <td>${{contractCell(row)}}</td>
          <td>${{esc(row.department || '')}}</td>
          <td>${{esc(row.member_name || '')}}</td>
          <td>${{esc(row.steward || '')}}</td>
          <td>${{esc(row.occurrence_date || '')}}</td>
          <td>${{esc(row.issue_summary || '')}}</td>
          <td>${{esc(row.first_level_request_sent_date || '')}}</td>
          <td>${{esc(row.second_level_request_sent_date || '')}}</td>
          <td>${{esc(row.third_level_request_sent_date || '')}}</td>
          <td>${{esc(row.fourth_level_request_sent_date || '')}}</td>
          <td>${{esc(row.officer_assignee || '')}}</td>
          <td><span class="badge ${{esc(row.officer_status)}}">${{esc(labelForStatus(row.officer_status))}}</span></td>
          <td>${{esc(row.workflow_status || '')}}</td>
          <td>${{esc(labelForSource(row.officer_source))}}</td>
          ${{SHOW_ACTIONS ? `
            <td class="actions-col">
              <div class="row-actions">
                ${{VIEWER.can_edit ? `<button type="button" data-action="edit" data-case-id="${{esc(row.case_id)}}">Edit</button>` : ''}}
                ${{VIEWER.can_view_audit ? `<button type="button" class="secondary" data-action="audit" data-case-id="${{esc(row.case_id)}}">Audit</button>` : ''}}
                ${{VIEWER.can_delete ? `<button type="button" class="danger" data-action="delete" data-case-id="${{esc(row.case_id)}}">Delete</button>` : ''}}
              </div>
            </td>` : ''}}
        </tr>
      `).join('');
      updateSelectionUi();
    }}

    function queryString() {{
      const params = new URLSearchParams();
      const search = valueOf('filterSearch');
      const contractScope = valueOf('filterContractScope');
      const assignee = valueOf('filterAssignee');
      const officerStatus = valueOf('filterStatus');
      const source = valueOf('filterSource');
      if (search) params.set('search', search);
      if (contractScope) params.set('contract_scope', contractScope);
      if (assignee) params.set('assignee', assignee);
      if (officerStatus) params.set('officer_status', officerStatus);
      if (source) params.set('source', source);
      const encoded = params.toString();
      return encoded ? `?${{encoded}}` : '';
    }}

    async function loadCases() {{
      try {{
        const data = await call(`/officers/cases${{queryString()}}`);
        currentRoster = Array.isArray(data.roster) ? data.roster : [];
        populateScopeOptions(data.available_contract_scopes || [], valueOf('filterContractScope'));
        if (data.viewer) {{
          document.getElementById('viewerLabel') && (document.getElementById('viewerLabel').textContent =
            `${{data.viewer.display_name || data.viewer.email || 'Unknown'}} · ${{labelForRole(data.viewer.role)}}`);
        }}
        renderRows(Array.isArray(data.rows) ? data.rows : []);
        show(data);
      }} catch (e) {{
        show(e);
      }}
    }}

    function resetCreateForm() {{
      for (const id of [
        'createGrievanceNumber', 'createGrievanceId', 'createContract', 'createMemberName', 'createMemberEmail',
        'createDepartment', 'createSteward', 'createOccurrenceDate', 'createFirstLevelDate',
        'createSecondLevelDate', 'createThirdLevelDate', 'createFourthLevelDate',
        'createAssigneeManual', 'createIssueSummary', 'createOfficerNotes'
      ]) {{
        const el = document.getElementById(id);
        if (el) el.value = '';
      }}
      if (document.getElementById('createOfficerStatus')) document.getElementById('createOfficerStatus').value = 'open';
      if (document.getElementById('createAssigneeSelect')) document.getElementById('createAssigneeSelect').value = '';
    }}

    async function createCase() {{
      const payload = {{
        grievance_number: nullableValue('createGrievanceNumber'),
        grievance_id: nullableValue('createGrievanceId'),
        contract: nullableValue('createContract'),
        member_name: valueOf('createMemberName'),
        member_email: nullableValue('createMemberEmail'),
        department: nullableValue('createDepartment'),
        steward: nullableValue('createSteward'),
        occurrence_date: nullableValue('createOccurrenceDate'),
        issue_summary: nullableValue('createIssueSummary'),
        first_level_request_sent_date: nullableValue('createFirstLevelDate'),
        second_level_request_sent_date: nullableValue('createSecondLevelDate'),
        third_level_request_sent_date: nullableValue('createThirdLevelDate'),
        fourth_level_request_sent_date: nullableValue('createFourthLevelDate'),
        officer_assignee: assigneeValue('createAssigneeSelect', 'createAssigneeManual'),
        officer_notes: nullableValue('createOfficerNotes'),
        officer_status: valueOf('createOfficerStatus') || 'open'
      }};
      try {{
        const data = await call('/officers/cases', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        resetCreateForm();
        show(data);
        await loadCases();
      }} catch (e) {{
        show(e);
      }}
    }}

    async function deleteCase(caseId) {{
      const row = currentRows.get(caseId);
      if (!row) {{
        return show({{ error: 'Case not found in current table view.' }});
      }}
      const confirmed = window.confirm(
        `Permanently delete ${{row.display_grievance}} and its related records?`
      );
      if (!confirmed) return;
      try {{
        const data = await call(`/officers/cases/${{encodeURIComponent(caseId)}}`, {{
          method: 'DELETE'
        }});
        selectedCaseIds.delete(caseId);
        if (valueOf('editCaseId') === caseId) clearEditSelection();
        show(data);
        await loadCases();
      }} catch (e) {{
        show(e);
      }}
    }}

    async function loadAudit(caseId) {{
      try {{
        const data = await call(`/officers/cases/${{encodeURIComponent(caseId)}}/events`);
        show(data);
      }} catch (e) {{
        show(e);
      }}
    }}

    async function applyBulkUpdate() {{
      const caseIds = [...selectedCaseIds].filter((caseId) => currentRows.has(caseId));
      if (!caseIds.length) {{
        return show({{ error: 'Check at least one case first.' }});
      }}
      const payload = {{ case_ids: caseIds }};
      const officerStatus = valueOf('bulkOfficerStatus');
      const firstLevelDate = nullableValue('bulkFirstLevelDate');
      const secondLevelDate = nullableValue('bulkSecondLevelDate');
      const thirdLevelDate = nullableValue('bulkThirdLevelDate');
      const fourthLevelDate = nullableValue('bulkFourthLevelDate');
      const assignee = assigneeValue('bulkAssigneeSelect', 'bulkAssigneeManual');
      const officerNotes = nullableValue('bulkOfficerNotes');

      if (officerStatus) payload.officer_status = officerStatus;
      if (firstLevelDate) payload.first_level_request_sent_date = firstLevelDate;
      if (secondLevelDate) payload.second_level_request_sent_date = secondLevelDate;
      if (thirdLevelDate) payload.third_level_request_sent_date = thirdLevelDate;
      if (fourthLevelDate) payload.fourth_level_request_sent_date = fourthLevelDate;
      if (assignee) payload.officer_assignee = assignee;
      if (officerNotes) payload.officer_notes = officerNotes;

      if (Object.keys(payload).length === 1) {{
        return show({{ error: 'Set at least one bulk field before applying.' }});
      }}

      try {{
        const data = await call('/officers/cases/bulk', {{
          method: 'PATCH',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        resetBulkForm();
        clearBulkSelection();
        show(data);
        await loadCases();
      }} catch (e) {{
        show(e);
      }}
    }}

    async function deleteSelectedCases() {{
      const caseIds = [...selectedCaseIds].filter((caseId) => currentRows.has(caseId));
      if (!caseIds.length) {{
        return show({{ error: 'Check at least one case first.' }});
      }}
      const confirmed = window.confirm(
        `Permanently delete ${{caseIds.length}} checked case(s) and their related records?`
      );
      if (!confirmed) return;
      try {{
        const data = await call('/officers/cases/bulk-delete', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ case_ids: caseIds }})
        }});
        if (caseIds.includes(valueOf('editCaseId'))) clearEditSelection();
        clearBulkSelection();
        show(data);
        await loadCases();
      }} catch (e) {{
        show(e);
      }}
    }}

    function clearEditSelection() {{
      for (const id of [
        'editCaseId', 'editCaseIdDisplay', 'editWorkflowStatus', 'editSource', 'editGrievanceNumber',
        'editMemberName', 'editMemberEmail', 'editContract', 'editDepartment', 'editSteward',
        'editOccurrenceDate', 'editFirstLevelDate', 'editSecondLevelDate', 'editThirdLevelDate',
        'editFourthLevelDate', 'editAssigneeManual', 'editIssueSummary', 'editOfficerNotes'
      ]) {{
        const el = document.getElementById(id);
        if (el) el.value = '';
      }}
      if (document.getElementById('editOfficerStatus')) document.getElementById('editOfficerStatus').value = 'open';
      if (document.getElementById('editAssigneeSelect')) document.getElementById('editAssigneeSelect').value = '';
      if (document.getElementById('editHint')) document.getElementById('editHint').textContent = 'Select a table row to edit.';
      if (document.getElementById('editMeta')) document.getElementById('editMeta').textContent = '';
      if (document.getElementById('caseExternalStewardSelect')) document.getElementById('caseExternalStewardSelect').value = '';
      renderCaseExternalAssignments(null);
    }}

    function startEdit(caseId) {{
      const row = currentRows.get(caseId);
      if (!row) return;
      document.getElementById('editCaseId').value = row.case_id || '';
      document.getElementById('editCaseIdDisplay').value = row.case_id || '';
      document.getElementById('editWorkflowStatus').value = row.workflow_status || '';
      document.getElementById('editSource').value = labelForSource(row.officer_source || '');
      document.getElementById('editGrievanceNumber').value = row.grievance_number || '';
      document.getElementById('editMemberName').value = row.member_name || '';
      document.getElementById('editMemberEmail').value = row.member_email || '';
      if (document.getElementById('editContract')) document.getElementById('editContract').value = row.contract || '';
      document.getElementById('editDepartment').value = row.department || '';
      document.getElementById('editSteward').value = row.steward || '';
      document.getElementById('editOccurrenceDate').value = row.occurrence_date || '';
      document.getElementById('editOfficerStatus').value = row.officer_status || 'open';
      document.getElementById('editFirstLevelDate').value = row.first_level_request_sent_date || '';
      document.getElementById('editSecondLevelDate').value = row.second_level_request_sent_date || '';
      document.getElementById('editThirdLevelDate').value = row.third_level_request_sent_date || '';
      document.getElementById('editFourthLevelDate').value = row.fourth_level_request_sent_date || '';
      document.getElementById('editAssigneeManual').value = '';
      document.getElementById('editAssigneeSelect').value = row.officer_assignee || '';
      document.getElementById('editIssueSummary').value = row.issue_summary || '';
      document.getElementById('editOfficerNotes').value = row.officer_notes || '';
      document.getElementById('editHint').textContent = `Editing ${{row.display_grievance}}`;
      document.getElementById('editMeta').textContent = row.officer_closed_at_utc
        ? `Closed ${{row.officer_closed_at_utc}}${{row.officer_closed_by ? ` by ${{row.officer_closed_by}}` : ''}}`
        : '';
      if (VIEWER.can_manage_chief_assignments) void loadCaseExternalStewardAssignments(caseId);
    }}

    async function saveEdit() {{
      const caseId = valueOf('editCaseId');
      if (!caseId) {{
        return show({{ error: 'Select a case first.' }});
      }}
      const payload = {{
        grievance_number: nullableValue('editGrievanceNumber'),
        member_name: nullableValue('editMemberName'),
        member_email: nullableValue('editMemberEmail'),
        department: nullableValue('editDepartment'),
        steward: nullableValue('editSteward'),
        occurrence_date: nullableValue('editOccurrenceDate'),
        issue_summary: nullableValue('editIssueSummary'),
        first_level_request_sent_date: nullableValue('editFirstLevelDate'),
        second_level_request_sent_date: nullableValue('editSecondLevelDate'),
        third_level_request_sent_date: nullableValue('editThirdLevelDate'),
        fourth_level_request_sent_date: nullableValue('editFourthLevelDate'),
        officer_assignee: assigneeValue('editAssigneeSelect', 'editAssigneeManual'),
        officer_notes: nullableValue('editOfficerNotes'),
        officer_status: valueOf('editOfficerStatus') || 'open'
      }};
      if (VIEWER.can_delete) {{
        payload.contract = nullableValue('editContract');
      }}
      try {{
        const data = await call(`/officers/cases/${{encodeURIComponent(caseId)}}`, {{
          method: 'PATCH',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        show(data);
        await loadCases();
        startEdit(caseId);
      }} catch (e) {{
        show(e);
      }}
    }}

    function renderChiefAssignments(rows) {{
      if (!chiefAssignmentsBody) return;
      if (!rows.length) {{
        chiefAssignmentsBody.innerHTML = '<tr><td colspan="5">No chief steward assignments saved yet.</td></tr>';
        return;
      }}
      chiefAssignmentsBody.innerHTML = rows.map((row) => `
        <tr>
          <td>${{esc(row.principal_display_name || 'Unspecified')}}</td>
          <td>${{esc(row.principal_email || '')}}</td>
          <td>${{esc(scopeLabel(row.contract_scope || ''))}}</td>
          <td>${{esc(row.updated_at_utc || '')}}<div class="muted">${{esc(row.assigned_by || '')}}</div></td>
          <td class="actions-col">
            <div class="row-actions">
              <button type="button" class="danger" data-chief-assignment-id="${{esc(row.assignment_id)}}">Remove</button>
            </div>
          </td>
        </tr>
      `).join('');
    }}

    function providerBindingLabel(row) {{
      if (!row || !row.auth_subject || !row.auth_issuer) return 'Not yet bound';
      return `${{row.auth_source || 'external_oidc'}} · ${{row.auth_issuer}} · ${{row.auth_subject}}`;
    }}

    function populateExternalStewardSelect(selectedUserId) {{
      if (!caseExternalStewardSelect) return;
      const activeUsers = (currentExternalStewardUsers || []).filter((row) => row.status === 'active');
      caseExternalStewardSelect.innerHTML = '<option value="">Select external steward</option>' + activeUsers
        .map((row) => `<option value="${{esc(row.user_id)}}">${{esc(row.display_name || row.email)}}${{row.email ? ` · ${{esc(row.email)}}` : ''}}</option>`)
        .join('');
      if (selectedUserId) caseExternalStewardSelect.value = String(selectedUserId);
    }}

    function renderExternalStewardUsers(rows) {{
      currentExternalStewardUsers = Array.isArray(rows) ? rows : [];
      populateExternalStewardSelect('');
      if (!externalStewardUsersBody) return;
      if (!currentExternalStewardUsers.length) {{
        externalStewardUsersBody.innerHTML = '<tr><td colspan="7">No external stewards allowlisted yet.</td></tr>';
        return;
      }}
      externalStewardUsersBody.innerHTML = currentExternalStewardUsers.map((row) => `
        <tr>
          <td>${{esc(row.display_name || 'Unspecified')}}</td>
          <td>${{esc(row.email || '')}}</td>
          <td>${{esc(row.status || '')}}</td>
          <td>${{esc(providerBindingLabel(row))}}</td>
          <td>${{esc(row.last_login_at_utc || '')}}</td>
          <td>${{esc(row.assignment_count || 0)}}</td>
          <td class="actions-col">
            <div class="row-actions">
              <button
                type="button"
                class="${{row.status === 'active' ? 'danger' : 'secondary'}}"
                data-external-steward-user-id="${{esc(row.user_id)}}"
                data-external-steward-next-status="${{esc(row.status === 'active' ? 'disabled' : 'active')}}"
              >${{esc(row.status === 'active' ? 'Disable' : 'Enable')}}</button>
            </div>
          </td>
        </tr>
      `).join('');
    }}

    function renderCaseExternalAssignments(data) {{
      if (!caseExternalAssignmentsBody) return;
      const rows = Array.isArray(data && data.rows) ? data.rows : [];
      if (caseExternalAssignmentHint) {{
        const grievance = data && data.display_grievance ? data.display_grievance : '';
        caseExternalAssignmentHint.textContent = grievance
          ? `External steward access for ${{grievance}}`
          : 'Select a grievance row first, then assign outside steward access for that case.';
      }}
      if (!rows.length) {{
        caseExternalAssignmentsBody.innerHTML = '<tr><td colspan="5">No external stewards assigned to this case.</td></tr>';
        return;
      }}
      caseExternalAssignmentsBody.innerHTML = rows.map((row) => `
        <tr>
          <td>${{esc(row.display_name || 'Unspecified')}}</td>
          <td>${{esc(row.email || '')}}</td>
          <td>${{esc(row.status || '')}}</td>
          <td>${{esc(row.updated_at_utc || row.created_at_utc || '')}}<div class="muted">${{esc(row.assigned_by || '')}}</div></td>
          <td class="actions-col">
            <div class="row-actions">
              <button
                type="button"
                class="danger"
                data-case-external-assignment-id="${{esc(row.assignment_id)}}"
                data-case-external-case-id="${{esc(row.case_id)}}"
              >Remove</button>
            </div>
          </td>
        </tr>
      `).join('');
    }}

    async function loadChiefAssignments() {{
      if (!VIEWER.can_manage_chief_assignments) return;
      try {{
        const data = await call('/officers/chief-assignments');
        populateChiefAssignmentScopeOptions(data.available_contract_scopes || [], valueOf('chiefAssignmentScope'));
        renderChiefAssignments(Array.isArray(data.rows) ? data.rows : []);
        show(data);
      }} catch (e) {{
        show(e);
      }}
    }}

    async function loadExternalStewards() {{
      if (!VIEWER.can_manage_chief_assignments) return;
      try {{
        const data = await call('/officers/external-stewards');
        renderExternalStewardUsers(Array.isArray(data.rows) ? data.rows : []);
        show(data);
      }} catch (e) {{
        show(e);
      }}
    }}

    async function saveExternalSteward() {{
      const payload = {{
        email: valueOf('externalStewardEmail'),
        display_name: nullableValue('externalStewardName'),
      }};
      if (!payload.email) {{
        return show({{ error: 'External steward email is required.' }});
      }}
      try {{
        const data = await call('/officers/external-stewards', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        document.getElementById('externalStewardEmail').value = '';
        document.getElementById('externalStewardName').value = '';
        show(data);
        await loadExternalStewards();
      }} catch (e) {{
        show(e);
      }}
    }}

    async function toggleExternalStewardStatus(userId, nextStatus) {{
      if (!userId || !nextStatus) return;
      try {{
        const data = await call(`/officers/external-stewards/${{encodeURIComponent(userId)}}`, {{
          method: 'PATCH',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ status: nextStatus }})
        }});
        show(data);
        await loadExternalStewards();
        const caseId = valueOf('editCaseId');
        if (caseId) await loadCaseExternalStewardAssignments(caseId);
      }} catch (e) {{
        show(e);
      }}
    }}

    async function loadCaseExternalStewardAssignments(caseId) {{
      if (!VIEWER.can_manage_chief_assignments || !caseId) {{
        renderCaseExternalAssignments(null);
        return;
      }}
      try {{
        const data = await call(`/officers/cases/${{encodeURIComponent(caseId)}}/external-stewards`);
        renderCaseExternalAssignments(data);
        show(data);
      }} catch (e) {{
        renderCaseExternalAssignments(null);
        show(e);
      }}
    }}

    async function assignExternalStewardToCase() {{
      const caseId = valueOf('editCaseId');
      const externalStewardUserId = valueOf('caseExternalStewardSelect');
      if (!caseId) {{
        return show({{ error: 'Select a case first.' }});
      }}
      if (!externalStewardUserId) {{
        return show({{ error: 'Select an external steward first.' }});
      }}
      try {{
        const data = await call(`/officers/cases/${{encodeURIComponent(caseId)}}/external-stewards`, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ external_steward_user_id: Number(externalStewardUserId) }})
        }});
        document.getElementById('caseExternalStewardSelect').value = '';
        show(data);
        await loadExternalStewards();
        await loadCaseExternalStewardAssignments(caseId);
      }} catch (e) {{
        show(e);
      }}
    }}

    async function removeExternalStewardFromCase(caseId, assignmentId) {{
      if (!caseId || !assignmentId) return;
      try {{
        const data = await call(`/officers/cases/${{encodeURIComponent(caseId)}}/external-stewards/${{encodeURIComponent(assignmentId)}}`, {{
          method: 'DELETE'
        }});
        show(data);
        await loadExternalStewards();
        await loadCaseExternalStewardAssignments(caseId);
      }} catch (e) {{
        show(e);
      }}
    }}

    async function searchDirectoryUsers() {{
      const search = valueOf('directorySearchInput');
      if (search.length < 2) {{
        if (directorySearchStatus) directorySearchStatus.textContent = '';
        renderDirectoryResults([], '');
        return show({{ error: 'Enter at least 2 characters to search the directory.' }});
      }}
      try {{
        const data = await call(`/officers/directory/users?search=${{encodeURIComponent(search)}}`);
        if (directorySearchStatus) directorySearchStatus.textContent = data.warning || '';
        renderDirectoryResults(Array.isArray(data.rows) ? data.rows : [], data.search || search);
        show(data);
      }} catch (e) {{
        if (directorySearchStatus) directorySearchStatus.textContent = '';
        renderDirectoryResults([], search);
        show(e);
      }}
    }}

    async function saveChiefAssignment() {{
      const payload = {{
        principal_id: nullableValue('chiefAssignmentPrincipalId'),
        principal_email: valueOf('chiefAssignmentEmail'),
        principal_display_name: nullableValue('chiefAssignmentName'),
        contract_scope: valueOf('chiefAssignmentScope'),
      }};
      if (!payload.principal_email || !payload.contract_scope) {{
        return show({{ error: 'Chief steward email and contract scope are required.' }});
      }}
      try {{
        const data = await call('/officers/chief-assignments', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }});
        document.getElementById('chiefAssignmentPrincipalId').value = '';
        document.getElementById('chiefAssignmentEmail').value = '';
        document.getElementById('chiefAssignmentName').value = '';
        show(data);
        await loadChiefAssignments();
      }} catch (e) {{
        show(e);
      }}
    }}

    async function deleteChiefAssignment(assignmentId) {{
      if (!assignmentId) return;
      const confirmed = window.confirm('Remove this chief steward contract assignment?');
      if (!confirmed) return;
      try {{
        const data = await call(`/officers/chief-assignments/${{encodeURIComponent(assignmentId)}}`, {{
          method: 'DELETE'
        }});
        show(data);
        await loadChiefAssignments();
      }} catch (e) {{
        show(e);
      }}
    }}

    tableBody.addEventListener('click', (event) => {{
      const button = event.target.closest('button[data-case-id][data-action]');
      if (!button) return;
      const caseId = button.dataset.caseId || '';
      if (button.dataset.action === 'delete') {{
        void deleteCase(caseId);
        return;
      }}
      if (button.dataset.action === 'audit') {{
        void loadAudit(caseId);
        return;
      }}
      startEdit(caseId);
    }});
    chiefAssignmentsBody && chiefAssignmentsBody.addEventListener('click', (event) => {{
      const button = event.target.closest('button[data-chief-assignment-id]');
      if (!button) return;
      void deleteChiefAssignment(button.dataset.chiefAssignmentId || '');
    }});
    externalStewardUsersBody && externalStewardUsersBody.addEventListener('click', (event) => {{
      const button = event.target.closest('button[data-external-steward-user-id][data-external-steward-next-status]');
      if (!button) return;
      void toggleExternalStewardStatus(
        button.dataset.externalStewardUserId || '',
        button.dataset.externalStewardNextStatus || '',
      );
    }});
    caseExternalAssignmentsBody && caseExternalAssignmentsBody.addEventListener('click', (event) => {{
      const button = event.target.closest('button[data-case-external-assignment-id][data-case-external-case-id]');
      if (!button) return;
      void removeExternalStewardFromCase(
        button.dataset.caseExternalCaseId || '',
        button.dataset.caseExternalAssignmentId || '',
      );
    }});
    directoryResultsBody && directoryResultsBody.addEventListener('click', (event) => {{
      const button = event.target.closest('button[data-directory-principal-id]');
      if (!button) return;
      applyDirectoryUserSelection(
        button.dataset.directoryPrincipalId || '',
        button.dataset.directoryEmail || '',
        button.dataset.directoryName || '',
      );
    }});
    tableBody.addEventListener('change', (event) => {{
      const checkbox = event.target.closest('input[data-select-case-id]');
      if (!checkbox) return;
      const caseId = checkbox.dataset.selectCaseId || '';
      if (!caseId) return;
      if (checkbox.checked) selectedCaseIds.add(caseId);
      else selectedCaseIds.delete(caseId);
      updateSelectionUi();
    }});

    document.getElementById('reloadBtn').addEventListener('click', () => {{ void loadCases(); }});
    document.getElementById('clearFiltersBtn').addEventListener('click', () => {{
      document.getElementById('filterSearch').value = '';
      document.getElementById('filterContractScope').value = '';
      document.getElementById('filterAssignee').value = '';
      document.getElementById('filterStatus').value = '';
      document.getElementById('filterSource').value = '';
      void loadCases();
    }});
    document.getElementById('applyBulkBtn') && document.getElementById('applyBulkBtn').addEventListener('click', () => {{ void applyBulkUpdate(); }});
    document.getElementById('deleteBulkBtn') && document.getElementById('deleteBulkBtn').addEventListener('click', () => {{ void deleteSelectedCases(); }});
    document.getElementById('clearBulkSelectionBtn') && document.getElementById('clearBulkSelectionBtn').addEventListener('click', clearBulkSelection);
    document.getElementById('createBtn') && document.getElementById('createBtn').addEventListener('click', () => {{ void createCase(); }});
    document.getElementById('saveEditBtn') && document.getElementById('saveEditBtn').addEventListener('click', () => {{ void saveEdit(); }});
    document.getElementById('clearEditBtn') && document.getElementById('clearEditBtn').addEventListener('click', clearEditSelection);
    document.getElementById('searchDirectoryBtn') && document.getElementById('searchDirectoryBtn').addEventListener('click', () => {{ void searchDirectoryUsers(); }});
    document.getElementById('saveExternalStewardBtn') && document.getElementById('saveExternalStewardBtn').addEventListener('click', () => {{ void saveExternalSteward(); }});
    document.getElementById('assignCaseExternalStewardBtn') && document.getElementById('assignCaseExternalStewardBtn').addEventListener('click', () => {{ void assignExternalStewardToCase(); }});
    document.getElementById('saveChiefAssignmentBtn')
      && document.getElementById('saveChiefAssignmentBtn').addEventListener('click', () => {{ void saveChiefAssignment(); }});
    document.getElementById('directorySearchInput') && document.getElementById('directorySearchInput').addEventListener('keydown', (event) => {{
      if (event.key !== 'Enter') return;
      event.preventDefault();
      void searchDirectoryUsers();
    }});
    document.getElementById('chiefAssignmentEmail') && document.getElementById('chiefAssignmentEmail').addEventListener('input', () => {{
      const principalIdInput = document.getElementById('chiefAssignmentPrincipalId');
      if (principalIdInput) principalIdInput.value = '';
    }});
    selectAllRows && selectAllRows.addEventListener('change', () => {{
      if (selectAllRows.checked) {{
        for (const caseId of currentRows.keys()) selectedCaseIds.add(caseId);
      }} else {{
        for (const caseId of currentRows.keys()) selectedCaseIds.delete(caseId);
      }}
      for (const checkbox of tableBody.querySelectorAll('input[data-select-case-id]')) {{
        checkbox.checked = selectAllRows.checked;
      }}
      updateSelectionUi();
    }});
    window.addEventListener('DOMContentLoaded', () => {{
      document.getElementById('viewerLabel') && (document.getElementById('viewerLabel').textContent =
        `${{VIEWER.display_name || VIEWER.email || 'Unknown'}} · ${{labelForRole(VIEWER.role)}}`);
      populateAssigneeOptions(filterAssignee, [], '', 'All assignees');
      populateAssigneeOptions(createAssigneeSelect, [], '', 'Roster assignee');
      populateAssigneeOptions(editAssigneeSelect, [], '', 'Roster assignee');
      populateAssigneeOptions(bulkAssigneeSelect, [], '', 'Keep current assignee');
      populateChiefAssignmentScopeOptions([], '');
      populateExternalStewardSelect('');
      updateSelectionUi();
      void loadCases();
      if (VIEWER.can_manage_chief_assignments) {{
        void loadChiefAssignments();
        void loadExternalStewards();
      }}
    }});
  </script>
</body>
</html>
"""


async def _event_rows_for_case(db: Db, case_id: str) -> list[OfficerCaseEventRow]:
    rows = await db.fetchall(
        "SELECT id, ts_utc, event_type, details_json FROM events WHERE case_id=? ORDER BY id DESC",
        (case_id,),
    )
    return [
        OfficerCaseEventRow(
            event_id=int(row[0]),
            ts_utc=str(row[1] or ""),
            event_type=str(row[2] or ""),
            details=parse_json_safely(row[3]),
        )
        for row in rows
    ]


@router.get("/officers", response_class=HTMLResponse)
async def officers_page(request: Request):
    gate = await require_officer_page_access(request, next_path="/officers")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(_render_officers_page(gate))


@router.get("/officers/cases", response_model=OfficerCaseListResponse)
async def officer_cases(
    request: Request,
    search: str | None = None,
    assignee: str | None = None,
    officer_status: str | None = None,
    source: str | None = None,
    contract_scope: str | None = None,
):
    user = await require_authenticated_officer(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    rows = await _load_officer_case_rows(db, cfg=cfg)
    visible_rows = [row for row in rows if user_can_view_case(user, contract_scope=row.contract_scope)]
    filtered = [
        row
        for row in visible_rows
        if _case_matches_filters(
            row,
            search=search,
            assignee=assignee,
            officer_status=officer_status,
            source=source,
            contract_scope=contract_scope,
        )
    ]
    available_contract_scopes = sorted(
        {
            row.contract_scope
            for row in visible_rows
            if row.contract_scope
        }
    )
    return OfficerCaseListResponse(
        rows=filtered,
        roster=list(cfg.officer_tracking.roster),
        viewer=_build_viewer_model(user),
        available_contract_scopes=available_contract_scopes,
        count=len(filtered),
    )


@router.get("/officers/chief-assignments", response_model=ChiefStewardAssignmentListResponse)
async def chief_steward_assignments(request: Request):
    await require_admin_user(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    return ChiefStewardAssignmentListResponse(
        rows=await _load_chief_steward_assignments(db),
        available_contract_scopes=_configured_contract_scopes(cfg),
    )


@router.get("/officers/directory/users", response_model=DirectoryUserSearchResponse)
async def officer_directory_users(request: Request, search: str = "", limit: int = 10):
    await require_admin_user(request)
    query = str(search or "").strip()
    if len(query) < 2:
        return DirectoryUserSearchResponse(search=query, count=0, rows=[], warning=None)

    capped_limit = max(1, min(int(limit or 10), 25))
    local_rows = await _local_directory_user_rows(request.app.state.db, cfg=request.app.state.cfg, query=query, limit=capped_limit)

    graph = getattr(request.app.state, "graph", None)
    warning: str | None = None
    graph_rows: list[DirectoryUserRow] = []
    if graph is None or not hasattr(graph, "search_directory_users"):
        warning = "Directory lookup is unavailable; showing locally known people only."
    else:
        try:
            matches = graph.search_directory_users(query, limit=capped_limit)
            graph_rows = [_directory_user_row(item) for item in matches]
        except RuntimeError as exc:
            detail = str(exc)
            if "Authorization_RequestDenied" in detail or "Insufficient privileges" in detail:
                warning = (
                    "Microsoft Graph directory lookup is unavailable until the app has User.Read.All "
                    "or Directory.Read.All application permission with admin consent. "
                    "Showing locally known people only."
                )
            else:
                warning = f"Microsoft Graph directory lookup failed. Showing locally known people only. {detail}"

    rows = _merge_directory_user_rows(graph_rows, local_rows, query=query, limit=capped_limit)
    return DirectoryUserSearchResponse(search=query, count=len(rows), rows=rows, warning=warning)


@router.post("/officers/chief-assignments", response_model=ChiefStewardAssignmentRow)
async def create_chief_steward_assignment(body: ChiefStewardAssignmentCreateRequest, request: Request):
    user = await require_admin_user(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    return await _upsert_chief_steward_assignment(
        db,
        cfg=cfg,
        principal_id=body.principal_id,
        principal_email=body.principal_email,
        principal_display_name=body.principal_display_name,
        contract_scope=body.contract_scope,
        assigned_by=actor_identity(user, fallback="admin"),
    )


@router.delete("/officers/chief-assignments/{assignment_id}", response_model=ChiefStewardAssignmentRow)
async def delete_chief_steward_assignment(assignment_id: int, request: Request):
    await require_admin_user(request)
    db: Db = request.app.state.db
    return await _delete_chief_steward_assignment(db, assignment_id)


@router.get("/officers/cases/{case_id}/events", response_model=OfficerCaseEventsResponse)
async def officer_case_events(case_id: str, request: Request):
    await require_admin_user(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    case_row = await _load_officer_case_row(db, cfg=cfg, case_id=case_id)
    events = await _event_rows_for_case(db, case_id)
    return OfficerCaseEventsResponse(
        case_id=case_row.case_id,
        display_grievance=case_row.display_grievance,
        event_count=len(events),
        events=events,
    )


@router.delete("/officers/cases/{case_id}", response_model=OfficerCaseDeleteResponse)
async def delete_officer_case(case_id: str, request: Request):
    await require_admin_user(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    case_row = await _load_officer_case_row(db, cfg=cfg, case_id=case_id)
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
    cfg = request.app.state.cfg
    auth_enabled = officer_auth_enabled(cfg)
    if not auth_enabled:
        raise HTTPException(status_code=423, detail="officer changes are disabled until officer auth is enabled")

    user = await require_authenticated_officer(request)
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
        current_case = _build_officer_case_row(cfg, rows_by_id[case_id])
        editor = await require_case_edit_access(request, contract_scope=current_case.contract_scope)
        updates, updated_by = _case_update_fields(body, current_row=rows_by_id[case_id], user=editor)
        if not updates:
            continue
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = tuple(updates.values()) + (case_id,)
        await db.exec(f"UPDATE cases SET {assignments} WHERE id=?", params)
        event_details = {
            "updated_by": updated_by,
            "changes": updates,
            **audit_actor_details(editor, case_contract_scope=current_case.contract_scope, bulk=True),
        }
        await db.add_event(case_id, None, "officer_case_updated", event_details)
        updated_case_ids.append(case_id)

    return OfficerCaseBulkUpdateResponse(
        selected_case_count=len(deduped_case_ids),
        updated_case_count=len(updated_case_ids),
        case_ids=updated_case_ids,
        changed_fields=requested_fields,
    )


@router.post("/officers/cases/bulk-delete", response_model=OfficerCaseBulkDeleteResponse)
async def bulk_delete_officer_cases(body: OfficerCaseBulkDeleteRequest, request: Request):
    await require_admin_user(request)
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
    user = await require_admin_user(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    case_id = new_case_id()
    grievance_id = normalize_grievance_id(body.grievance_id) or new_grievance_id()
    grievance_number = _normalize_optional_text(body.grievance_number)
    member_name = _normalize_member_name(body.member_name)
    member_email = _normalize_optional_text(body.member_email)
    contract = _normalize_optional_text(body.contract)
    department = _normalize_optional_text(body.department)
    steward = _normalize_optional_text(body.steward)
    occurrence_date = _normalize_date_text(body.occurrence_date)
    issue_summary = _normalize_optional_text(body.issue_summary)
    first_level_request_sent_date = _normalize_date_text(body.first_level_request_sent_date)
    second_level_request_sent_date = _normalize_date_text(body.second_level_request_sent_date)
    third_level_request_sent_date = _normalize_date_text(body.third_level_request_sent_date)
    fourth_level_request_sent_date = _normalize_date_text(body.fourth_level_request_sent_date)
    officer_assignee = _normalize_optional_text(body.officer_assignee)
    officer_notes = _normalize_optional_text(body.officer_notes)
    officer_status = _normalize_officer_status(body.officer_status, default="open")
    updated_by = actor_identity(user, fallback=officer_assignee or "officer-ui")
    request_id = f"officer-manual-{time.time_ns()}"
    closed_at_utc = utcnow() if officer_status == "closed" else None
    closed_by = updated_by if officer_status == "closed" else None
    contract_scope = resolve_contract_scope(cfg, contract)

    await db.exec(
        """INSERT INTO cases(
             id, grievance_id, created_at_utc, status, approval_status,
             grievance_number, member_name, member_email, intake_request_id, intake_payload_json,
             sharepoint_case_folder, sharepoint_case_web_url,
             officer_status, officer_assignee, officer_notes, officer_source,
             officer_closed_at_utc, officer_closed_by, tracking_contract,
             tracking_department, tracking_steward, tracking_occurrence_date,
             tracking_issue_summary, tracking_first_level_request_sent_date,
             tracking_second_level_request_sent_date, tracking_third_level_request_sent_date,
             tracking_fourth_level_request_sent_date
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            contract,
            department,
            steward,
            occurrence_date,
            issue_summary,
            first_level_request_sent_date,
            second_level_request_sent_date,
            third_level_request_sent_date,
            fourth_level_request_sent_date,
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
            "contract": contract,
            **audit_actor_details(user, case_contract_scope=contract_scope, bulk=False),
        },
    )
    return await _load_officer_case_row(db, cfg=cfg, case_id=case_id)


@router.patch("/officers/cases/{case_id}", response_model=OfficerCaseRow)
async def update_officer_case(case_id: str, body: OfficerCaseUpdateRequest, request: Request):
    cfg = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        raise HTTPException(status_code=423, detail="officer changes are disabled until officer auth is enabled")

    db: Db = request.app.state.db
    current_row = await db.fetchone(f"{_CASE_SELECT_SQL} WHERE id=?", (case_id,))
    if not current_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    current_case = _build_officer_case_row(cfg, current_row)
    editor = await require_case_edit_access(request, contract_scope=current_case.contract_scope)
    if editor.role != "admin" and "contract" in body.model_fields_set:
        raise HTTPException(status_code=403, detail="chief stewards cannot change contract scope")

    fields = body.model_fields_set
    if not fields:
        return current_case

    updates, updated_by = _case_update_fields(body, current_row=current_row, user=editor)
    if not updates:
        return current_case

    assignments = ", ".join(f"{column}=?" for column in updates)
    params = tuple(updates.values()) + (case_id,)
    await db.exec(f"UPDATE cases SET {assignments} WHERE id=?", params)
    next_contract = updates.get("tracking_contract", current_case.contract)
    await db.add_event(
        case_id,
        None,
        "officer_case_updated",
        {
            "updated_by": updated_by,
            "changes": updates,
            **audit_actor_details(editor, case_contract_scope=resolve_contract_scope(cfg, next_contract), bulk=False),
        },
    )
    return await _load_officer_case_row(db, cfg=cfg, case_id=case_id)
