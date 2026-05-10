from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from html import escape
from math import ceil
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hmac_auth import compute_signature
from ..db.db import Db, utcnow
from .admin_common import parse_json_safely
from .hosted_forms_registry import (
    HostedFormDefinition,
    HostedFormRuntimeSettings,
    get_hosted_form_definition,
    list_hosted_form_definitions,
)
from .models import HostedFormAdminListResponse, HostedFormAdminRow, HostedFormSettingsUpdateRequest
from .officer_auth import (
    actor_identity,
    require_admin_user,
    require_authenticated_officer,
    require_officer_page_access,
    require_ops_page_access,
)

router = APIRouter()

_INTERNAL_API_BASE = "http://127.0.0.1:8080"
_PUBLIC_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
_PUBLIC_RATE_LIMIT_MAX_ATTEMPTS = 10
_PUBLIC_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
_PUBLIC_RATE_LIMIT_LOCK = asyncio.Lock()


def _normalize_visibility(value: object, *, default: str = "public") -> str:
    text = str(value or "").strip().lower()
    return text if text in {"public", "private"} else default


async def _load_runtime_settings(db: Db) -> dict[str, HostedFormRuntimeSettings]:
    rows = await db.hosted_form_settings_by_key()
    out: dict[str, HostedFormRuntimeSettings] = {}
    for form_key, row in rows.items():
        visibility, enabled, updated_by, updated_at_utc = row
        out[form_key] = HostedFormRuntimeSettings(
            form_key=form_key,
            visibility=_normalize_visibility(visibility),
            enabled=bool(enabled),
            updated_by=updated_by,
            updated_at_utc=updated_at_utc,
        )
    return out


async def _resolve_runtime_setting(db: Db, definition: HostedFormDefinition) -> HostedFormRuntimeSettings:
    settings = await _load_runtime_settings(db)
    existing = settings.get(definition.form_key)
    if existing:
        return existing
    return HostedFormRuntimeSettings(
        form_key=definition.form_key,
        visibility=definition.default_visibility,
        enabled=definition.default_enabled,
    )


def _public_form_path(form_key: str) -> str:
    return f"/forms/{form_key}"


def _hosted_forms_nav(*, forms_active: bool = False) -> str:
    forms_current = ' aria-current="page"' if forms_active else ""
    return f"""
    <nav class="top-menu" aria-label="Page navigation">
      <a href="/officers">Main tracker</a>
      <a href="/forms"{forms_current}>Hosted forms</a>
    </nav>
    """


def _build_internal_headers(*, cfg, body: bytes) -> dict[str, str]:  # noqa: ANN001
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


async def _post_internal_json(*, cfg, url: str, payload: dict[str, object]) -> object:  # noqa: ANN001
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _build_internal_headers(cfg=cfg, body=body)
    response = await asyncio.to_thread(
        requests.post,
        url,
        data=body,
        headers=headers,
        timeout=180,
    )
    parsed_response = parse_json_safely(response.text)
    if not (200 <= response.status_code < 300):
        raise HTTPException(status_code=response.status_code, detail=parsed_response)
    return parsed_response


def _client_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for", "") or "").strip()
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return str(request.client.host if request.client else "").strip() or "unknown"


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _boolish(value: object) -> bool:
    if value is True:
        return True
    return str(value or "").strip().lower() in {"true", "1", "yes", "on"}


def _signature_redirect_from_backend(backend_response: object) -> dict[str, str | None]:
    if not isinstance(backend_response, dict):
        return {"url": None, "document_id": None, "document_status": None}
    documents = backend_response.get("documents")
    if not isinstance(documents, list):
        return {"url": None, "document_id": None, "document_status": None}
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        url = str(doc.get("signing_link") or "").strip()
        status = str(doc.get("status") or "").strip()
        if url:
            return {
                "url": url,
                "document_id": str(doc.get("document_id") or "").strip() or None,
                "document_status": status or None,
            }
    first = next((doc for doc in documents if isinstance(doc, dict)), None)
    return {
        "url": None,
        "document_id": str(first.get("document_id") or "").strip() if first else None,
        "document_status": str(first.get("status") or "").strip() if first else None,
    }


def _requires_signature_attestation(form_key: str) -> bool:
    return form_key == "statement_of_occurrence"


def _officer_details_from_request(request: Request) -> dict[str, str]:
    session = getattr(request, "session", {}) or {}
    user = session.get("officer_user") if isinstance(session, dict) else None
    if not isinstance(user, dict):
        return {"email": "", "name": ""}
    email = str(user.get("email") or "").strip()
    name = str(user.get("display_name") or "").strip() or email
    return {"email": email, "name": name}


def _attach_signature_attestation(
    *,
    form_key: str,
    payload: dict[str, object],
    raw_values: dict[str, object],
    request: Request,
) -> None:
    if not _requires_signature_attestation(form_key):
        return
    raw_attestation = raw_values.get("_signature_attestation")
    attestation = raw_attestation if isinstance(raw_attestation, dict) else {}
    if not _boolish(attestation.get("accepted")):
        raise HTTPException(status_code=422, detail="electronic signature consent is required before signing")

    template_data = payload.get("template_data")
    template_data = template_data if isinstance(template_data, dict) else {}
    signer_name = _first_text(
        attestation.get("signer_typed_name"),
        f"{payload.get('grievant_firstname') or ''} {payload.get('grievant_lastname') or ''}",
    )
    signer_email = _first_text(
        attestation.get("signer_email"),
        template_data.get("personal_email"),
        payload.get("grievant_email"),
    )
    signer_phone = _first_text(
        attestation.get("signer_phone"),
        template_data.get("personal_cell"),
        payload.get("grievant_phone"),
    )
    officer = _officer_details_from_request(request)
    payload["_signature_attestation"] = {
        "accepted": True,
        "attestation_version": "statement_of_occurrence_attested_redirect_v1",
        "accepted_at_utc": _first_text(attestation.get("accepted_at_utc"), utcnow()),
        "signer_typed_name": signer_name,
        "signer_email": signer_email,
        "signer_phone": signer_phone,
        "officer_email": officer["email"],
        "officer_name": officer["name"],
        "client_ip": _client_ip(request),
        "user_agent": str(request.headers.get("user-agent", "") or "").strip(),
        "request_path": str(getattr(getattr(request, "url", None), "path", "")),
        "intent_text": "I agree to use electronic records and signatures and intend to sign this Statement of Occurrence electronically.",
    }


