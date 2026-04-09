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
from ..db.db import Db
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


def _render_hosted_form_page(definition: HostedFormDefinition, *, submit_path: str) -> str:
    title = escape(definition.title)
    description = escape(definition.description)
    fields_json = json.dumps(_field_payload(definition.fields), ensure_ascii=False)
    metadata_html = "".join(
        f"<span><strong>{escape(label)}</strong> {escape(value)}</span>"
        for label, value in definition.metadata
    )
    form_key = escape(definition.form_key)
    submit_path_js = json.dumps(submit_path)
    form_key_js = json.dumps(definition.form_key)
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
    .question {{
      background: var(--panel-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 20px;
      margin-bottom: 12px;
      border-left: 4px solid transparent;
      box-shadow: var(--shadow);
    }}
    .question:focus-within {{ border-left-color: var(--forms-green); }}
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
    textarea {{ min-height: 120px; resize: vertical; }}
    input:focus, textarea:focus, select:focus {{
      outline: 2px solid rgba(3, 120, 124, 0.25);
      border-color: var(--forms-green);
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
    .footer {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 16px;
      text-align: center;
    }}
    @media (max-width: 620px) {{
      .shell {{ padding: 14px 10px 32px; }}
      .header, .fixed-meta, .question, .status {{ padding-left: 14px; padding-right: 14px; }}
      h1 {{ font-size: 24px; }}
      .actions button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="header">
      <h1>{title}</h1>
      <p class="subtitle">{description}</p>
    </header>
    <div class="fixed-meta" aria-label="Workflow metadata">
      {metadata_html}
    </div>

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

    <div class="footer">
      Hosted form key: <strong>{form_key}</strong>
    </div>
  </main>

  <script>
    const FORM_KEY = {form_key_js};
    const FORM_ENDPOINT = {submit_path_js};
    const fields = {fields_json};

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
        return `<textarea id="${{id}}" name="${{esc(field.name)}}"${{required}}${{placeholder}}></textarea>`;
      }}
      if (field.type === 'select') {{
        const options = ['<option value=""></option>'].concat(
          (field.options || []).map((option) => `<option value="${{esc(option)}}">${{esc(option)}}</option>`)
        ).join('');
        return `<select id="${{id}}" name="${{esc(field.name)}}"${{required}}>${{options}}</select>`;
      }}
      return `<input id="${{id}}" name="${{esc(field.name)}}" type="${{esc(field.type || 'text')}}"${{required}}${{placeholder}} />`;
    }}

    function renderQuestions() {{
      questions.innerHTML = fields.map((field) => {{
        const id = `field-${{field.name}}`;
        const required = field.required ? ' required aria-required="true"' : '';
        const label = `${{esc(field.label)}}${{field.required ? ' <span class="required">*</span>' : ''}}`;
        const hint = field.hint ? `<p class="hint">${{esc(field.hint)}}</p>` : '';
        const placeholder = field.placeholder ? ` placeholder="${{esc(field.placeholder)}}"` : '';
        const control = renderControl(field, id, required, placeholder);
        return `<div class="question"><label for="${{id}}">${{label}}</label>${{hint}}${{control}}</div>`;
      }}).join('');
    }}

    function setStatus(state, title, message, details) {{
      statusPanel.classList.remove('hidden');
      statusPanel.dataset.state = state || '';
      statusTitle.textContent = title || '';
      statusMessage.textContent = message || '';
      statusDetails.textContent = details ? JSON.stringify(details, null, 2) : '';
    }}

    function collectPayload() {{
      const data = new FormData(form);
      const payload = {{ request_id: responseId() }};
      for (const field of fields) {{
        payload[field.name] = String(data.get(field.name) || '').trim();
      }}
      return payload;
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
      return 'The form was submitted successfully.';
    }}

    async function submitForm(event) {{
      event.preventDefault();
      if (!form.reportValidity()) return;
      submitBtn.disabled = true;
      savingText.classList.remove('hidden');
      setStatus('', 'Submitting', 'The request is being sent into the workflow.', null);
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
        setStatus('success', 'Submitted', successMessage(data), data);
        form.reset();
      }} catch (error) {{
        setStatus('error', 'Submission failed', 'Review the response below and try again.', error);
      }} finally {{
        submitBtn.disabled = false;
        savingText.classList.add('hidden');
      }}
    }}

    renderQuestions();
    form.addEventListener('submit', (event) => {{ void submitForm(event); }});
    resetBtn.addEventListener('click', () => {{
      form.reset();
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
    .linkish { color: var(--accent); text-decoration: none; }
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
    <section class="panel">
      <table>
        <thead>
          <tr>
            <th>Form</th>
            <th>Route</th>
            <th>Hosted Page</th>
            <th>Backend Path</th>
            <th>Visibility</th>
            <th>Enabled</th>
            <th>Save</th>
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

    function renderRows(rows) {
      rowsEl.innerHTML = rows.map((row) => {
        const meta = row.updated_at_utc ? `<div class="meta">Updated ${esc(row.updated_at_utc)}${row.updated_by ? ` by ${esc(row.updated_by)}` : ''}</div>` : '';
        return `
          <tr data-form-key="${esc(row.form_key)}">
            <td>
              <div class="row-title">${esc(row.title)}</div>
              <div class="meta">${esc(row.form_key)}</div>
              ${meta}
            </td>
            <td>${esc(row.route_type)}</td>
            <td><a class="linkish" href="${esc(row.public_path)}" target="_blank" rel="noreferrer">${esc(row.public_path)}</a></td>
            <td><span class="meta">${esc(row.target_path)}</span></td>
            <td>
              <select data-role="visibility">
                <option value="public"${row.visibility === 'public' ? ' selected' : ''}>public</option>
                <option value="private"${row.visibility === 'private' ? ' selected' : ''}>private</option>
              </select>
            </td>
            <td>
              <label class="enabled-wrap">
                <input data-role="enabled" type="checkbox"${row.enabled ? ' checked' : ''} />
                <span>${row.enabled ? 'enabled' : 'disabled'}</span>
              </label>
            </td>
            <td><button type="button" data-role="save">Save</button></td>
          </tr>
        `;
      }).join('');
    }

    async function loadRows() {
      setStatus('Loading hosted forms...', false);
      const response = await fetch('/officers/forms/settings');
      const data = await response.json();
      if (!response.ok) {
        throw new Error(JSON.stringify(data));
      }
      renderRows(data.rows || []);
      setStatus('Hosted forms loaded.', true);
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
      const button = event.target.closest('[data-role="save"]');
      if (!button) return;
      const tr = button.closest('tr');
      if (!tr) return;
      void saveRow(tr);
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
    backend_response = await _post_internal_json(
        cfg=request.app.state.cfg,
        url=f"{_INTERNAL_API_BASE}{definition.target_path}",
        payload=payload,
    )
    return {
        "request_id": payload["request_id"],
        "form_key": definition.form_key,
        "route_type": definition.route_type,
        "backend_response": backend_response,
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
