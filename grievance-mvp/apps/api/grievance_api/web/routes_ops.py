from __future__ import annotations

import asyncio
import copy
import json
import time

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hmac_auth import compute_signature
from ..db.db import Db, utcnow
from .admin_common import parse_json_safely, require_local_access
from .officer_auth import require_admin_user, require_ops_page_access

router = APIRouter()

_OPS_CLEARED_STATUS = "ops_cleared"


async def _require_ops_api_access(request: Request) -> None:
    cfg = request.app.state.cfg
    if getattr(cfg, "officer_auth", None) and cfg.officer_auth.enabled:
        await require_admin_user(request)
        return
    require_local_access(request)


def _build_intake_headers(*, cfg, body: bytes) -> dict[str, str]:  # noqa: ANN001
    headers: dict[str, str] = {"Content-Type": "application/json"}

    shared_header_value = (cfg.intake_auth.shared_header_value or "").strip()
    if shared_header_value:
        headers[cfg.intake_auth.shared_header_name] = shared_header_value

    cf_id = (cfg.intake_auth.cloudflare_access_client_id or "").strip()
    cf_secret = (cfg.intake_auth.cloudflare_access_client_secret or "").strip()
    if cf_id and cf_secret:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = cf_secret

    hmac_secret = (cfg.hmac_shared_secret or "").strip()
    if hmac_secret and not hmac_secret.upper().startswith("REPLACE"):
        ts = str(int(time.time()))
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = compute_signature(hmac_secret, ts, body)

    return headers


def _normalize_ops_reason(value: object) -> str:
    reason = str(value or "").strip()
    return reason or "Cleared from ops queue"


def _normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def _is_case_signature_active(status: object) -> bool:
    return str(status or "").strip().lower().startswith("sent_for_signature")


def _is_standalone_signature_active(status: object) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized == "awaiting_signature" or normalized.startswith("sent_for_signature")


async def _load_active_signature_queue(*, db: Db, grievance_ref: str | None = None) -> dict[str, object]:
    ref = str(grievance_ref or "").strip()

    case_sql = """
        SELECT c.id, c.grievance_id, c.grievance_number, c.status, c.approval_status, c.member_name,
               d.id, d.doc_type, d.template_key, d.status, d.signer_order_json, d.docuseal_submission_id,
               d.docuseal_signing_link, d.created_at_utc, d.completed_at_utc,
               c.sharepoint_case_folder, c.sharepoint_case_web_url
        FROM documents d
        JOIN cases c ON c.id=d.case_id
        WHERE d.requires_signature=1
          AND COALESCE(d.docuseal_submission_id, '')<>''
          AND lower(COALESCE(d.status, '')) LIKE 'sent_for_signature%'
    """
    case_params: list[object] = []
    if ref:
        case_sql += " AND (c.grievance_id=? OR c.grievance_number=?)"
        case_params.extend([ref, ref])
    case_sql += " ORDER BY d.created_at_utc DESC, d.id DESC"

    standalone_sql = """
        SELECT s.id, s.form_key, s.form_title, s.signer_email, s.status,
               d.id, d.template_key, d.status, d.docuseal_submission_id,
               d.docuseal_signing_link, d.created_at_utc, d.completed_at_utc,
               s.sharepoint_folder_path, s.sharepoint_folder_web_url
        FROM standalone_documents d
        JOIN standalone_submissions s ON s.id=d.submission_id
        WHERE d.requires_signature=1
          AND COALESCE(d.docuseal_submission_id, '')<>''
          AND (
            lower(COALESCE(d.status, ''))='awaiting_signature'
            OR lower(COALESCE(d.status, '')) LIKE 'sent_for_signature%'
          )
    """
    standalone_params: list[object] = []
    if ref:
        standalone_sql += " AND (s.id=? OR s.request_id=? OR s.filing_label=?)"
        standalone_params.extend([ref, ref, ref])
    standalone_sql += " ORDER BY d.created_at_utc DESC, d.id DESC"

    case_rows = await db.fetchall(case_sql, tuple(case_params))
    standalone_rows = await db.fetchall(standalone_sql, tuple(standalone_params))

    case_documents = [
        {
            "case_id": row[0],
            "grievance_id": row[1],
            "grievance_number": row[2],
            "case_status": row[3],
            "approval_status": row[4],
            "member_name": row[5],
            "document_id": row[6],
            "doc_type": row[7],
            "template_key": row[8],
            "document_status": row[9],
            "signer_order": parse_json_safely(row[10]),
            "docuseal_submission_id": row[11],
            "docuseal_signing_link": row[12],
            "created_at_utc": row[13],
            "completed_at_utc": row[14],
            "sharepoint_case_folder": row[15],
            "sharepoint_case_web_url": row[16],
        }
        for row in case_rows
    ]
    standalone_documents = [
        {
            "submission_id": row[0],
            "form_key": row[1],
            "form_title": row[2],
            "signer_email": row[3],
            "submission_status": row[4],
            "document_id": row[5],
            "template_key": row[6],
            "document_status": row[7],
            "docuseal_submission_id": row[8],
            "docuseal_signing_link": row[9],
            "created_at_utc": row[10],
            "completed_at_utc": row[11],
            "sharepoint_folder_path": row[12],
            "sharepoint_folder_web_url": row[13],
        }
        for row in standalone_rows
    ]

    return {
        "grievance_ref": ref or None,
        "case_document_count": len(case_documents),
        "standalone_document_count": len(standalone_documents),
        "total_count": len(case_documents) + len(standalone_documents),
        "case_documents": case_documents,
        "standalone_documents": standalone_documents,
    }


async def _recalculate_case_status_after_ops_clear(*, db: Db, case_id: str) -> str:
    rows = await db.fetchall("SELECT status FROM documents WHERE case_id=? ORDER BY created_at_utc", (case_id,))
    statuses = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]

    if any(_is_case_signature_active(status) for status in statuses):
        return "awaiting_signatures"
    if any(status == "pending_grievance_number" for status in statuses):
        return "pending_grievance_number"

    non_cleared = [status for status in statuses if status != _OPS_CLEARED_STATUS]
    if not non_cleared:
        return _OPS_CLEARED_STATUS
    if any(status == "pending_approval" for status in non_cleared):
        return "pending_approval"
    if all(status in {"approved", "uploaded", "completed"} for status in non_cleared):
        return "approved"
    if all(status == "failed" for status in non_cleared):
        return "failed"
    if all(status in {"approved", "uploaded", "completed", "failed"} for status in non_cleared):
        if any(status in {"approved", "uploaded", "completed"} for status in non_cleared):
            return "approved"
        return "failed"
    return _OPS_CLEARED_STATUS


async def _recalculate_standalone_status_after_ops_clear(*, db: Db, submission_id: str) -> str:
    rows = await db.fetchall(
        "SELECT status FROM standalone_documents WHERE submission_id=? ORDER BY created_at_utc",
        (submission_id,),
    )
    statuses = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]

    if any(_is_standalone_signature_active(status) for status in statuses):
        return "awaiting_signature"

    non_cleared = [status for status in statuses if status != _OPS_CLEARED_STATUS]
    if not non_cleared:
        return _OPS_CLEARED_STATUS
    if all(status in {"completed", "approved", "uploaded"} for status in non_cleared):
        return "completed"
    if all(status == "failed" for status in non_cleared):
        return "failed"
    if all(status in {"completed", "approved", "uploaded", "failed"} for status in non_cleared):
        if any(status in {"completed", "approved", "uploaded"} for status in non_cleared):
            return "completed"
        return "failed"
    return _OPS_CLEARED_STATUS