async def _record_signature_redirect_prepared(
    *,
    request: Request,
    form_key: str,
    backend_response: object,
    redirect: dict[str, str | None],
) -> None:
    if not _requires_signature_attestation(form_key):
        return
    url = redirect.get("url")
    if not url or not isinstance(backend_response, dict):
        return
    case_id = str(backend_response.get("case_id") or "").strip()
    if not case_id:
        return
    await request.app.state.db.add_event(
        case_id,
        redirect.get("document_id"),
        "signature_immediate_redirect_prepared",
        {
            "form_key": form_key,
            "prepared_at_utc": utcnow(),
            "document_status": redirect.get("document_status") or "",
            "signing_link_present": True,
            "client_ip": _client_ip(request),
            "user_agent": str(request.headers.get("user-agent", "") or "").strip(),
            "officer_email": _officer_details_from_request(request)["email"],
        },
    )


async def _enforce_public_rate_limit(request: Request, form_key: str) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    bucket_key = f"{form_key}:{ip}"
    async with _PUBLIC_RATE_LIMIT_LOCK:
        bucket = _PUBLIC_RATE_LIMIT_BUCKETS.setdefault(bucket_key, deque())
        while bucket and (now - bucket[0]) >= _PUBLIC_RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _PUBLIC_RATE_LIMIT_MAX_ATTEMPTS:
            retry_after = max(1, ceil(_PUBLIC_RATE_LIMIT_WINDOW_SECONDS - (now - bucket[0])))
            raise HTTPException(
                status_code=429,
                detail="too many form submissions from this IP; try again later",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


async def _require_form_access(
    *,
    request: Request,
    definition: HostedFormDefinition,
    settings: HostedFormRuntimeSettings,
    next_path: str,
    for_submit: bool,
):
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="hosted form not found")
    if settings.visibility != "private":
        if for_submit:
            await _enforce_public_rate_limit(request, definition.form_key)
        return None
    if for_submit:
        await require_authenticated_officer(request)
        return None
    gate = await require_officer_page_access(request, next_path=next_path)
    if isinstance(gate, RedirectResponse):
        return gate
    return None


def _field_payload(fields: tuple[Any, ...]) -> list[dict[str, object]]:
    return [
        {
            "name": field.name,
            "label": field.label,
            "type": field.type,
            "required": field.required,
            "placeholder": field.placeholder,
            "hint": field.hint,
            "options": list(field.options),
        }
        for field in fields
    ]


def _form_sections_payload(definition: HostedFormDefinition) -> list[dict[str, object]]:
    if definition.form_key == "referral":
        return [
            {
                "title": "Your Information",
                "summary": "Tell us who is making the referral and how officers can reach you.",
                "fields": ["referrer_name", "referrer_phone", "referrer_address", "referrer_email", "referrer_group"],
            },
            {
                "title": "Person You're Referring",
                "summary": "Add the person officers should follow up with. The AT&T UID is optional.",
                "fields": ["referred_name", "referred_group", "referred_att_uid"],
            },
            {
                "title": "Notes",
                "summary": "Share context that will help the officer follow up.",
                "fields": ["referral_notes"],
            },
        ]
    return [{"title": "", "summary": "", "fields": [field.name for field in definition.fields]}]


def _public_metadata(definition: HostedFormDefinition) -> tuple[tuple[str, str], ...]:
    return tuple(item for item in definition.metadata if item[0] == "Contract")


