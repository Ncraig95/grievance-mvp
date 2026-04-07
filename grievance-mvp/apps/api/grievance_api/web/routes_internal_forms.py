from __future__ import annotations

import asyncio
import json
import time
from html import escape
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..core.hmac_auth import compute_signature
from .admin_common import parse_json_safely
from .officer_auth import require_authenticated_officer, require_officer_page_access

router = APIRouter()

_INTERNAL_INTAKE_URL = "http://127.0.0.1:8080/intake"
_NON_DISCIPLINE_COMMAND = "non_discipline_brief"
_NON_DISCIPLINE_TITLE = "Non-Discipline Grievance Brief"


class NonDisciplineInternalFormSubmission(BaseModel):
    request_id: str | None = None
    grievant_firstname: str
    grievant_lastname: str
    grievant_email: str
    local_number: str
    local_grievance_number: str | None = None
    location: str
    grievant_or_work_group: str
    grievant_home_address: str
    date_grievance_occurred: str
    date_grievance_filed: str
    date_grievance_appealed_to_executive_level: str | None = None
    issue_or_condition_involved: str
    action_taken: str
    chronology_of_facts: str
    analysis_of_grievance: str
    current_status: str
    union_position: str
    company_position: str
    potential_witnesses: str | None = None
    recommendation: str
    attachment_1: str | None = None
    attachment_2: str | None = None
    attachment_3: str | None = None
    attachment_4: str | None = None
    attachment_5: str | None = None
    attachment_6: str | None = None
    attachment_7: str | None = None
    attachment_8: str | None = None
    attachment_9: str | None = None
    attachment_10: str | None = None
    signer_email: str | None = None


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


async def _post_internal_json(*, cfg, url: str, payload: dict[str, object]) -> object:  # noqa: ANN001
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _build_intake_headers(cfg=cfg, body=body)
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


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _require_text(values: dict[str, Any], key: str, label: str) -> str:
    text = _clean_text(values.get(key))
    if not text:
        raise HTTPException(status_code=422, detail=f"{label} is required")
    return text


def _build_non_discipline_intake_payload(body: NonDisciplineInternalFormSubmission) -> dict[str, object]:
    values = body.model_dump()
    first_name = _require_text(values, "grievant_firstname", "Grievant first name")
    last_name = _require_text(values, "grievant_lastname", "Grievant last name")
    grievant_email = _require_text(values, "grievant_email", "Grievant email")
    if "@" not in grievant_email:
        raise HTTPException(status_code=422, detail="Grievant email must be a valid email address")

    signer_email = _clean_text(values.get("signer_email"))
    if signer_email and "@" not in signer_email:
        raise HTTPException(status_code=422, detail="Signer email override must be a valid email address")

    request_id = _clean_text(values.get("request_id")) or f"forms-internal-non-discipline-{time.time_ns()}"
    grievant_name = f"{first_name} {last_name}".strip()
    template_data: dict[str, object] = {
        "grievant_name": grievant_name,
        "local_number": _require_text(values, "local_number", "Local number"),
        "local_grievance_number": _clean_text(values.get("local_grievance_number")),
        "location": _require_text(values, "location", "Location"),
        "grievant_or_work_group": _require_text(values, "grievant_or_work_group", "Grievant(s) or work group"),
        "grievant_home_address": _require_text(values, "grievant_home_address", "Grievant home address"),
        "date_grievance_occurred": _require_text(values, "date_grievance_occurred", "Date grievance occurred"),
        "date_grievance_filed": _require_text(values, "date_grievance_filed", "Date grievance filed"),
        "date_grievance_appealed_to_executive_level": _clean_text(
            values.get("date_grievance_appealed_to_executive_level")
        ),
        "issue_or_condition_involved": _require_text(values, "issue_or_condition_involved", "Issue or condition involved"),
        "action_taken": _require_text(values, "action_taken", "Action taken"),
        "chronology_of_facts": _require_text(values, "chronology_of_facts", "Chronology of facts"),
        "analysis_of_grievance": _require_text(values, "analysis_of_grievance", "Analysis of grievance"),
        "current_status": _require_text(values, "current_status", "Current status"),
        "union_position": _require_text(values, "union_position", "Union position"),
        "company_position": _require_text(values, "company_position", "Company position"),
        "potential_witnesses": _clean_text(values.get("potential_witnesses")),
        "recommendation": _require_text(values, "recommendation", "Recommendation"),
        "signer_email": signer_email,
    }
    for idx in range(1, 11):
        template_data[f"attachment_{idx}"] = _clean_text(values.get(f"attachment_{idx}"))

    return {
        "request_id": request_id,
        "document_command": _NON_DISCIPLINE_COMMAND,
        "contract": "CWA",
        "grievant_firstname": first_name,
        "grievant_lastname": last_name,
        "grievant_email": grievant_email,
        "narrative": "Non-discipline grievance brief",
        "template_data": template_data,
    }