async def _clear_case_document(*, db: Db, docuseal, document_id: str, reason: str) -> dict[str, object]:  # noqa: ANN001
    row = await db.fetchone(
        """SELECT d.id, d.case_id, d.doc_type, d.template_key, d.status, d.docuseal_submission_id,
                  c.grievance_id, c.grievance_number
           FROM documents d
           JOIN cases c ON c.id=d.case_id
           WHERE d.id=?""",
        (document_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="document_id not found")

    current_status = str(row[4] or "").strip()
    if not _is_case_signature_active(current_status):
        raise HTTPException(status_code=400, detail="document is not actively awaiting signatures")

    submission_id = str(row[5] or "").strip()
    delete_result: dict[str, object] | None = None
    if submission_id:
        try:
            delete_result = await asyncio.to_thread(docuseal.delete_submission, submission_id=submission_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DocuSeal submission delete failed: {exc}") from exc

    await db.exec(
        "UPDATE documents SET status=?, docuseal_signing_link=? WHERE id=?",
        (_OPS_CLEARED_STATUS, None, document_id),
    )
    if submission_id:
        await db.exec(
            """UPDATE document_stages
               SET status=?, failed_at_utc=COALESCE(failed_at_utc, ?)
               WHERE document_id=? AND docuseal_submission_id=? AND status LIKE 'sent_for_signature%'""",
            (_OPS_CLEARED_STATUS, utcnow(), document_id, submission_id),
        )

    case_id = str(row[1])
    updated_case_status = await _recalculate_case_status_after_ops_clear(db=db, case_id=case_id)
    await db.exec("UPDATE cases SET status=? WHERE id=?", (updated_case_status, case_id))
    await db.add_event(
        case_id,
        document_id,
        "ops_document_cleared",
        {
            "reason": _normalize_ops_reason(reason),
            "previous_status": current_status,
            "updated_status": _OPS_CLEARED_STATUS,
            "docuseal_submission_id": submission_id,
            "remote_delete": delete_result or {"ok": True, "already_missing": False, "status_code": None},
        },
    )

    return {
        "case_id": case_id,
        "document_id": str(row[0]),
        "doc_type": row[2],
        "template_key": row[3],
        "grievance_id": row[6],
        "grievance_number": row[7],
        "document_status": _OPS_CLEARED_STATUS,
        "case_status": updated_case_status,
        "reason": _normalize_ops_reason(reason),
        "docuseal_submission_id": submission_id or None,
        "remote_delete": delete_result,
    }


async def _clear_standalone_document(
    *,
    db: Db,
    docuseal,  # noqa: ANN001
    document_id: str,
    reason: str,
) -> dict[str, object]:
    row = await db.fetchone(
        """SELECT d.id, d.submission_id, d.form_key, d.template_key, d.status, d.docuseal_submission_id,
                  s.form_title, s.status
           FROM standalone_documents d
           JOIN standalone_submissions s ON s.id=d.submission_id
           WHERE d.id=?""",
        (document_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="standalone document_id not found")

    current_status = str(row[4] or "").strip()
    if not _is_standalone_signature_active(current_status):
        raise HTTPException(status_code=400, detail="standalone document is not actively awaiting signatures")

    submission_id = str(row[5] or "").strip()
    delete_result: dict[str, object] | None = None
    if submission_id:
        try:
            delete_result = await asyncio.to_thread(docuseal.delete_submission, submission_id=submission_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DocuSeal submission delete failed: {exc}") from exc

    await db.exec(
        "UPDATE standalone_documents SET status=?, docuseal_signing_link=? WHERE id=?",
        (_OPS_CLEARED_STATUS, None, document_id),
    )

    standalone_submission_id = str(row[1])
    updated_submission_status = await _recalculate_standalone_status_after_ops_clear(
        db=db,
        submission_id=standalone_submission_id,
    )
    await db.exec(
        "UPDATE standalone_submissions SET status=? WHERE id=?",
        (updated_submission_status, standalone_submission_id),
    )
    await db.add_standalone_event(
        standalone_submission_id,
        document_id,
        "ops_document_cleared",
        {
            "reason": _normalize_ops_reason(reason),
            "previous_status": current_status,
            "updated_status": _OPS_CLEARED_STATUS,
            "docuseal_submission_id": submission_id,
            "remote_delete": delete_result or {"ok": True, "already_missing": False, "status_code": None},
        },
    )

    return {
        "submission_id": standalone_submission_id,
        "document_id": str(row[0]),
        "form_key": row[2],
        "template_key": row[3],
        "form_title": row[6],
        "document_status": _OPS_CLEARED_STATUS,
        "submission_status": updated_submission_status,
        "reason": _normalize_ops_reason(reason),
        "docuseal_submission_id": submission_id or None,
        "remote_delete": delete_result,
    }


def _replace_email_in_signer_order(
    signer_order: object,
    *,
    current_email: str,
    new_email: str,
) -> tuple[list[str], bool]:
    parsed = signer_order if isinstance(signer_order, list) else []
    normalized_current = _normalize_email(current_email)
    normalized_new = str(new_email or "").strip()
    changed = False
    updated: list[str] = []
    for value in parsed:
        text = str(value or "").strip()
        if text and _normalize_email(text) == normalized_current:
            updated.append(normalized_new)
            changed = True
        else:
            updated.append(text)
    return updated, changed


def _docuseal_active_submitter_candidates(submitters: list[dict[str, object]]) -> list[dict[str, object]]:
    active: list[dict[str, object]] = []
    for item in submitters:
        status = str(item.get("status") or "").strip().lower()
        if status in {"completed", "declined"}:
            continue
        if item.get("completed_at") or item.get("declined_at"):
            continue
        active.append(item)
    return active or submitters


def _resolve_docuseal_submitter(
    *,
    submitters: list[dict[str, object]],
    current_email: str,
) -> dict[str, object]:
    normalized_current = _normalize_email(current_email)
    active_submitters = _docuseal_active_submitter_candidates(submitters)

    if normalized_current:
        matches = [item for item in active_submitters if _normalize_email(item.get("email")) == normalized_current]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="multiple active DocuSeal submitters match current_email")
        fallback_matches = [item for item in submitters if _normalize_email(item.get("email")) == normalized_current]
        if len(fallback_matches) == 1:
            return fallback_matches[0]
        raise HTTPException(status_code=404, detail="current_email not found on DocuSeal submission")

    if len(active_submitters) == 1:
        return active_submitters[0]
    raise HTTPException(status_code=400, detail="current_email required when multiple active submitters exist")


async def _update_case_document_signer_email(
    *,
    db: Db,
    docuseal,  # noqa: ANN001
    document_id: str,
    current_email: str,
    new_email: str,
    resend_email: bool,
) -> dict[str, object]:
    normalized_new = str(new_email or "").strip()
    if "@" not in normalized_new:
        raise HTTPException(status_code=400, detail="new_email must be a valid email address")

    row = await db.fetchone(
        """SELECT d.id, d.case_id, d.doc_type, d.template_key, d.status, d.signer_order_json,
                  d.docuseal_submission_id, d.docuseal_signing_link, c.intake_payload_json
           FROM documents d
           JOIN cases c ON c.id=d.case_id
           WHERE d.id=?""",
        (document_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="document_id not found")

    current_status = str(row[4] or "").strip()
    if not _is_case_signature_active(current_status):
        raise HTTPException(status_code=400, detail="document is not actively awaiting signatures")

    submission_id = str(row[6] or "").strip()
    if not submission_id:
        raise HTTPException(status_code=400, detail="document has no DocuSeal submission_id")

    submitters = await asyncio.to_thread(docuseal.list_submitters, submission_id=submission_id)
    submitter = _resolve_docuseal_submitter(submitters=submitters, current_email=current_email)
    previous_email = str(submitter.get("email") or "").strip()
    if _normalize_email(previous_email) == _normalize_email(normalized_new):
        raise HTTPException(status_code=400, detail="new_email matches the current signer email")

    updated_submitter = await asyncio.to_thread(
        docuseal.update_submitter,
        submitter_id=submitter.get("id"),
        email=normalized_new,
        send_email=bool(resend_email),
    )

    parsed_signer_order = parse_json_safely(row[5])
    signer_order, changed = _replace_email_in_signer_order(
        parsed_signer_order,
        current_email=previous_email,
        new_email=normalized_new,
    )
    if not changed:
        remote_submitters = await asyncio.to_thread(docuseal.list_submitters, submission_id=submission_id)
        signer_order = [str(item.get("email") or "").strip() for item in remote_submitters if str(item.get("email") or "").strip()]

    signing_links = await asyncio.to_thread(docuseal.fetch_signing_links_by_email, submission_id=submission_id)
    updated_signing_link = signing_links.get(_normalize_email(normalized_new)) or row[7]

    await db.exec(
        "UPDATE documents SET signer_order_json=?, docuseal_signing_link=? WHERE id=?",
        (json.dumps(signer_order, ensure_ascii=False), updated_signing_link, document_id),
    )

    intake_payload = parse_json_safely(row[8])
    if isinstance(intake_payload, dict):
        documents_payload = intake_payload.get("documents")
        if isinstance(documents_payload, list):
            for item in documents_payload:
                if not isinstance(item, dict):
                    continue
                if not _document_matches_target(
                    doc_type=item.get("doc_type"),
                    template_key=item.get("template_key"),
                    target=str(row[2] or row[3] or ""),
                ):
                    continue
                signers = item.get("signers")
                if isinstance(signers, list):
                    updated_signers, payload_changed = _replace_email_in_signer_order(
                        signers,
                        current_email=previous_email,
                        new_email=normalized_new,
                    )
                    if payload_changed:
                        item["signers"] = updated_signers
            await db.exec(
                "UPDATE cases SET intake_payload_json=? WHERE id=?",
                (json.dumps(intake_payload, ensure_ascii=False), row[1]),
            )

    await db.add_event(
        str(row[1]),
        document_id,
        "ops_signer_email_updated",
        {
            "previous_email": previous_email,
            "new_email": normalized_new,
            "docuseal_submission_id": submission_id,
            "resend_email": bool(resend_email),
            "submitter_id": submitter.get("id"),
        },
    )

    return {
        "case_id": str(row[1]),
        "document_id": str(row[0]),
        "doc_type": row[2],
        "template_key": row[3],
        "document_status": current_status,
        "previous_email": previous_email,
        "new_email": normalized_new,
        "resend_email": bool(resend_email),
        "docuseal_submission_id": submission_id,
        "submitter_id": submitter.get("id"),
        "submitter_response": updated_submitter,
        "signer_order": signer_order,
        "docuseal_signing_link": updated_signing_link,
    }


async def _update_standalone_document_signer_email(
    *,
    db: Db,
    docuseal,  # noqa: ANN001
    document_id: str,
    current_email: str,
    new_email: str,
    resend_email: bool,
) -> dict[str, object]:
    normalized_new = str(new_email or "").strip()
    if "@" not in normalized_new:
        raise HTTPException(status_code=400, detail="new_email must be a valid email address")

    row = await db.fetchone(
        """SELECT d.id, d.submission_id, d.form_key, d.template_key, d.status, d.signer_order_json,
                  d.docuseal_submission_id, d.docuseal_signing_link, s.signer_email, s.status
           FROM standalone_documents d
           JOIN standalone_submissions s ON s.id=d.submission_id
           WHERE d.id=?""",
        (document_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="standalone document_id not found")

    current_status = str(row[4] or "").strip()
    if not _is_standalone_signature_active(current_status):
        raise HTTPException(status_code=400, detail="standalone document is not actively awaiting signatures")

    submission_id = str(row[6] or "").strip()
    if not submission_id:
        raise HTTPException(status_code=400, detail="standalone document has no DocuSeal submission_id")

    submitters = await asyncio.to_thread(docuseal.list_submitters, submission_id=submission_id)
    submitter = _resolve_docuseal_submitter(submitters=submitters, current_email=current_email or str(row[8] or ""))
    previous_email = str(submitter.get("email") or "").strip()
    if _normalize_email(previous_email) == _normalize_email(normalized_new):
        raise HTTPException(status_code=400, detail="new_email matches the current signer email")

    updated_submitter = await asyncio.to_thread(
        docuseal.update_submitter,
        submitter_id=submitter.get("id"),
        email=normalized_new,
        send_email=bool(resend_email),
    )

    parsed_signer_order = parse_json_safely(row[5])
    signer_order, changed = _replace_email_in_signer_order(
        parsed_signer_order,
        current_email=previous_email,
        new_email=normalized_new,
    )
    if not changed:
        signer_order = [normalized_new]

    signing_links = await asyncio.to_thread(docuseal.fetch_signing_links_by_email, submission_id=submission_id)
    updated_signing_link = signing_links.get(_normalize_email(normalized_new)) or row[7]

    await db.exec(
        """UPDATE standalone_documents
           SET signer_order_json=?, docuseal_signing_link=?
           WHERE id=?""",
        (json.dumps(signer_order, ensure_ascii=False), updated_signing_link, document_id),
    )
    await db.exec(
        "UPDATE standalone_submissions SET signer_email=? WHERE id=?",
        (normalized_new, row[1]),
    )
    await db.add_standalone_event(
        str(row[1]),
        document_id,
        "ops_signer_email_updated",
        {
            "previous_email": previous_email,
            "new_email": normalized_new,
            "docuseal_submission_id": submission_id,
            "resend_email": bool(resend_email),
            "submitter_id": submitter.get("id"),
        },
    )

    return {
        "submission_id": str(row[1]),
        "document_id": str(row[0]),
        "form_key": row[2],
        "template_key": row[3],
        "document_status": current_status,
        "submission_status": row[9],
        "previous_email": previous_email,
        "new_email": normalized_new,
        "resend_email": bool(resend_email),
        "docuseal_submission_id": submission_id,
        "submitter_id": submitter.get("id"),
        "submitter_response": updated_submitter,
        "signer_order": signer_order,
        "docuseal_signing_link": updated_signing_link,
    }


async def _load_case_trace(*, db: Db, case_id: str) -> dict[str, object]:
    case_row = await db.fetchone(
        """SELECT id, grievance_id, status, approval_status, grievance_number,
                  member_name, member_email, intake_request_id, created_at_utc
           FROM cases WHERE id=?""",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    docs_rows = await db.fetchall(
        """SELECT id, doc_type, template_key, status, requires_signature, signer_order_json,
                  docuseal_submission_id, docuseal_signing_link, created_at_utc, completed_at_utc
           FROM documents
           WHERE case_id=?
           ORDER BY created_at_utc""",
        (case_id,),
    )
    events_rows = await db.fetchall(
        """SELECT ts_utc, event_type, document_id, details_json
           FROM events
           WHERE case_id=?
           ORDER BY ts_utc DESC
           LIMIT 200""",
        (case_id,),
    )
    email_rows = await db.fetchall(
        """SELECT recipient_email, template_key, status, resend_count, last_sent_at_utc,
                  document_scope_id, graph_message_id
           FROM outbound_emails
           WHERE case_id=?
           ORDER BY updated_at_utc DESC
           LIMIT 200""",
        (case_id,),
    )

    return {
        "case": {
            "case_id": case_row[0],
            "grievance_id": case_row[1],
            "status": case_row[2],
            "approval_status": case_row[3],
            "grievance_number": case_row[4],
            "member_name": case_row[5],
            "member_email": case_row[6],
            "intake_request_id": case_row[7],
            "created_at_utc": case_row[8],
        },
        "documents": [
            {
                "document_id": row[0],
                "doc_type": row[1],
                "template_key": row[2],
                "status": row[3],
                "requires_signature": bool(row[4]),
                "signer_order": parse_json_safely(row[5]),
                "docuseal_submission_id": row[6],
                "docuseal_signing_link": row[7],
                "created_at_utc": row[8],
                "completed_at_utc": row[9],
            }
            for row in docs_rows
        ],
        "events": [
            {
                "ts_utc": row[0],
                "event_type": row[1],
                "document_id": row[2],
                "details": parse_json_safely(row[3]),
            }
            for row in events_rows
        ],
        "outbound_emails": [
            {
                "recipient_email": row[0],
                "template_key": row[1],
                "status": row[2],
                "resend_count": row[3],
                "last_sent_at_utc": row[4],
                "document_scope_id": row[5],
                "graph_message_id": row[6],
            }
            for row in email_rows
        ],
    }


async def _load_standalone_trace(*, db: Db, submission_id: str) -> dict[str, object]:
    submission_row = await db.fetchone(
        """SELECT id, request_id, form_key, form_title, signer_email, status, created_at_utc,
                  filing_year, filing_sequence, filing_label, sharepoint_folder_path, sharepoint_folder_web_url
           FROM standalone_submissions WHERE id=?""",
        (submission_id,),
    )
    if not submission_row:
        raise HTTPException(status_code=404, detail="submission_id not found")

    docs_rows = await db.fetchall(
        """SELECT id, template_key, status, requires_signature, signer_order_json,
                  docuseal_submission_id, docuseal_signing_link,
                  sharepoint_generated_url, sharepoint_signed_url, sharepoint_audit_url,
                  created_at_utc, completed_at_utc
           FROM standalone_documents
           WHERE submission_id=?
           ORDER BY created_at_utc""",
        (submission_id,),
    )
    events_rows = await db.fetchall(
        """SELECT ts_utc, event_type, document_id, details_json
           FROM standalone_events
           WHERE submission_id=?
           ORDER BY ts_utc DESC
           LIMIT 200""",
        (submission_id,),
    )
    email_rows = await db.fetchall(
        """SELECT recipient_email, template_key, status, resend_count, last_sent_at_utc,
                  document_scope_id, graph_message_id
           FROM standalone_outbound_emails
           WHERE submission_id=?
           ORDER BY updated_at_utc DESC
           LIMIT 200""",
        (submission_id,),
    )

    return {
        "submission": {
            "submission_id": submission_row[0],
            "request_id": submission_row[1],
            "form_key": submission_row[2],
            "form_title": submission_row[3],
            "signer_email": submission_row[4],
            "status": submission_row[5],
            "created_at_utc": submission_row[6],
            "filing_year": submission_row[7],
            "filing_sequence": submission_row[8],
            "filing_label": submission_row[9],
            "sharepoint_folder_path": submission_row[10],
            "sharepoint_folder_web_url": submission_row[11],
        },
        "documents": [
            {
                "document_id": row[0],
                "template_key": row[1],
                "status": row[2],
                "requires_signature": bool(row[3]),
                "signer_order": parse_json_safely(row[4]),
                "docuseal_submission_id": row[5],
                "docuseal_signing_link": row[6],
                "sharepoint_generated_url": row[7],
                "sharepoint_signed_url": row[8],
                "sharepoint_audit_url": row[9],
                "created_at_utc": row[10],
                "completed_at_utc": row[11],
            }
            for row in docs_rows
        ],
        "events": [
            {
                "ts_utc": row[0],
                "event_type": row[1],
                "document_id": row[2],
                "details": parse_json_safely(row[3]),
            }
            for row in events_rows
        ],
        "outbound_emails": [
            {
                "recipient_email": row[0],
                "template_key": row[1],
                "status": row[2],
                "resend_count": row[3],
                "last_sent_at_utc": row[4],
                "document_scope_id": row[5],
                "graph_message_id": row[6],
            }
            for row in email_rows
        ],
    }


def _new_resubmit_request_id(base_request_id: str) -> str:
    return f"{base_request_id}-resubmit-{time.time_ns()}"


def _normalize_lookup_token(value: object) -> str:
    return str(value or "").strip().lower()


def _document_matches_target(*, doc_type: object, template_key: object, target: str) -> bool:
    target_norm = _normalize_lookup_token(target)
    if not target_norm:
        return False
    return target_norm in {
        _normalize_lookup_token(doc_type),
        _normalize_lookup_token(template_key),
    }


def _filter_payload_documents_for_target(
    *,
    payload: dict[str, object],
    target_doc_type: str,
    fallback_doc: dict[str, object],
) -> dict[str, object]:
    cloned = copy.deepcopy(payload)
    raw_documents = cloned.get("documents")
    filtered_documents: list[dict[str, object]] = []

    if isinstance(raw_documents, list):
        for item in raw_documents:
            if not isinstance(item, dict):
                continue
            if _document_matches_target(
                doc_type=item.get("doc_type"),
                template_key=item.get("template_key"),
                target=target_doc_type,
            ):
                filtered_documents.append(item)

    if not filtered_documents:
        fallback_signers = fallback_doc.get("signer_order")
        filtered_documents = [
            {
                "doc_type": fallback_doc.get("doc_type") or target_doc_type,
                "template_key": fallback_doc.get("template_key") or None,
                "requires_signature": bool(fallback_doc.get("requires_signature")),
                "signers": fallback_signers if isinstance(fallback_signers, list) else None,
            }
        ]

    cloned["documents"] = filtered_documents
    cloned.pop("document_command", None)
    return cloned


async def _load_grievance_doc_catalog(*, db: Db, grievance_ref: str) -> dict[str, object]:
    ref = str(grievance_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="grievance_ref required")

    rows = await db.fetchall(
        """SELECT c.id, c.grievance_id, c.grievance_number, c.status, c.approval_status,
                  c.member_name, c.member_email, c.intake_request_id, c.created_at_utc,
                  d.id, d.doc_type, d.template_key, d.status, d.requires_signature,
                  d.signer_order_json, d.docuseal_submission_id, d.docuseal_signing_link,
                  d.sharepoint_generated_url, d.sharepoint_signed_url, d.sharepoint_audit_url,
                  d.created_at_utc, d.completed_at_utc
           FROM cases c
           LEFT JOIN documents d ON d.case_id=c.id
           WHERE c.grievance_id=? OR c.grievance_number=?
           ORDER BY c.created_at_utc DESC, d.created_at_utc DESC, d.id DESC""",
        (ref, ref),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="no cases found for grievance_ref")

    cases: list[dict[str, object]] = []
    cases_by_id: dict[str, dict[str, object]] = {}
    doc_type_summary: dict[str, dict[str, object]] = {}

    for row in rows:
        case_id = row[0]
        case_entry = cases_by_id.get(case_id)
        if case_entry is None:
            match_fields: list[str] = []
            if str(row[1] or "").strip() == ref:
                match_fields.append("grievance_id")
            if str(row[2] or "").strip() == ref:
                match_fields.append("grievance_number")
            case_entry = {
                "case_id": case_id,
                "grievance_id": row[1],
                "grievance_number": row[2],
                "status": row[3],
                "approval_status": row[4],
                "member_name": row[5],
                "member_email": row[6],
                "intake_request_id": row[7],
                "created_at_utc": row[8],
                "match_fields": match_fields,
                "documents": [],
            }
            cases_by_id[case_id] = case_entry
            cases.append(case_entry)

        document_id = row[9]
        if not document_id:
            continue

        signer_order = parse_json_safely(row[14])
        doc_entry = {
            "document_id": document_id,
            "doc_type": row[10],
            "template_key": row[11],
            "status": row[12],
            "requires_signature": bool(row[13]),
            "signer_order": signer_order,
            "docuseal_submission_id": row[15],
            "docuseal_signing_link": row[16],
            "sharepoint_generated_url": row[17],
            "sharepoint_signed_url": row[18],
            "sharepoint_audit_url": row[19],
            "created_at_utc": row[20],
            "completed_at_utc": row[21],
        }
        case_entry["documents"].append(doc_entry)

        summary_key = str(row[10] or row[11] or "").strip()
        if not summary_key:
            continue
        summary = doc_type_summary.get(summary_key)
        if summary is None:
            summary = {
                "doc_type": row[10],
                "template_keys": [],
                "document_count": 0,
                "case_count": 0,
                "latest_case_id": case_id,
                "latest_document_id": document_id,
                "latest_document_status": row[12],
                "latest_document_created_at_utc": row[20],
                "_case_ids": set(),
                "_template_keys": set(),
            }
            doc_type_summary[summary_key] = summary

        summary["document_count"] += 1
        case_ids = summary["_case_ids"]
        if case_id not in case_ids:
            case_ids.add(case_id)
            summary["case_count"] += 1
        template_key = str(row[11] or "").strip()
        if template_key:
            template_keys = summary["_template_keys"]
            if template_key not in template_keys:
                template_keys.add(template_key)
                summary["template_keys"].append(template_key)

    doc_types = sorted(
        (
            {
                key: value
                for key, value in summary.items()
                if not key.startswith("_")
            }
            for summary in doc_type_summary.values()
        ),
        key=lambda item: (
            str(item.get("latest_document_created_at_utc") or ""),
            str(item.get("doc_type") or ""),
        ),
        reverse=True,
    )

    return {
        "grievance_ref": ref,
        "case_count": len(cases),
        "doc_type_count": len(doc_types),
        "doc_types": doc_types,
        "cases": cases,
    }


async def _post_internal_json(*, cfg, url: str, payload: dict[str, object]) -> object:  # noqa: ANN001
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _build_intake_headers(cfg=cfg, body=body)
    resp = await asyncio.to_thread(
        requests.post,
        url,
        data=body,
        headers=headers,
        timeout=180,
    )

    parsed_response = parse_json_safely(resp.text)
    if not (200 <= resp.status_code < 300):
        raise HTTPException(status_code=resp.status_code, detail=parsed_response)
    return parsed_response


@router.get("/ops", response_class=HTMLResponse)
async def ops_page(request: Request):
    gate = await require_ops_page_access(request, next_path="/ops")
    if isinstance(gate, RedirectResponse):
        return gate
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Grievance Ops</title>
  <style>
    :root {
      --ops-border: #d7e0ea;
      --ops-text: #203040;
      --ops-muted: #5d7080;
      --ops-bg: #eef4f8;
      --ops-card: rgba(255, 255, 255, 0.95);
      --ops-accent: #1f4d7a;
      --ops-accent-soft: #edf4fa;
    }
    body {
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      margin: 0;
      padding: 24px;
      color: var(--ops-text);
      background:
        radial-gradient(circle at top left, rgba(31, 77, 122, 0.10), transparent 18%),
        radial-gradient(circle at top right, rgba(149, 207, 70, 0.10), transparent 16%),
        linear-gradient(180deg, #f8fbfd 0%, var(--ops-bg) 100%);
    }
    .page-shell {
      max-width: 1680px;
      margin: 0 auto;
    }
    .top-nav {
      position: sticky;
      top: 14px;
      z-index: 20;
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
      margin-bottom: 16px;
      padding: 12px 16px;
      border: 1px solid var(--ops-border);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.95);
      box-shadow: 0 16px 36px rgba(15, 23, 42, 0.08);
      backdrop-filter: blur(8px);
    }
    .top-nav-title {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-right: auto;
    }
    .eyebrow {
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--ops-muted);
    }
    .top-nav h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1;
      letter-spacing: -0.03em;
    }
    .nav-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .nav-link {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 10px 14px;
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
      color: #17334f;
      border: 1px solid var(--ops-border);
      background: rgba(255, 255, 255, 0.92);
    }
    .nav-link-primary {
      color: white;
      background: linear-gradient(180deg, #173a5c 0%, var(--ops-accent) 100%);
      border-color: #173a5c;
      box-shadow: 0 12px 24px rgba(31, 77, 122, 0.22);
    }
    .panel {
      background: var(--ops-card);
      border: 1px solid var(--ops-border);
      border-radius: 18px;
      padding: 18px;
      margin-bottom: 18px;
      box-shadow: 0 18px 36px rgba(15, 23, 42, 0.05);
    }
    .section {
      scroll-margin-top: 92px;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 14px;
    }
    .section-head h2 {
      margin: 0;
      font-size: 24px;
    }
    .section-head .summary {
      margin: 6px 0 0;
      font-weight: 500;
    }
    .tool-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(320px, 1fr));
      gap: 16px;
    }
    .tool-card {
      border: 1px solid var(--ops-border);
      border-radius: 16px;
      padding: 16px;
      background: linear-gradient(180deg, #ffffff 0%, #f6f9fc 100%);
    }
    .tool-card h3 {
      margin: 0 0 8px;
      font-size: 18px;
    }
    .tool-card .summary {
      margin: 0 0 12px;
      font-weight: 500;
    }
    .row { margin-bottom: 12px; }
    input, select {
      width: min(100%, 460px);
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #bdc9d6;
      background: white;
      font: inherit;
    }
    button {
      padding: 10px 14px;
      margin-right: 8px;
      border-radius: 10px;
      border: 0;
      background: var(--ops-accent);
      color: white;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    table { border-collapse: collapse; width: 100%; margin: 12px 0 0; }
    th, td { border: 1px solid #ccd5de; padding: 10px 8px; vertical-align: top; text-align: left; }
    th { background: #eef4f8; }
    .summary { margin: 8px 0 12px; font-weight: 600; color: #334e62; }
    .muted { color: var(--ops-muted); font-size: 12px; }
    .link-group a { margin-right: 8px; }
    pre {
      margin: 0;
      background: #101923;
      color: #dde7f1;
      padding: 14px;
      border-radius: 14px;
      overflow: auto;
      max-height: 48vh;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    @media (max-width: 1100px) {
      .tool-grid {
        grid-template-columns: 1fr;
      }
      .top-nav {
        position: static;
      }
      .top-nav-title {
        margin-right: 0;
        width: 100%;
      }
    }
    @media (max-width: 760px) {
      body {
        padding: 12px;
      }
      .panel {
        padding: 14px;
        border-radius: 14px;
      }
      .nav-links {
        width: 100%;
      }
      .nav-link {
        flex: 1 1 calc(50% - 8px);
        justify-content: center;
      }
      button {
        width: 100%;
        margin-right: 0;
        margin-bottom: 8px;
      }
      input, select {
        width: 100%;
      }
      #activeQueueTable thead {
        display: none;
      }
      #activeQueueTable,
      #activeQueueTable tbody,
      #activeQueueTable tr,
      #activeQueueTable td {
        display: block;
        width: 100%;
      }
      #activeQueueTable tr {
        margin-bottom: 12px;
        border: 1px solid #ccd5de;
        border-radius: 14px;
        overflow: hidden;
        background: white;
      }
      #activeQueueTable td {
        border: 0;
        border-top: 1px solid #e6edf3;
      }
      #activeQueueTable td:first-child {
        border-top: 0;
      }
      #activeQueueTable td::before {
        content: attr(data-label);
        display: block;
        margin-bottom: 4px;
        font-size: 11px;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--ops-muted);
      }
    }
  </style>
</head>
<body>
  <div class="page-shell">
  <div class="top-nav" id="opsNav">
    <div class="top-nav-title">
      <div class="eyebrow">Operations Console</div>
      <h1>Grievance Ops</h1>
    </div>
    <div class="nav-links">
      <a class="nav-link nav-link-primary" href="/officers">Officer Tracker</a>
      <a class="nav-link" href="#activeQueueSection">Active Queue</a>
      <a class="nav-link" href="#caseToolsSection">Case Tools</a>
      <a class="nav-link" href="#grievanceToolsSection">Grievance Docs</a>
      <a class="nav-link" href="#standaloneToolsSection">Standalone</a>
      <a class="nav-link" href="#responsePanel">Last Response</a>
    </div>
  </div>
  <div class="panel section" id="activeQueueSection">
    <div class="section-head">
      <div>
        <div class="eyebrow">Signature Queue</div>
        <h2>Active Signature Requests</h2>
        <div class="summary">Load the current signature backlog, inspect who is blocked, and jump straight into trace or cleanup actions.</div>
      </div>
    </div>
    <div class="row">
      <button onclick="loadActiveSignatures()">Load Active Signature Queue</button>
    </div>
    <div id="activeSummary" class="summary">Active signature requests not loaded yet.</div>
    <div class="muted">Optional filter: fill Grievance ID / Number below before loading the queue.</div>
    <table id="activeQueueTable">
      <thead>
        <tr>
          <th>Kind</th>
          <th>Document</th>
          <th>Case / Submission</th>
          <th>Grievance / Filing</th>
          <th>Signer(s)</th>
          <th>Status</th>
          <th>Created</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="activeQueueBody">
        <tr><td colspan="8">No queue loaded.</td></tr>
      </tbody>
    </table>
  </div>
  <div class="tool-grid">
  <div class="panel section tool-card" id="caseToolsSection">
    <div class="eyebrow">Case Operations</div>
    <h3>Case Tools</h3>
    <div class="summary">Trace a case, resend outstanding signature links, or resubmit the full case package.</div>
    <div class="row">
      <input id="caseId" placeholder="Case ID (example: C2026...)" />
    </div>
    <div class="row">
      <button onclick="loadTrace()">Load Trace</button>
      <button onclick="resendSignature()">Resend Signature Emails</button>
      <button onclick="resubmitCase()">Resubmit Case</button>
    </div>
  </div>
  <div class="panel section tool-card" id="grievanceToolsSection">
    <div class="eyebrow">Document Operations</div>
    <h3>Grievance Docs</h3>
    <div class="summary">Inspect the document catalog for a grievance and resubmit the latest matching document type.</div>
    <div class="row">
      <input id="grievanceRef" placeholder="Grievance ID or Grievance Number (example: 2026015)" />
    </div>
    <div class="row">
      <select id="docTypeSelect">
        <option value="">Select doc type after loading grievance docs</option>
      </select>
    </div>
    <div class="row">
      <button onclick="loadGrievanceDocs()">Load Grievance Docs</button>
      <button onclick="resubmitDocType()">Resubmit Latest Matching Doc Type</button>
    </div>
  </div>
  <div class="panel section tool-card" id="standaloneToolsSection">
    <div class="eyebrow">Standalone Operations</div>
    <h3>Standalone Forms</h3>
    <div class="summary">Open the trace for a standalone submission or resubmit it without leaving the ops console.</div>
    <div class="row">
      <input id="submissionId" placeholder="Standalone Submission ID (example: S2026...)" />
    </div>
    <div class="row">
      <button onclick="loadStandaloneTrace()">Load Standalone Trace</button>
      <button onclick="resubmitStandalone()">Resubmit Standalone</button>
    </div>
  </div>
  </div>
  <div class="panel section" id="responsePanel">
    <div class="section-head">
      <div>
        <div class="eyebrow">Debug Output</div>
        <h2>Last Response</h2>
      </div>
    </div>
    <pre id="out">Ready.</pre>
  </div>
  </div>
  <script>
    const out = document.getElementById('out');
    const caseInput = document.getElementById('caseId');
    const grievanceRefInput = document.getElementById('grievanceRef');
    const docTypeSelect = document.getElementById('docTypeSelect');
    const submissionInput = document.getElementById('submissionId');
    const activeSummary = document.getElementById('activeSummary');
    const activeQueueBody = document.getElementById('activeQueueBody');
    async function call(url, opts) {
      const res = await fetch(url, opts || {});
      const text = await res.text();
      let data = text;
      try { data = JSON.parse(text); } catch {}
      if (!res.ok) throw { status: res.status, data };
      return data;
    }
    function show(data) { out.textContent = JSON.stringify(data, null, 2); }
    function cid() { return caseInput.value.trim(); }
    function gid() { return grievanceRefInput.value.trim(); }
    function sid() { return submissionInput.value.trim(); }
    function esc(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
    function signerText(value) {
      if (Array.isArray(value)) return value.filter(Boolean).join(', ');
      return String(value || '');
    }
    function traceCase(caseId) {
      caseInput.value = caseId || '';
      return loadTrace();
    }
    function traceStandalone(submissionId) {
      submissionInput.value = submissionId || '';
      return loadStandaloneTrace();
    }
    function renderActiveQueue(data) {
      const caseDocs = Array.isArray(data && data.case_documents) ? data.case_documents : [];
      const standaloneDocs = Array.isArray(data && data.standalone_documents) ? data.standalone_documents : [];
      const total = Number(data && data.total_count || 0);
      const filterLabel = data && data.grievance_ref ? ` for ${data.grievance_ref}` : '';
      activeSummary.textContent = `Active signature requests${filterLabel}: ${total} (${caseDocs.length} grievance docs, ${standaloneDocs.length} standalone forms)`;
      const rows = [];
      for (const item of caseDocs) {
        rows.push(`
          <tr>
            <td data-label="Kind">Grievance</td>
            <td data-label="Document">${esc(item.doc_type || item.template_key || '')}</td>
            <td data-label="Case / Submission">${esc(item.case_id || '')}<br><span class="muted">${esc(item.document_id || '')}</span></td>
            <td data-label="Grievance / Filing">${esc(item.grievance_id || item.grievance_number || '')}</td>
            <td data-label="Signer(s)">${esc(signerText(item.signer_order))}</td>
            <td data-label="Status">${esc(item.document_status || '')}<br><span class="muted">${esc(item.case_status || '')}</span></td>
            <td data-label="Created">${esc(item.created_at_utc || '')}</td>
            <td class="link-group" data-label="Actions">
              <button type="button" data-action="trace-case" data-case-id="${esc(item.case_id || '')}">Trace</button>
              ${item.docuseal_signing_link ? `<a href="${esc(item.docuseal_signing_link)}" target="_blank" rel="noreferrer">Open Link</a>` : ''}
              <button
                type="button"
                data-action="fix-case-email"
                data-document-id="${esc(item.document_id || '')}"
                data-signer-summary="${esc(signerText(item.signer_order))}"
              >Fix Email</button>
              <button type="button" data-action="clear-case-document" data-document-id="${esc(item.document_id || '')}">Clear</button>
            </td>
          </tr>
        `);
      }
      for (const item of standaloneDocs) {
        rows.push(`
          <tr>
            <td data-label="Kind">Standalone</td>
            <td data-label="Document">${esc(item.form_key || item.template_key || '')}</td>
            <td data-label="Case / Submission">${esc(item.submission_id || '')}<br><span class="muted">${esc(item.document_id || '')}</span></td>
            <td data-label="Grievance / Filing">${esc(item.sharepoint_folder_path || item.form_title || '')}</td>
            <td data-label="Signer(s)">${esc(item.signer_email || '')}</td>
            <td data-label="Status">${esc(item.document_status || '')}<br><span class="muted">${esc(item.submission_status || '')}</span></td>
            <td data-label="Created">${esc(item.created_at_utc || '')}</td>
            <td class="link-group" data-label="Actions">
              <button type="button" data-action="trace-standalone" data-submission-id="${esc(item.submission_id || '')}">Trace</button>
              ${item.docuseal_signing_link ? `<a href="${esc(item.docuseal_signing_link)}" target="_blank" rel="noreferrer">Open Link</a>` : ''}
              <button
                type="button"
                data-action="fix-standalone-email"
                data-document-id="${esc(item.document_id || '')}"
                data-current-email="${esc(item.signer_email || '')}"
              >Fix Email</button>
              <button type="button" data-action="clear-standalone-document" data-document-id="${esc(item.document_id || '')}">Clear</button>
            </td>
          </tr>
        `);
      }
      activeQueueBody.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="8">No active signature requests found.</td></tr>';
    }
    activeQueueBody.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const action = button.dataset.action || '';
      if (action === 'trace-case') {
        void traceCase(button.dataset.caseId || '');
        return;
      }
      if (action === 'trace-standalone') {
        void traceStandalone(button.dataset.submissionId || '');
        return;
      }
      if (action === 'fix-case-email') {
        void fixCaseEmail(button.dataset.documentId || '', button.dataset.signerSummary || '');
        return;
      }
      if (action === 'fix-standalone-email') {
        void fixStandaloneEmail(button.dataset.documentId || '', button.dataset.currentEmail || '');
        return;
      }
      if (action === 'clear-case-document') {
        void clearCaseDocument(button.dataset.documentId || '');
        return;
      }
      if (action === 'clear-standalone-document') {
        void clearStandaloneDocument(button.dataset.documentId || '');
      }
    });
    function updateDocTypeSelect(data) {
      const current = docTypeSelect.value;
      docTypeSelect.innerHTML = '<option value="">Select doc type after loading grievance docs</option>';
      const docTypes = Array.isArray(data && data.doc_types) ? data.doc_types : [];
      for (const item of docTypes) {
        const docType = (item && item.doc_type) || '';
        if (!docType) continue;
        const option = document.createElement('option');
        option.value = docType;
        const count = Number(item.document_count || 0);
        const latestCase = item.latest_case_id || '';
        option.textContent = `${docType} (${count}) ${latestCase ? '- latest ' + latestCase : ''}`;
        docTypeSelect.appendChild(option);
      }
      if (current) docTypeSelect.value = current;
    }
    async function loadActiveSignatures() {
      const grievanceRef = gid();
      const suffix = grievanceRef ? `?grievance_ref=${encodeURIComponent(grievanceRef)}` : '';
      try {
        const data = await call(`/ops/active-signatures${suffix}`);
        renderActiveQueue(data);
        show(data);
      } catch (e) { show(e); }
    }
    async function loadTrace() {
      const id = cid();
      if (!id) return show({ error: 'case_id required' });
      try { show(await call(`/ops/cases/${encodeURIComponent(id)}/trace`)); }
      catch (e) { show(e); }
    }
    async function resendSignature() {
      const id = cid();
      if (!id) return show({ error: 'case_id required' });
      try { show(await call(`/ops/cases/${encodeURIComponent(id)}/resend-signature`, { method: 'POST' })); }
      catch (e) { show(e); }
    }
    async function resubmitCase() {
      const id = cid();
      if (!id) return show({ error: 'case_id required' });
      try { show(await call(`/ops/cases/${encodeURIComponent(id)}/resubmit`, { method: 'POST' })); }
      catch (e) { show(e); }
    }
    async function loadGrievanceDocs() {
      const id = gid();
      if (!id) return show({ error: 'grievance_ref required' });
      try {
        const data = await call(`/ops/grievances/${encodeURIComponent(id)}/documents`);
        updateDocTypeSelect(data);
        show(data);
      } catch (e) { show(e); }
    }
    async function resubmitDocType() {
      const id = gid();
      const docType = docTypeSelect.value.trim();
      if (!id) return show({ error: 'grievance_ref required' });
      if (!docType) return show({ error: 'doc_type required; load grievance docs first' });
      try {
        show(await call(`/ops/grievances/${encodeURIComponent(id)}/resubmit?doc_type=${encodeURIComponent(docType)}`, { method: 'POST' }));
      } catch (e) { show(e); }
    }
    async function loadStandaloneTrace() {
      const id = sid();
      if (!id) return show({ error: 'submission_id required' });
      try { show(await call(`/ops/standalone/${encodeURIComponent(id)}/trace`)); }
      catch (e) { show(e); }
    }
    async function resubmitStandalone() {
      const id = sid();
      if (!id) return show({ error: 'submission_id required' });
      try { show(await call(`/ops/standalone/${encodeURIComponent(id)}/resubmit`, { method: 'POST' })); }
      catch (e) { show(e); }
    }
    async function clearCaseDocument(documentId) {
      if (!documentId) return show({ error: 'document_id required' });
      const reason = window.prompt('Reason for clearing this grievance document?', 'testing / false submission');
      if (reason === null) return;
      try {
        show(await call(`/ops/documents/${encodeURIComponent(documentId)}/clear?reason=${encodeURIComponent(reason)}`, { method: 'POST' }));
        await loadActiveSignatures();
      } catch (e) { show(e); }
    }
    async function clearStandaloneDocument(documentId) {
      if (!documentId) return show({ error: 'document_id required' });
      const reason = window.prompt('Reason for clearing this standalone form?', 'testing / false submission');
      if (reason === null) return;
      try {
        show(await call(`/ops/standalone-documents/${encodeURIComponent(documentId)}/clear?reason=${encodeURIComponent(reason)}`, { method: 'POST' }));
        await loadActiveSignatures();
      } catch (e) { show(e); }
    }
    async function fixCaseEmail(documentId, signerSummary) {
      if (!documentId) return show({ error: 'document_id required' });
      const currentEmail = window.prompt(`Current signer email to replace${signerSummary ? ` (${signerSummary})` : ''}:`, '');
      if (currentEmail === null) return;
      const newEmail = window.prompt('New signer email:', currentEmail || '');
      if (newEmail === null) return;
      const resend = window.confirm('Resend the DocuSeal signature request email to the corrected address?');
      try {
        show(await call(`/ops/documents/${encodeURIComponent(documentId)}/update-email?current_email=${encodeURIComponent(currentEmail)}&new_email=${encodeURIComponent(newEmail)}&resend_email=${resend ? 'true' : 'false'}`, { method: 'POST' }));
        await loadActiveSignatures();
      } catch (e) { show(e); }
    }
    async function fixStandaloneEmail(documentId, currentEmailValue) {
      if (!documentId) return show({ error: 'document_id required' });
      const currentEmail = window.prompt('Current signer email to replace:', currentEmailValue || '');
      if (currentEmail === null) return;
      const newEmail = window.prompt('New signer email:', currentEmail || '');
      if (newEmail === null) return;
      const resend = window.confirm('Resend the DocuSeal signature request email to the corrected address?');
      try {
        show(await call(`/ops/standalone-documents/${encodeURIComponent(documentId)}/update-email?current_email=${encodeURIComponent(currentEmail)}&new_email=${encodeURIComponent(newEmail)}&resend_email=${resend ? 'true' : 'false'}`, { method: 'POST' }));
        await loadActiveSignatures();
      } catch (e) { show(e); }
    }
  </script>
</body>
</html>
"""


@router.get("/ops/cases/{case_id}/trace")
async def ops_case_trace(case_id: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    return await _load_case_trace(db=db, case_id=case_id)


@router.get("/ops/grievances/{grievance_ref}/documents")
async def ops_grievance_documents(grievance_ref: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    return await _load_grievance_doc_catalog(db=db, grievance_ref=grievance_ref)


@router.get("/ops/active-signatures")
async def ops_active_signatures(request: Request, grievance_ref: str | None = None):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    return await _load_active_signature_queue(db=db, grievance_ref=grievance_ref)


@router.post("/ops/documents/{document_id}/clear")
async def ops_clear_document(document_id: str, request: Request, reason: str = ""):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    docuseal = request.app.state.docuseal
    return await _clear_case_document(
        db=db,
        docuseal=docuseal,
        document_id=document_id,
        reason=_normalize_ops_reason(reason),
    )


@router.post("/ops/standalone-documents/{document_id}/clear")
async def ops_clear_standalone_document(document_id: str, request: Request, reason: str = ""):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    docuseal = request.app.state.docuseal
    return await _clear_standalone_document(
        db=db,
        docuseal=docuseal,
        document_id=document_id,
        reason=_normalize_ops_reason(reason),
    )


@router.post("/ops/documents/{document_id}/update-email")
async def ops_update_document_email(
    document_id: str,
    request: Request,
    current_email: str = "",
    new_email: str = "",
    resend_email: bool = True,
):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    docuseal = request.app.state.docuseal
    return await _update_case_document_signer_email(
        db=db,
        docuseal=docuseal,
        document_id=document_id,
        current_email=current_email,
        new_email=new_email,
        resend_email=bool(resend_email),
    )


@router.post("/ops/standalone-documents/{document_id}/update-email")
async def ops_update_standalone_document_email(
    document_id: str,
    request: Request,
    current_email: str = "",
    new_email: str = "",
    resend_email: bool = True,
):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    docuseal = request.app.state.docuseal
    return await _update_standalone_document_signer_email(
        db=db,
        docuseal=docuseal,
        document_id=document_id,
        current_email=current_email,
        new_email=new_email,
        resend_email=bool(resend_email),
    )


@router.get("/ops/standalone/{submission_id}/trace")
async def ops_standalone_trace(submission_id: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    return await _load_standalone_trace(db=db, submission_id=submission_id)


@router.post("/ops/cases/{case_id}/resend-signature")
async def ops_resend_signature(case_id: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db

    docs = await db.fetchall(
        "SELECT id, requires_signature FROM documents WHERE case_id=? ORDER BY created_at_utc",
        (case_id,),
    )
    if not docs:
        raise HTTPException(status_code=404, detail="case_id not found")

    target_docs = [row[0] for row in docs if int(row[1] or 0) == 1]
    if not target_docs:
        raise HTTPException(status_code=400, detail="no signature documents for case")

    results: list[dict[str, object]] = []
    for doc_id in target_docs:
        body = {
            "template_key": "signature_request",
            "idempotency_key": f"ops-resend-{case_id}-{doc_id}-{int(time.time())}",
            "document_id": doc_id,
        }
        resp = await asyncio.to_thread(
            requests.post,
            f"http://127.0.0.1:8080/cases/{case_id}/notifications/resend",
            json=body,
            timeout=120,
        )
        payload = parse_json_safely(resp.text)
        results.append(
            {
                "document_id": doc_id,
                "status_code": resp.status_code,
                "ok": 200 <= resp.status_code < 300,
                "response": payload,
            }
        )
    return {"case_id": case_id, "results": results}


@router.post("/ops/cases/{case_id}/resubmit")
async def ops_resubmit(case_id: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    row = await db.fetchone("SELECT intake_payload_json FROM cases WHERE id=?", (case_id,))
    if not row:
        raise HTTPException(status_code=404, detail="case_id not found")

    payload = parse_json_safely(row[0])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="stored intake payload is not a JSON object")

    base_request_id = str(payload.get("request_id", case_id)).strip() or case_id
    new_request_id = _new_resubmit_request_id(base_request_id)
    payload["request_id"] = new_request_id

    parsed_response = await _post_internal_json(
        cfg=cfg,
        url="http://127.0.0.1:8080/intake",
        payload=payload,
    )

    return {
        "case_id": case_id,
        "new_request_id": new_request_id,
        "intake_response": parsed_response,
    }


@router.post("/ops/grievances/{grievance_ref}/resubmit")
async def ops_resubmit_by_grievance(grievance_ref: str, doc_type: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    doc_type_value = str(doc_type or "").strip()
    if not doc_type_value:
        raise HTTPException(status_code=400, detail="doc_type query parameter is required")

    row = await db.fetchone(
        """SELECT c.id, c.intake_payload_json, c.intake_request_id,
                  d.id, d.doc_type, d.template_key, d.requires_signature, d.signer_order_json
           FROM cases c
           JOIN documents d ON d.case_id=c.id
           WHERE (c.grievance_id=? OR c.grievance_number=?)
             AND (lower(d.doc_type)=lower(?) OR lower(COALESCE(d.template_key, ''))=lower(?))
           ORDER BY d.created_at_utc DESC, c.created_at_utc DESC, d.id DESC
           LIMIT 1""",
        (grievance_ref, grievance_ref, doc_type_value, doc_type_value),
    )
    if not row:
        raise HTTPException(status_code=404, detail="no matching grievance/doc_type found")

    payload = parse_json_safely(row[1])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="stored intake payload is not a JSON object")

    fallback_doc = {
        "document_id": row[3],
        "doc_type": row[4],
        "template_key": row[5],
        "requires_signature": bool(row[6]),
        "signer_order": parse_json_safely(row[7]),
    }
    filtered_payload = _filter_payload_documents_for_target(
        payload=payload,
        target_doc_type=doc_type_value,
        fallback_doc=fallback_doc,
    )

    base_request_id = str(filtered_payload.get("request_id", row[2] or row[0])).strip() or str(row[0])
    new_request_id = _new_resubmit_request_id(base_request_id)
    filtered_payload["request_id"] = new_request_id

    parsed_response = await _post_internal_json(
        cfg=cfg,
        url="http://127.0.0.1:8080/intake",
        payload=filtered_payload,
    )

    resubmitted_docs = filtered_payload.get("documents")
    resubmitted_doc_count = len(resubmitted_docs) if isinstance(resubmitted_docs, list) else 0

    return {
        "grievance_ref": grievance_ref,
        "doc_type": doc_type_value,
        "source_case_id": row[0],
        "source_document_id": row[3],
        "new_request_id": new_request_id,
        "resubmitted_document_count": resubmitted_doc_count,
        "intake_response": parsed_response,
    }


@router.post("/ops/standalone/{submission_id}/resubmit")
async def ops_resubmit_standalone(submission_id: str, request: Request):
    await _require_ops_api_access(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    row = await db.fetchone(
        """SELECT request_id, form_key, signer_email, template_data_json
           FROM standalone_submissions
           WHERE id=?""",
        (submission_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="submission_id not found")

    base_request_id = str(row[0] or submission_id).strip() or submission_id
    new_request_id = _new_resubmit_request_id(base_request_id)
    form_key = str(row[1] or "").strip()
    signer_email = str(row[2] or "").strip()
    template_data = parse_json_safely(row[3])
    if not isinstance(template_data, dict):
        raise HTTPException(status_code=500, detail="stored standalone template data is not a JSON object")

    payload: dict[str, object] = {
        "request_id": new_request_id,
        "form_key": form_key,
        "template_data": template_data,
    }
    if signer_email:
        payload["local_president_signer_email"] = signer_email

    parsed_response = await _post_internal_json(
        cfg=cfg,
        url=f"http://127.0.0.1:8080/standalone/forms/{form_key}/submissions",
        payload=payload,
    )

    return {
        "submission_id": submission_id,
        "new_request_id": new_request_id,
        "standalone_response": parsed_response,
    }