def _render_hosted_form_page(definition: HostedFormDefinition, *, submit_path: str) -> str:
    title = escape(definition.title)
    description = escape(definition.description)
    fields_json = json.dumps(_field_payload(definition.fields), ensure_ascii=False)
    sections_json = json.dumps(_form_sections_payload(definition), ensure_ascii=False)
    metadata_html = "".join(
        f"<span><strong>{escape(label)}</strong> {escape(value)}</span>"
        for label, value in _public_metadata(definition)
    )
    metadata_block = (
        f"""
    <div class="fixed-meta" aria-label="Form details">
      {metadata_html}
    </div>
        """
        if metadata_html
        else ""
    )
    submit_path_js = json.dumps(submit_path)
    form_key_js = json.dumps(definition.form_key)
    requires_attestation_js = json.dumps(_requires_signature_attestation(definition.form_key))
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --forms-green: #03787c;
      --forms-green-dark: #025c61;
      --page-bg: #f3f6f8;
      --panel-bg: #ffffff;
      --border: #d1dbe3;
      --text: #1b1f23;
      --muted: #5f6f7a;
      --error: #a4262c;
      --success: #107c10;
      --shadow: 0 14px 34px rgba(14, 30, 37, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(3, 120, 124, 0.12), transparent 34%),
        linear-gradient(180deg, #f7fbfc 0%, #eef3f6 100%);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      line-height: 1.45;
    }}
    a {{ color: var(--forms-green-dark); }}
    .shell {{ max-width: 920px; margin: 0 auto; padding: 28px 16px 44px; }}
    .top-menu {{
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .top-menu a {{
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 7px 12px;
      background: rgba(255, 255, 255, 0.92);
      color: var(--forms-green-dark);
      text-decoration: none;
      font-size: 14px;
      font-weight: 700;
      box-shadow: 0 8px 18px rgba(14, 30, 37, 0.05);
    }}
    .top-menu a:hover, .top-menu a:focus {{
      border-color: var(--forms-green-dark);
      outline: none;
    }}
    .header {{
      background: linear-gradient(180deg, var(--forms-green) 0%, var(--forms-green-dark) 100%);
      color: #fff;
      border-radius: 12px 12px 0 0;
      padding: 22px 24px;
      box-shadow: var(--shadow);
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; line-height: 1.15; font-weight: 600; }}
    .subtitle {{ margin: 0; font-size: 15px; max-width: 720px; }}
    .fixed-meta {{
      background: var(--panel-bg);
      border: 1px solid var(--border);
      border-top: 0;
      padding: 16px 24px;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      gap: 12px 18px;
      font-size: 14px;
      box-shadow: var(--shadow);
      border-radius: 0 0 12px 12px;
    }}
    .fixed-meta strong {{ color: var(--text); font-weight: 600; }}
    form {{ margin: 16px 0 0; }}
    .form-section {{
      background: var(--panel-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px;
      margin-bottom: 14px;
      box-shadow: var(--shadow);
    }}
    .form-section-header {{
      margin-bottom: 14px;
    }}
    .form-section-header h2 {{
      margin: 0 0 4px;
      font-size: 20px;
      line-height: 1.2;
    }}
    .form-section-header p {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .question-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .question {{
      background: #fbfdfe;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
      border-left: 4px solid transparent;
    }}
    .question.full-width {{ grid-column: 1 / -1; }}
    .question:focus-within {{ border-left-color: var(--forms-green); }}
    .attestation {{
      background: #fff;
      border: 1px solid var(--border);
      border-left: 4px solid var(--forms-green);
      border-radius: 10px;
      padding: 16px 18px;
      margin-bottom: 12px;
      box-shadow: var(--shadow);
    }}
    .attestation label {{
      display: flex;
      gap: 10px;
      align-items: flex-start;
      margin: 0;
    }}
    .attestation input {{ width: auto; margin-top: 3px; }}
    .attestation span {{ font-weight: 500; }}
    label {{ display: block; font-weight: 600; margin-bottom: 8px; }}
    .required {{ color: var(--error); }}
    .hint {{ color: var(--muted); font-size: 13px; margin: -2px 0 10px; }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid #aab7c2;
      border-radius: 6px;
      padding: 10px 11px;
      color: var(--text);
      background: #fff;
      font: inherit;
    }}
    textarea {{ min-height: 120px; overflow: hidden; resize: none; }}
    input:focus, textarea:focus, select:focus {{
      outline: 2px solid rgba(3, 120, 124, 0.25);
      border-color: var(--forms-green);
    }}
    .choice-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 8px;
    }}
    .choice-button {{
      width: 100%;
      border: 1px solid #aab7c2;
      border-radius: 10px;
      background: #fff;
      color: #18333a;
      padding: 10px 12px;
      text-align: center;
      font-weight: 700;
    }}
    .choice-button[aria-pressed="true"] {{
      border-color: var(--forms-green-dark);
      background: #e4f3f2;
      color: var(--forms-green-dark);
      box-shadow: inset 0 0 0 1px var(--forms-green-dark);
    }}
    .choice-button:focus {{
      outline: 2px solid rgba(3, 120, 124, 0.25);
      outline-offset: 2px;
    }}
    .custom-choice-input {{
      margin-top: 10px;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: flex-start;
      margin-top: 6px;
      flex-wrap: wrap;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 18px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    #submitBtn {{
      background: var(--forms-green);
      color: #fff;
      min-width: 148px;
    }}
    #submitBtn:disabled {{ opacity: 0.7; cursor: progress; }}
    .secondary {{
      background: #fff;
      color: var(--forms-green-dark);
      border: 1px solid var(--forms-green-dark);
    }}
    .status {{
      margin-top: 18px;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 18px;
      box-shadow: var(--shadow);
    }}
    .status[data-state="success"] {{ border-left: 4px solid var(--success); }}
    .status[data-state="error"] {{ border-left: 4px solid var(--error); }}
    .status h2 {{ margin: 0 0 8px; font-size: 18px; }}
    .status pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 10px 0 0;
      color: #24313a;
      font-size: 13px;
    }}
    .hidden {{ display: none; }}
    .status pre:empty {{ display: none; }}
    @media (max-width: 620px) {{
      .shell {{ padding: 14px 10px 32px; }}
      .header, .fixed-meta, .form-section, .question, .status {{ padding-left: 14px; padding-right: 14px; }}
      .question-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 24px; }}
      .actions button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    {_hosted_forms_nav()}
    <header class="header">
      <h1>{title}</h1>
      <p class="subtitle">{description}</p>
    </header>
    {metadata_block}

    <form id="hostedForm">
      <div id="questions"></div>
      <div class="actions">
        <button id="submitBtn" type="submit">Submit</button>
        <button class="secondary" id="resetBtn" type="button">Clear form</button>
        <span id="savingText" class="hint hidden">Submitting...</span>
      </div>
    </form>

    <section id="statusPanel" class="status hidden" aria-live="polite">
      <h2 id="statusTitle">Ready</h2>
      <div id="statusMessage"></div>
      <pre id="statusDetails"></pre>
    </section>

  </main>

  <script>
    const FORM_KEY = {form_key_js};
    const FORM_ENDPOINT = {submit_path_js};
    const REQUIRES_SIGNATURE_ATTESTATION = {requires_attestation_js};
    const fields = {fields_json};
    const fieldSections = {sections_json};

    const form = document.getElementById('hostedForm');
    const questions = document.getElementById('questions');
    const submitBtn = document.getElementById('submitBtn');
    const resetBtn = document.getElementById('resetBtn');
    const savingText = document.getElementById('savingText');
    const statusPanel = document.getElementById('statusPanel');
    const statusTitle = document.getElementById('statusTitle');
    const statusMessage = document.getElementById('statusMessage');
    const statusDetails = document.getElementById('statusDetails');

    function esc(value) {{
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    function responseId() {{
      const cryptoApi = window.crypto || null;
      const random = cryptoApi && cryptoApi.randomUUID ? cryptoApi.randomUUID() : String(Date.now());
      return `forms-hosted-${{FORM_KEY}}-${{random}}`;
    }}

    function renderControl(field, id, required, placeholder) {{
      if (field.type === 'textarea') {{
        return `<textarea class="auto-expand" id="${{id}}" name="${{esc(field.name)}}" rows="4"${{required}}${{placeholder}}></textarea>`;
      }}
      if (FORM_KEY === 'referral' && ['referrer_group', 'referred_group'].includes(field.name) && field.options && field.options.length) {{
        const buttons = field.options.map((option) => `
          <button class="choice-button" type="button" data-choice-for="${{id}}" data-choice-value="${{esc(option)}}" aria-pressed="false">${{esc(option)}}</button>
        `).join('');
        return `<input id="${{id}}" name="${{esc(field.name)}}" type="hidden"${{required}} />` +
          `<div class="choice-grid" role="group" aria-labelledby="${{id}}-label">${{buttons}}</div>` +
          `<input class="custom-choice-input" type="text" data-custom-choice-for="${{id}}" placeholder="Type another company or group" aria-label="Type another company or group" />`;
      }}
      if (field.type === 'select') {{
        const options = ['<option value=""></option>'].concat(
          (field.options || []).map((option) => `<option value="${{esc(option)}}">${{esc(option)}}</option>`)
        ).join('');
        return `<select id="${{id}}" name="${{esc(field.name)}}"${{required}}>${{options}}</select>`;
      }}
      return `<input id="${{id}}" name="${{esc(field.name)}}" type="${{esc(field.type || 'text')}}"${{required}}${{placeholder}} />`;
    }}

    function isLongField(field) {{
      return field.type === 'textarea' || ['referrer_address', 'referral_notes'].includes(field.name);
    }}

    function renderQuestion(field) {{
        const id = `field-${{field.name}}`;
        const required = field.required ? ' required aria-required="true"' : '';
        const label = `${{esc(field.label)}}${{field.required ? ' <span class="required">*</span>' : ''}}`;
        const hint = field.hint ? `<p class="hint">${{esc(field.hint)}}</p>` : '';
        const placeholder = field.placeholder ? ` placeholder="${{esc(field.placeholder)}}"` : '';
        const control = renderControl(field, id, required, placeholder);
        return `<div class="question${{isLongField(field) ? ' full-width' : ''}}"><label id="${{id}}-label" for="${{id}}">${{label}}</label>${{hint}}${{control}}</div>`;
    }}

    function renderSection(section, fieldByName) {{
      const sectionFields = (section.fields || []).map((name) => fieldByName.get(name)).filter(Boolean);
      if (!sectionFields.length) return '';
      const heading = section.title
        ? `<div class="form-section-header"><h2>${{esc(section.title)}}</h2>${{section.summary ? `<p>${{esc(section.summary)}}</p>` : ''}}</div>`
        : '';
      return `<section class="form-section">${{heading}}<div class="question-grid">${{sectionFields.map(renderQuestion).join('')}}</div></section>`;
    }}

    function renderQuestions() {{
      const fieldByName = new Map(fields.map((field) => [field.name, field]));
      const renderedFields = fieldSections.map((section) => renderSection(section, fieldByName)).join('');
      const attestation = REQUIRES_SIGNATURE_ATTESTATION
        ? `<div class="attestation">
             <label for="signatureConsent">
               <input id="signatureConsent" name="signatureConsent" type="checkbox" required aria-required="true" />
               <span>I agree to use electronic records and signatures for this Statement of Occurrence, and I intend to sign the generated document electronically.</span>
             </label>
           </div>`
        : '';
      questions.innerHTML = renderedFields + attestation;
      syncAutoExpandTextareas();
    }}

    function autoExpandTextarea(textarea) {{
      if (!textarea) return;
      textarea.style.height = 'auto';
      textarea.style.height = `${{textarea.scrollHeight}}px`;
    }}

    function syncAutoExpandTextareas() {{
      questions.querySelectorAll('textarea.auto-expand').forEach((textarea) => {{
        autoExpandTextarea(textarea);
        textarea.addEventListener('input', () => autoExpandTextarea(textarea));
      }});
    }}

    function syncChoiceButtons(input) {{
      const buttons = questions.querySelectorAll(`[data-choice-for="${{input.id}}"]`);
      buttons.forEach((button) => {{
        button.setAttribute('aria-pressed', button.dataset.choiceValue === input.value ? 'true' : 'false');
      }});
    }}

    function chooseOption(button) {{
      const input = document.getElementById(button.dataset.choiceFor || '');
      if (!input) return;
      input.value = button.dataset.choiceValue || '';
      const customInput = questions.querySelector(`[data-custom-choice-for="${{input.id}}"]`);
      if (customInput) customInput.value = '';
      input.dispatchEvent(new Event('input', {{ bubbles: true }}));
      syncChoiceButtons(input);
    }}

    function typeCustomOption(customInput) {{
      const input = document.getElementById(customInput.dataset.customChoiceFor || '');
      if (!input) return;
      input.value = customInput.value.trim();
      input.dispatchEvent(new Event('input', {{ bubbles: true }}));
      syncChoiceButtons(input);
    }}

    function validateChoiceButtons() {{
      for (const input of questions.querySelectorAll('input[type="hidden"][required]')) {{
        if (String(input.value || '').trim()) continue;
        const label = document.getElementById(`${{input.id}}-label`);
        setStatus('error', 'Selection required', `Choose ${{label ? label.textContent.replace('*', '').trim().toLowerCase() : 'an option'}} before submitting.`, '');
        const firstButton = questions.querySelector(`[data-choice-for="${{input.id}}"]`);
        if (firstButton) firstButton.focus();
        return false;
      }}
      return true;
    }}

    function setStatus(state, title, message, details) {{
      statusPanel.classList.remove('hidden');
      statusPanel.dataset.state = state || '';
      statusTitle.textContent = title || '';
      statusMessage.textContent = message || '';
      statusDetails.textContent = details || '';
    }}

    function collectPayload() {{
      const data = new FormData(form);
      const payload = {{ request_id: responseId() }};
      for (const field of fields) {{
        payload[field.name] = String(data.get(field.name) || '').trim();
      }}
      if (REQUIRES_SIGNATURE_ATTESTATION) {{
        const first = String(payload.grievant_firstname || '').trim();
        const last = String(payload.grievant_lastname || '').trim();
        payload._signature_attestation = {{
          accepted: Boolean(data.get('signatureConsent')),
          accepted_at_utc: new Date().toISOString(),
          signer_typed_name: `${{first}} ${{last}}`.trim(),
          signer_email: String(payload.personal_email || payload.grievant_email || '').trim(),
          signer_phone: String(payload.personal_cell || payload.grievant_phone || '').trim()
        }};
      }}
      return payload;
    }}

    function redirectInfo(data) {{
      if (!data || typeof data !== 'object') return null;
      if (data.signing_redirect_url) {{
        return {{
          url: data.signing_redirect_url,
          reason: data.signing_redirect_reason || 'ready'
        }};
      }}
      if (data.intake_response && data.intake_response.documents) {{
        const doc = data.intake_response.documents.find((item) => item && item.signing_link);
        if (doc) return {{ url: doc.signing_link, reason: 'ready' }};
      }}
      return null;
    }}

    function successMessage(data) {{
      const backend = data && data.backend_response ? data.backend_response : data;
      if (backend && backend.case_id) {{
        const grievance = backend.grievance_id ? ` Grievance ${{backend.grievance_id}}.` : '';
        return `Case ${{backend.case_id}} was submitted.${{grievance}}`;
      }}
      if (backend && backend.submission_id) {{
        return `Standalone submission ${{backend.submission_id}} was created.`;
      }}
      if (backend && backend.referral_id) {{
        return 'Referral submitted. An officer will review it and follow up.';
      }}
      return 'The form was submitted successfully.';
    }}

    function failureMessage(error) {{
      const data = error && error.data ? error.data : error;
      const detail = data && typeof data === 'object' ? String(data.detail || '') : String(data || '');
      if (detail.toLowerCase().includes('sunset date has passed')) {{
        return 'This referral form is currently closed. Please contact an officer if the referral window has been extended.';
      }}
      if (detail.toLowerCase().includes('rate limit')) {{
        return 'Too many attempts were submitted from this connection. Please wait a few minutes and try again.';
      }}
      return 'We could not submit the form. Check the required fields and try again.';
    }}

    async function submitForm(event) {{
      event.preventDefault();
      if (!validateChoiceButtons()) return;
      if (!form.reportValidity()) return;
      submitBtn.disabled = true;
      savingText.classList.remove('hidden');
      setStatus('', 'Processing', 'The form is being submitted. Any next step will open when it is ready.', null);
      try {{
        const response = await fetch(FORM_ENDPOINT, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(collectPayload())
        }});
        const text = await response.text();
        let data = text;
        try {{ data = JSON.parse(text); }} catch {{}}
        if (!response.ok) throw {{ status: response.status, data }};
        const redirect = redirectInfo(data);
        if (redirect && redirect.url) {{
          setStatus('success', 'Ready to sign', 'Opening the signature page now.', '');
          window.setTimeout(() => {{
            window.location.assign(redirect.url);
          }}, 650);
        }} else {{
          setStatus('success', 'Submitted', successMessage(data), '');
        }}
        form.reset();
      }} catch (error) {{
        setStatus('error', 'Submission failed', failureMessage(error), '');
      }} finally {{
        submitBtn.disabled = false;
        savingText.classList.add('hidden');
      }}
    }}

    renderQuestions();
    questions.addEventListener('click', (event) => {{
      const button = event.target.closest('[data-choice-for]');
      if (!button) return;
      chooseOption(button);
    }});
    questions.addEventListener('input', (event) => {{
      const customInput = event.target.closest('[data-custom-choice-for]');
      if (!customInput) return;
      typeCustomOption(customInput);
    }});
    form.addEventListener('submit', (event) => {{ void submitForm(event); }});
    resetBtn.addEventListener('click', () => {{
      form.reset();
      questions.querySelectorAll('[data-custom-choice-for]').forEach((input) => {{ input.value = ''; }});
      questions.querySelectorAll('input[type="hidden"]').forEach(syncChoiceButtons);
      statusPanel.classList.add('hidden');
    }});
  </script>
