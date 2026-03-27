from __future__ import annotations

import asyncio
import ipaddress
import json
import time

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core.hmac_auth import compute_signature
from ..db.db import Db

router = APIRouter()


def _require_local_access(request: Request) -> None:
    client_host = (request.client.host if request.client else "").strip()
    if client_host.lower() == "localhost":
        return
    try:
        ip = ipaddress.ip_address(client_host)
    except Exception as exc:
        raise HTTPException(status_code=403, detail="ops endpoints require local/private network access") from exc
    if not (ip.is_loopback or ip.is_private):
        raise HTTPException(status_code=403, detail="ops endpoints require local/private network access")


def _parse_json_safely(raw: object) -> object:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


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
                "signer_order": _parse_json_safely(row[5]),
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
                "details": _parse_json_safely(row[3]),
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
                "signer_order": _parse_json_safely(row[4]),
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
                "details": _parse_json_safely(row[3]),
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

    parsed_response = _parse_json_safely(resp.text)
    if not (200 <= resp.status_code < 300):
        raise HTTPException(status_code=resp.status_code, detail=parsed_response)
    return parsed_response


@router.get("/ops", response_class=HTMLResponse)
async def ops_page(request: Request):
    _require_local_access(request)
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Grievance Ops</title>
  <style>
    body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin: 24px; }
    .row { margin-bottom: 12px; }
    input { width: 420px; padding: 8px; }
    button { padding: 8px 12px; margin-right: 8px; }
    pre { background: #111; color: #ddd; padding: 12px; overflow: auto; max-height: 70vh; }
  </style>
</head>
<body>
  <h2>Grievance Ops</h2>
  <div class="row">
    <input id="caseId" placeholder="Case ID (example: C2026...)" />
  </div>
  <div class="row">
    <button onclick="loadTrace()">Load Trace</button>
    <button onclick="resendSignature()">Resend Signature Emails</button>
    <button onclick="resubmitCase()">Resubmit Case</button>
  </div>
  <div class="row" style="margin-top: 24px;">
    <input id="submissionId" placeholder="Standalone Submission ID (example: S2026...)" />
  </div>
  <div class="row">
    <button onclick="loadStandaloneTrace()">Load Standalone Trace</button>
    <button onclick="resubmitStandalone()">Resubmit Standalone</button>
  </div>
  <pre id="out">Ready.</pre>
  <script>
    const out = document.getElementById('out');
    const caseInput = document.getElementById('caseId');
    const submissionInput = document.getElementById('submissionId');
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
    function sid() { return submissionInput.value.trim(); }
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
  </script>
</body>
</html>
"""


@router.get("/ops/cases/{case_id}/trace")
async def ops_case_trace(case_id: str, request: Request):
    _require_local_access(request)
    db: Db = request.app.state.db
    return await _load_case_trace(db=db, case_id=case_id)


@router.get("/ops/standalone/{submission_id}/trace")
async def ops_standalone_trace(submission_id: str, request: Request):
    _require_local_access(request)
    db: Db = request.app.state.db
    return await _load_standalone_trace(db=db, submission_id=submission_id)


@router.post("/ops/cases/{case_id}/resend-signature")
async def ops_resend_signature(case_id: str, request: Request):
    _require_local_access(request)
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
        payload = _parse_json_safely(resp.text)
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
    _require_local_access(request)
    db: Db = request.app.state.db
    cfg = request.app.state.cfg

    row = await db.fetchone("SELECT intake_payload_json FROM cases WHERE id=?", (case_id,))
    if not row:
        raise HTTPException(status_code=404, detail="case_id not found")

    payload = _parse_json_safely(row[0])
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


@router.post("/ops/standalone/{submission_id}/resubmit")
async def ops_resubmit_standalone(submission_id: str, request: Request):
    _require_local_access(request)
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
    template_data = _parse_json_safely(row[3])
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