def _render_non_discipline_form_page() -> str:
    title = escape(_NON_DISCIPLINE_TITLE)
    command = escape(_NON_DISCIPLINE_COMMAND)
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
      --border: #d1dbe3;
      --text: #1b1f23;
      --muted: #5f6f7a;
      --error: #a4262c;
      --success: #107c10;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--page-bg);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      line-height: 1.45;
    }}
    .shell {{ max-width: 860px; margin: 0 auto; padding: 28px 16px 44px; }}
    .header {{
      background: var(--forms-green);
      color: #fff;
      border-radius: 8px 8px 0 0;
      padding: 20px 24px;
    }}
    h1 {{ margin: 0 0 6px; font-size: 28px; line-height: 1.2; font-weight: 600; }}
    .subtitle {{ margin: 0; font-size: 15px; }}
    .fixed-meta {{
      background: #fff;
      border: 1px solid var(--border);
      border-top: 0;
      padding: 16px 24px;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      gap: 12px 18px;
      font-size: 14px;
    }}
    .fixed-meta strong {{ color: var(--text); font-weight: 600; }}
    form {{ margin: 16px 0 0; }}
    .question {{
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px 20px;
      margin-bottom: 12px;
      border-left: 4px solid transparent;
    }}
    .question:focus-within {{ border-left-color: var(--forms-green); }}
    label {{ display: block; font-weight: 600; margin-bottom: 8px; }}
    .required {{ color: var(--error); }}
    .hint {{ color: var(--muted); font-size: 13px; margin: -2px 0 10px; }}
    input, textarea {{
      width: 100%;
      border: 1px solid #aab7c2;
      border-radius: 4px;
      padding: 10px 11px;
      color: var(--text);
      background: #fff;
      font: inherit;
    }}
    textarea {{ min-height: 110px; resize: vertical; }}
    input:focus, textarea:focus {{
      outline: 2px solid rgba(3, 120, 124, 0.25);
      border-color: var(--forms-green);
    }}
    .actions {{
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 18px;
    }}
    button {{
      border: 0;
      border-radius: 4px;
      padding: 10px 18px;
      color: #fff;
      background: var(--forms-green);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    button:hover {{ background: var(--forms-green-dark); }}
    button:disabled {{ background: #8aa7aa; cursor: wait; }}
    button.secondary {{ background: #596b75; }}
    .status {{
      margin-top: 16px;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px 18px;
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
    @media (max-width: 620px) {{
      .shell {{ padding: 14px 10px 32px; }}
      .header, .fixed-meta, .question, .status {{ padding-left: 14px; padding-right: 14px; }}
      h1 {{ font-size: 23px; }}
      .actions button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="header">
      <h1>{title}</h1>
      <p class="subtitle">Complete every required question and submit it into the grievance intake workflow.</p>
    </header>
    <div class="fixed-meta" aria-label="Fixed submission values">
      <span><strong>Document command</strong> {command}</span>
      <span><strong>Contract</strong> CWA</span>
      <span><strong>Narrative</strong> Non-discipline grievance brief</span>
    </div>

    <form id="nonDisciplineForm">
      <div id="questions"></div>
      <div class="actions">
        <button id="submitBtn" type="submit">Submit</button>
        <button class="secondary" id="resetBtn" type="button">Clear form</button>
        <span id="savingText" class="hint hidden">Submitting.</span>
      </div>
    </form>

    <section id="statusPanel" class="status hidden" aria-live="polite">
      <h2 id="statusTitle">Ready</h2>
      <div id="statusMessage"></div>
      <pre id="statusDetails"></pre>
    </section>
  </main>

  <script>
    const FORM_ENDPOINT = '/internal/forms/non-discipline-brief/submissions';
    const fields = [
      {{ name: 'grievant_firstname', label: 'Grievant first name', required: true, type: 'text' }},
      {{ name: 'grievant_lastname', label: 'Grievant last name', required: true, type: 'text' }},
      {{ name: 'grievant_email', label: 'Grievant email', required: true, type: 'email' }},
      {{ name: 'local_number', label: 'Local number', required: true, type: 'text', placeholder: '3106' }},
      {{ name: 'local_grievance_number', label: 'Local grievance number', required: false, type: 'text' }},
      {{ name: 'location', label: 'Location', required: true, type: 'text', placeholder: 'Jacksonville, FL' }},
      {{ name: 'grievant_or_work_group', label: 'Grievant(s) or work group', required: true, type: 'text' }},
      {{ name: 'grievant_home_address', label: 'Grievant home address', required: true, type: 'textarea' }},
      {{ name: 'date_grievance_occurred', label: 'Date grievance occurred', required: true, type: 'date' }},
      {{ name: 'date_grievance_filed', label: 'Date grievance filed', required: true, type: 'date' }},
      {{ name: 'date_grievance_appealed_to_executive_level', label: 'Date grievance appealed to executive level', required: false, type: 'date' }},
      {{ name: 'issue_or_condition_involved', label: 'Issue or condition involved', required: true, type: 'textarea', hint: 'Section I' }},
      {{ name: 'action_taken', label: 'Action taken', required: true, type: 'textarea', hint: 'Section II' }},
      {{ name: 'chronology_of_facts', label: 'Chronology of facts pertaining to grievance', required: true, type: 'textarea', hint: 'Section III' }},
      {{ name: 'analysis_of_grievance', label: 'Analysis of grievance', required: true, type: 'textarea', hint: 'Section IV' }},
      {{ name: 'current_status', label: 'Current status of grievant or condition', required: true, type: 'textarea', hint: 'Section V' }},
      {{ name: 'union_position', label: 'Union position', required: true, type: 'textarea', hint: 'Section VI' }},
      {{ name: 'company_position', label: 'Company position', required: true, type: 'textarea', hint: 'Section VII' }},
      {{ name: 'potential_witnesses', label: 'Potential witnesses', required: false, type: 'textarea', hint: 'Section VIII' }},
      {{ name: 'recommendation', label: 'Recommendation', required: true, type: 'textarea', hint: 'Section IX' }},
      {{ name: 'attachment_1', label: 'Attachment 1 label', required: false, type: 'text' }},
      {{ name: 'attachment_2', label: 'Attachment 2 label', required: false, type: 'text' }},
      {{ name: 'attachment_3', label: 'Attachment 3 label', required: false, type: 'text' }},
      {{ name: 'attachment_4', label: 'Attachment 4 label', required: false, type: 'text' }},
      {{ name: 'attachment_5', label: 'Attachment 5 label', required: false, type: 'text' }},
      {{ name: 'attachment_6', label: 'Attachment 6 label', required: false, type: 'text' }},
      {{ name: 'attachment_7', label: 'Attachment 7 label', required: false, type: 'text' }},
      {{ name: 'attachment_8', label: 'Attachment 8 label', required: false, type: 'text' }},
      {{ name: 'attachment_9', label: 'Attachment 9 label', required: false, type: 'text' }},
      {{ name: 'attachment_10', label: 'Attachment 10 label', required: false, type: 'text' }},
      {{ name: 'signer_email', label: 'Signer email override', required: false, type: 'email', hint: 'Leave blank to use the grievant email fallback.' }}
    ];

    const form = document.getElementById('nonDisciplineForm');
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
      return `forms-internal-non-discipline-${{random}}`;
    }}

    function renderQuestions() {{
      questions.innerHTML = fields.map((field) => {{
        const id = `field-${{field.name}}`;
        const required = field.required ? ' required aria-required="true"' : '';
        const label = `${{esc(field.label)}}${{field.required ? ' <span class="required">*</span>' : ''}}`;
        const hint = field.hint ? `<p class="hint">${{esc(field.hint)}}</p>` : '';
        const placeholder = field.placeholder ? ` placeholder="${{esc(field.placeholder)}}"` : '';
        const control = field.type === 'textarea'
          ? `<textarea id="${{id}}" name="${{esc(field.name)}}"${{required}}${{placeholder}}></textarea>`
          : `<input id="${{id}}" name="${{esc(field.name)}}" type="${{esc(field.type || 'text')}}"${{required}}${{placeholder}} />`;
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

    async function submitForm(event) {{
      event.preventDefault();
      if (!form.reportValidity()) return;
      submitBtn.disabled = true;
      savingText.classList.remove('hidden');
      setStatus('', 'Submitting', 'The intake request is being sent.', null);
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
        const intake = data && data.intake_response ? data.intake_response : data;
        const caseId = intake && intake.case_id ? intake.case_id : '';
        const grievanceId = intake && intake.grievance_id ? intake.grievance_id : '';
        setStatus('success', 'Submitted', `Case ${{caseId}} was submitted. Grievance ${{grievanceId}}.`, data);
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


@router.get("/internal/forms/non-discipline-brief", response_class=HTMLResponse)
async def non_discipline_internal_form_page(request: Request):
    gate = await require_officer_page_access(request, next_path="/internal/forms/non-discipline-brief")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(_render_non_discipline_form_page())


@router.post("/internal/forms/non-discipline-brief/submissions")
async def submit_non_discipline_internal_form(
    body: NonDisciplineInternalFormSubmission,
    request: Request,
):
    await require_authenticated_officer(request)
    payload = _build_non_discipline_intake_payload(body)
    intake_response = await _post_internal_json(
        cfg=request.app.state.cfg,
        url=_INTERNAL_INTAKE_URL,
        payload=payload,
    )
    return {
        "request_id": payload["request_id"],
        "document_command": _NON_DISCIPLINE_COMMAND,
        "intake_response": intake_response,
    }