</body>
</html>
"""


def _render_hosted_forms_index(definitions: tuple[HostedFormDefinition, ...]) -> str:
    cards = "".join(
        f"""
        <a class="card" href="{escape(_public_form_path(definition.form_key), quote=True)}">
          <div class="card-title">{escape(definition.title)}</div>
          <div class="card-summary">{escape(definition.description)}</div>
          <div class="card-meta">
            <span>{escape(definition.route_type)}</span>
            <span>{escape(definition.target_path)}</span>
          </div>
        </a>
        """
        for definition in definitions
    )
    if not cards:
        cards = '<div class="empty">No public hosted forms are currently enabled.</div>'
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hosted Forms</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef3f6;
      --card: #fff;
      --text: #1b1f23;
      --muted: #5f6f7a;
      --accent: #03787c;
      --border: #d6dfe6;
      --shadow: 0 18px 38px rgba(14, 30, 37, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(3, 120, 124, 0.12), transparent 32%),
        linear-gradient(180deg, #f7fbfc 0%, var(--bg) 100%);
    }}
    .shell {{ max-width: 1020px; margin: 0 auto; padding: 28px 16px 48px; }}
    .top-menu {{
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }}
    .top-menu a {{
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 7px 12px;
      background: rgba(255, 255, 255, 0.92);
      color: #025c61;
      text-decoration: none;
      font-size: 14px;
      font-weight: 700;
      box-shadow: 0 8px 18px rgba(14, 30, 37, 0.05);
    }}
    .top-menu a:hover, .top-menu a:focus, .top-menu a[aria-current="page"] {{
      border-color: #025c61;
      outline: none;
    }}
    .hero {{
      background: linear-gradient(180deg, #03787c 0%, #025c61 100%);
      color: #fff;
      border-radius: 18px;
      padding: 28px 24px;
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 34px; line-height: 1.1; }}
    .subtitle {{ margin: 0; max-width: 760px; font-size: 15px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
    }}
    .card {{
      display: block;
      text-decoration: none;
      color: inherit;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
      box-shadow: var(--shadow);
    }}
    .card:hover {{
      transform: translateY(-1px);
      border-color: #b5c9d5;
    }}
    .card-title {{ font-size: 18px; font-weight: 700; margin-bottom: 8px; }}
    .card-summary {{ color: var(--muted); font-size: 14px; min-height: 58px; }}
    .card-meta {{
      margin-top: 14px;
      color: var(--accent);
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      font-size: 13px;
      font-weight: 600;
    }}
    .empty {{
      background: #fff;
      border: 1px dashed var(--border);
      border-radius: 16px;
      padding: 22px;
      color: var(--muted);
      text-align: center;
    }}
  </style>
</head>
<body>
  <main class="shell">
    {_hosted_forms_nav(forms_active=True)}
    <section class="hero">
      <h1>Hosted Forms</h1>
      <p class="subtitle">Use these web pages as the second input path alongside Microsoft Forms and Power Automate. Only currently enabled public forms appear here.</p>
    </section>
    <section class="grid">
      {cards}
    </section>
  </main>
</body>
</html>
"""


def _render_admin_page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Hosted Form Controls</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --text: #1f2328;
      --muted: #5c6c76;
      --accent: #0b6e75;
      --border: #d6dfe6;
      --shadow: 0 16px 36px rgba(14, 30, 37, 0.08);
      --success: #107c10;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      color: var(--text);
      background: linear-gradient(180deg, #f7fbfc 0%, var(--bg) 100%);
    }
    .shell { max-width: 1180px; margin: 0 auto; padding: 28px 16px 48px; }
    .hero {
      background: linear-gradient(180deg, #0b6e75 0%, #084e53 100%);
      color: #fff;
      border-radius: 18px;
      padding: 26px 24px;
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }
    h1 { margin: 0 0 8px; font-size: 32px; }
    .subtitle { margin: 0; max-width: 780px; font-size: 15px; }
    .toolbar {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: end;
      flex-wrap: wrap;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      box-shadow: var(--shadow);
      margin-bottom: 14px;
    }
    .toolbar label {
      display: grid;
      gap: 6px;
      min-width: min(420px, 100%);
      font-size: 13px;
      font-weight: 700;
      color: #334650;
    }
    .toolbar input {
      border: 1px solid #aab7c2;
      border-radius: 8px;
      padding: 9px 10px;
      font: inherit;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    table { width: 100%; border-collapse: collapse; }
    th, td {
      padding: 14px 12px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      text-align: left;
      font-size: 14px;
    }
    th {
      background: #f8fbfc;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    tr:last-child td { border-bottom: 0; }
    select, button {
      font: inherit;
      border-radius: 8px;
    }
    select {
      min-width: 110px;
      padding: 8px 10px;
      border: 1px solid #aab7c2;
      background: #fff;
    }
    .enabled-wrap {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }
    button {
      border: 0;
      background: var(--accent);
      color: #fff;
      padding: 9px 12px;
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled { opacity: 0.65; cursor: progress; }
    .meta { color: var(--muted); font-size: 12px; }
    .row-title { font-weight: 700; margin-bottom: 4px; }
    .route-pill {
      display: inline-flex;
      border-radius: 999px;
      padding: 4px 8px;
      background: #eef6f7;
      color: #075b61;
      font-size: 12px;
      font-weight: 700;
    }
    .linkish { color: var(--accent); text-decoration: none; }
    .public-link-wrap {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      gap: 8px;
      align-items: center;
      max-width: 420px;
    }
    .public-link-input {
      width: 100%;
      min-width: 0;
      border: 1px solid #aab7c2;
      border-radius: 8px;
      padding: 8px 10px;
      font: inherit;
      color: var(--text);
      background: #fff;
    }
    .copy-button {
      white-space: nowrap;
      padding: 8px 10px;
    }
    .form-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .open-button {
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      padding: 8px 10px;
      font-weight: 700;
    }
    .copy-button.secondary {
      background: #fff;
      color: var(--accent);
      border: 1px solid var(--accent);
    }
    details {
      color: var(--muted);
      font-size: 13px;
    }
    summary {
      cursor: pointer;
      color: var(--accent);
      font-weight: 700;
    }
    .detail-grid {
      display: grid;
      gap: 5px;
      margin-top: 8px;
    }
    .private-note {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }
    .status {
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .ok { color: var(--success); }
    @media (max-width: 900px) {
      .panel { overflow-x: auto; }
      table { min-width: 980px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <h1>Hosted Form Controls</h1>
      <p class="subtitle">Manage which hosted forms are enabled, whether they are public or private, and where each page points in the backend. Public/private changes take effect immediately without a redeploy.</p>
    </section>
    <section class="toolbar">
      <label>Find a hosted form
        <input id="formSearchInput" type="search" placeholder="Search by title, key, or route type" />
      </label>
      <div class="meta">Use this page to copy the public referral link and control form visibility.</div>
    </section>
    <section class="panel">
      <table>
        <thead>
          <tr>
            <th>Form</th>
            <th>Public Link</th>
            <th>Settings</th>
            <th>Technical Details</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </section>
    <div class="status" id="statusText">Loading hosted forms...</div>
  </main>

  <script>
    const rowsEl = document.getElementById('rows');
    const statusEl = document.getElementById('statusText');
    const searchInput = document.getElementById('formSearchInput');
    let allRows = [];

    function esc(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function setStatus(text, ok) {
      statusEl.textContent = text;
      statusEl.className = ok ? 'status ok' : 'status';
    }

    function publicUrl(path) {
      return new URL(path, window.location.origin).toString();
    }

    function hostedPageCell(row) {
      if (row.visibility !== 'public') {
        return `<div class="private-note">Private access. Set visibility to public and save to share a public link.</div>`;
      }
      const url = publicUrl(row.public_path);
      const disabledNote = row.enabled ? '' : '<div class="meta">This form is public but currently disabled.</div>';
      return `
        <div class="public-link-wrap">
          <input class="public-link-input" data-role="public-link" type="text" readonly value="${esc(url)}" />
          <button class="copy-button secondary" type="button" data-role="copy-public-link">Copy Link</button>
        </div>
        <div class="form-actions">
          <a class="open-button" href="${esc(row.public_path)}" target="_blank" rel="noreferrer">Open Public Page</a>
        </div>
        ${disabledNote}
      `;
    }

    function technicalDetails(row) {
      return `
        <details>
          <summary>Technical details</summary>
          <div class="detail-grid">
            <div><strong>Route:</strong> ${esc(row.route_type)}</div>
            <div><strong>Backend path:</strong> ${esc(row.target_path)}</div>
            <div><strong>Public path:</strong> ${esc(row.public_path)}</div>
          </div>
        </details>
      `;
    }

    function filteredRows() {
      const query = searchInput.value.trim().toLowerCase();
      if (!query) return allRows;
      return allRows.filter((row) => [
        row.title,
        row.form_key,
        row.route_type,
        row.public_path
      ].join(' ').toLowerCase().includes(query));
    }

    function renderRows(rows) {
      if (!rows.length) {
        rowsEl.innerHTML = '<tr><td colspan="4" class="private-note">No hosted forms match the current search.</td></tr>';
        return;
      }
      rowsEl.innerHTML = rows.map((row) => {
        const meta = row.updated_at_utc ? `<div class="meta">Updated ${esc(row.updated_at_utc)}${row.updated_by ? ` by ${esc(row.updated_by)}` : ''}</div>` : '';
        return `
          <tr data-form-key="${esc(row.form_key)}">
            <td>
              <div class="row-title">${esc(row.title)}</div>
              <div class="meta">${esc(row.form_key)}</div>
              <div><span class="route-pill">${esc(row.route_type)}</span></div>
              ${meta}
            </td>
            <td>${hostedPageCell(row)}</td>
            <td>
              <select data-role="visibility">
                <option value="public"${row.visibility === 'public' ? ' selected' : ''}>public</option>
                <option value="private"${row.visibility === 'private' ? ' selected' : ''}>private</option>
              </select>
              <label class="enabled-wrap">
                <input data-role="enabled" type="checkbox"${row.enabled ? ' checked' : ''} />
                <span>${row.enabled ? 'enabled' : 'disabled'}</span>
              </label>
              <div class="form-actions"><button type="button" data-role="save">Save Settings</button></div>
            </td>
            <td>${technicalDetails(row)}</td>
          </tr>
        `;
      }).join('');
    }

    async function copyText(text) {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', 'readonly');
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      textarea.remove();
    }

    async function loadRows() {
      setStatus('Loading hosted forms...', false);
      const response = await fetch('/officers/forms/settings');
      const data = await response.json();
      if (!response.ok) {
        throw new Error(JSON.stringify(data));
      }
      allRows = data.rows || [];
      renderRows(filteredRows());
      setStatus(`${allRows.length} hosted forms loaded.`, true);
    }

    async function saveRow(tr) {
      const button = tr.querySelector('[data-role="save"]');
      const visibility = tr.querySelector('[data-role="visibility"]').value;
      const enabled = tr.querySelector('[data-role="enabled"]').checked;
      const formKey = tr.dataset.formKey;
      button.disabled = true;
      setStatus(`Saving ${formKey}...`, false);
      try {
        const response = await fetch(`/officers/forms/${encodeURIComponent(formKey)}/settings`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ visibility, enabled })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(JSON.stringify(data));
        await loadRows();
        setStatus(`Saved ${formKey}.`, true);
      } finally {
        button.disabled = false;
      }
    }

    rowsEl.addEventListener('click', (event) => {
      const copyButton = event.target.closest('[data-role="copy-public-link"]');
      if (copyButton) {
        const tr = copyButton.closest('tr');
        const input = tr ? tr.querySelector('[data-role="public-link"]') : null;
        if (!input) return;
        void copyText(input.value).then(() => {
          input.select();
          setStatus(`Copied public link for ${tr.dataset.formKey}.`, true);
        }).catch((error) => {
          setStatus(`Unable to copy link: ${error.message}`, false);
        });
        return;
      }
      const button = event.target.closest('[data-role="save"]');
      if (!button) return;
      const tr = button.closest('tr');
      if (!tr) return;
      void saveRow(tr);
    });
    searchInput.addEventListener('input', () => {
      renderRows(filteredRows());
    });

    void loadRows().catch((error) => {
      setStatus(`Unable to load hosted forms: ${error.message}`, false);
    });
  </script>
</body>
</html>
"""


async def submit_hosted_form(
    form_key: str,
    raw_values: dict[str, object],
    request: Request,
    *,
    bypass_visibility: bool = False,
):
    definition = get_hosted_form_definition(form_key)
    if not definition:
        raise HTTPException(status_code=404, detail="hosted form not found")
    settings = await _resolve_runtime_setting(request.app.state.db, definition)
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="hosted form not found")
    if not bypass_visibility:
        await _require_form_access(
            request=request,
            definition=definition,
            settings=settings,
            next_path=_public_form_path(definition.form_key),
            for_submit=True,
        )
    try:
        payload = definition.build_payload(raw_values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _attach_signature_attestation(
        form_key=definition.form_key,
        payload=payload,
        raw_values=raw_values,
        request=request,
    )
    backend_response = await _post_internal_json(
        cfg=request.app.state.cfg,
        url=f"{_INTERNAL_API_BASE}{definition.target_path}",
        payload=payload,
    )
    redirect = _signature_redirect_from_backend(backend_response)
    await _record_signature_redirect_prepared(
        request=request,
        form_key=definition.form_key,
        backend_response=backend_response,
        redirect=redirect,
    )
    return {
        "request_id": payload["request_id"],
        "form_key": definition.form_key,
        "route_type": definition.route_type,
        "backend_response": backend_response,
        "signing_redirect_url": redirect.get("url"),
        "signing_redirect_reason": "ready" if redirect.get("url") else "not_available",
        "document_status": redirect.get("document_status"),
    }


async def render_hosted_form_alias_page(
    *,
    form_key: str,
    submit_path: str,
    request: Request,
    next_path: str,
):
    definition = get_hosted_form_definition(form_key)
    if not definition:
        raise HTTPException(status_code=404, detail="hosted form not found")
    settings = await _resolve_runtime_setting(request.app.state.db, definition)
    gate = await _require_form_access(
        request=request,
        definition=definition,
        settings=settings,
        next_path=next_path,
        for_submit=False,
    )
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(_render_hosted_form_page(definition, submit_path=submit_path))


def _admin_row(definition: HostedFormDefinition, settings: HostedFormRuntimeSettings) -> HostedFormAdminRow:
    return HostedFormAdminRow(
        form_key=definition.form_key,
        title=definition.title,
        route_type=definition.route_type,
        public_path=_public_form_path(definition.form_key),
        target_path=definition.target_path,
        visibility=settings.visibility,
        enabled=settings.enabled,
        default_visibility=definition.default_visibility,
        default_enabled=definition.default_enabled,
        updated_by=settings.updated_by,
        updated_at_utc=settings.updated_at_utc,
    )


@router.get("/forms", response_class=HTMLResponse)
async def hosted_forms_index(request: Request):
    db: Db = request.app.state.db
    settings = await _load_runtime_settings(db)
    visible = tuple(
        definition
        for definition in list_hosted_form_definitions()
        if (settings.get(definition.form_key) or HostedFormRuntimeSettings(definition.form_key, definition.default_visibility, definition.default_enabled)).enabled
        and (settings.get(definition.form_key) or HostedFormRuntimeSettings(definition.form_key, definition.default_visibility, definition.default_enabled)).visibility
        == "public"
    )
    return HTMLResponse(_render_hosted_forms_index(visible))


@router.get("/forms/{form_key}", response_class=HTMLResponse)
async def hosted_form_page(form_key: str, request: Request):
    return await render_hosted_form_alias_page(
        form_key=form_key,
        submit_path=f"/forms/{form_key}/submissions",
        request=request,
        next_path=_public_form_path(form_key),
    )


@router.post("/forms/{form_key}/submissions")
async def hosted_form_submission(form_key: str, request: Request):
    try:
        raw_payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="hosted form payload must be a JSON object") from exc
    if not isinstance(raw_payload, dict):
        raise HTTPException(status_code=400, detail="hosted form payload must be a JSON object")
    return await submit_hosted_form(form_key, raw_payload, request)


@router.get("/officers/forms", response_class=HTMLResponse)
async def hosted_forms_admin_page(request: Request):
    gate = await require_ops_page_access(request, next_path="/officers/forms")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(_render_admin_page())


@router.get("/officers/forms/settings", response_model=HostedFormAdminListResponse)
async def hosted_forms_admin_settings(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    db: Db = request.app.state.db
    settings = await _load_runtime_settings(db)
    rows = [
        _admin_row(
            definition,
            settings.get(definition.form_key)
            or HostedFormRuntimeSettings(
                form_key=definition.form_key,
                visibility=definition.default_visibility,
                enabled=definition.default_enabled,
            ),
        )
        for definition in list_hosted_form_definitions()
    ]
    return HostedFormAdminListResponse(rows=rows)


@router.patch("/officers/forms/{form_key}/settings", response_model=HostedFormAdminRow)
async def update_hosted_form_setting(
    form_key: str,
    body: HostedFormSettingsUpdateRequest,
    request: Request,
):
    user = await require_admin_user(request, allow_local_fallback=True)
    definition = get_hosted_form_definition(form_key)
    if not definition:
        raise HTTPException(status_code=404, detail="hosted form not found")
    existing = await _resolve_runtime_setting(request.app.state.db, definition)
    visibility = _normalize_visibility(body.visibility, default=existing.visibility)
    enabled = existing.enabled if body.enabled is None else bool(body.enabled)
    await request.app.state.db.upsert_hosted_form_setting(
        form_key=definition.form_key,
        visibility=visibility,
        enabled=enabled,
        updated_by=actor_identity(user, fallback="admin"),
    )
    updated = await _resolve_runtime_setting(request.app.state.db, definition)
    return _admin_row(definition, updated)
