from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..web.officer_auth import require_ops_page_access
from . import database, exporter, scanner, sharepoint_sync

router = APIRouter()


async def _require_dues_forms_access(request: Request, *, next_path: str):
    gate = await require_ops_page_access(request, next_path=next_path)
    if isinstance(gate, RedirectResponse):
        return gate
    return None


def _template(name: str) -> str:
    return (Path(__file__).with_name("templates") / name).read_text(encoding="utf-8")


def _status_label(status: object) -> str:
    return str(status or "").replace("_", " ").title()


def _status_options(selected: str | None) -> str:
    options = ['<option value="">All statuses</option>']
    for status in database.ALLOWED_REVIEW_STATUSES:
        selected_attr = " selected" if selected == status else ""
        options.append(
            f'<option value="{escape(status, quote=True)}"{selected_attr}>{escape(_status_label(status))}</option>'
        )
    return "\n".join(options)


def _row_html(record: dict) -> str:  # noqa: ANN001
    status = escape(str(record.get("review_status") or ""), quote=True)
    name = " ".join(
        part for part in (str(record.get("first_name") or "").strip(), str(record.get("last_name") or "").strip()) if part
    )
    error = str(record.get("error_message") or "").strip()
    return f"""
      <tr>
        <td><a href="/dues-forms/{int(record["id"])}">{int(record["id"])}</a></td>
        <td>{escape(str(record.get("processed_at") or ""))}</td>
        <td><span class="status status-{status}">{escape(_status_label(status))}</span></td>
        <td>{escape(name)}</td>
        <td>{escape(str(record.get("employee_id") or ""))}</td>
        <td>{escape(str(record.get("local_no") or ""))}</td>
        <td>{escape(str(record.get("source_filename") or ""))}</td>
        <td>{escape(str(record.get("extraction_method") or ""))}</td>
        <td>{escape(error)}</td>
      </tr>
    """


def _scan_message_html(scan_result: str | None) -> str:
    if not scan_result:
        return ""
    return f'<section class="panel notice">{escape(scan_result)}</section>'


def _ignored_row_html(record: dict) -> str:  # noqa: ANN001
    return f"""
      <tr>
        <td>{escape(str(record.get("ignored_at") or ""))}</td>
        <td>{escape(str(record.get("source_filename") or ""))}</td>
        <td>{escape(str(record.get("ignored_reason") or ""))}</td>
        <td>{escape(str(record.get("source_path") or ""))}</td>
        <td class="hash">{escape(str(record.get("source_sha256") or ""))}</td>
      </tr>
    """


def _render_ignored(records: list[dict]) -> str:
    rows = "\n".join(_ignored_row_html(record) for record in records)
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No ignored files found.</td></tr>'
    return (
        _template("dues_forms_ignored.html")
        .replace("{{rows}}", rows)
        .replace("{{total_count}}", str(len(records)))
    )


def _render_list(records: list[dict], *, selected_status: str | None, scan_result: str | None = None) -> str:
    rows = "\n".join(_row_html(record) for record in records)
    if not rows:
        rows = '<tr><td colspan="9" class="empty">No dues forms found.</td></tr>'
    return (
        _template("dues_forms_list.html")
        .replace("{{scan_message}}", _scan_message_html(scan_result))
        .replace("{{status_options}}", _status_options(selected_status))
        .replace("{{rows}}", rows)
        .replace("{{total_count}}", str(len(records)))
    )


def _detail_item(label: str, value: object) -> str:
    return f"""
      <div class="field">
        <dt>{escape(label)}</dt>
        <dd>{escape(str(value or ""))}</dd>
      </div>
    """


def _render_detail(record: dict) -> str:  # noqa: ANN001
    status_options = "\n".join(
        f'<option value="{escape(status, quote=True)}"{" selected" if record.get("review_status") == status else ""}>'
        f"{escape(_status_label(status))}</option>"
        for status in database.ALLOWED_REVIEW_STATUSES
    )
    detail_fields = [
        ("Source filename", record.get("source_filename")),
        ("Source path", record.get("source_path")),
        ("Source SHA256", record.get("source_sha256")),
        ("Processed at", record.get("processed_at")),
        ("Extraction method", record.get("extraction_method")),
        ("Error message", record.get("error_message")),
        ("Form type", record.get("form_type")),
        ("Contract", record.get("contract")),
        ("First name", record.get("first_name")),
        ("Last name", record.get("last_name")),
        ("Work location address", record.get("work_location_address")),
        ("Work location state", record.get("work_location_state")),
        ("Employee ID", record.get("employee_id")),
        ("Local No", record.get("local_no")),
        ("Home address", record.get("home_address")),
        ("City", record.get("city")),
        ("State", record.get("state")),
        ("ZIP", record.get("zip")),
        ("Personal email", record.get("personal_email")),
        ("Personal cell phone", record.get("personal_cell_phone")),
        ("Timestamp", record.get("timestamp")),
        ("IP address", record.get("ip_address")),
        ("Dues deduction authorization", record.get("dues_deduction_authorization")),
        ("Electronic signature", record.get("electronic_signature")),
    ]
    details = "\n".join(_detail_item(label, value) for label, value in detail_fields)
    return (
        _template("dues_forms_detail.html")
        .replace("{{id}}", str(int(record["id"])))
        .replace("{{review_status}}", escape(_status_label(record.get("review_status"))))
        .replace("{{status_options}}", status_options)
        .replace("{{details}}", details)
        .replace("{{raw_text}}", escape(str(record.get("raw_text") or "")))
    )


@router.get("/dues-forms", response_class=HTMLResponse)
async def dues_forms_list(request: Request, review_status: str | None = None, scan_result: str | None = None):
    redirect = await _require_dues_forms_access(request, next_path="/dues-forms")
    if redirect is not None:
        return redirect
    selected = review_status if review_status in database.ALLOWED_REVIEW_STATUSES else None
    records = database.list_records(review_status=selected)
    return HTMLResponse(_render_list(records, selected_status=selected, scan_result=scan_result))


def _manual_sharepoint_settings() -> sharepoint_sync.SharePointSyncSettings:
    return sharepoint_sync.SharePointSyncSettings.from_env(enabled=True, recursive=True)


def _scan_result_summary(summary: dict) -> str:  # noqa: ANN001
    sharepoint = summary.get("sharepoint") if isinstance(summary.get("sharepoint"), dict) else {}
    downloaded = int(sharepoint.get("downloaded", 0) or 0) if isinstance(sharepoint, dict) else 0
    remote = int(sharepoint.get("remote_files", 0) or 0) if isinstance(sharepoint, dict) else 0
    already_downloaded = int(sharepoint.get("already_downloaded", 0) or 0) if isinstance(sharepoint, dict) else 0
    duplicate_hashes = int(sharepoint.get("duplicate_hashes", 0) or 0) if isinstance(sharepoint, dict) else 0
    skipped_non_pdf = int(sharepoint.get("skipped_non_pdf", 0) or 0) if isinstance(sharepoint, dict) else 0
    download_errors = int(sharepoint.get("download_errors", 0) or 0) if isinstance(sharepoint, dict) else 0
    sharepoint_detail = ""
    if sharepoint:
        sharepoint_detail = (
            f", SharePoint: {downloaded} PDFs downloaded from {remote} remote files"
            f", {already_downloaded} already downloaded"
            f", {duplicate_hashes} duplicate hashes"
            f", {skipped_non_pdf} non-PDF skipped"
            f", {download_errors} download errors"
        )
    return (
        "Manual scan complete: "
        f"{int(summary.get('processed', 0) or 0)} processed, "
        f"{int(summary.get('needs_review', 0) or 0)} needs review, "
        f"{int(summary.get('failed', 0) or 0)} failed, "
        f"{int(summary.get('ignored', 0) or 0)} ignored, "
        f"{int(summary.get('duplicates', 0) or 0)} duplicates"
        + sharepoint_detail
        + "."
    )


@router.post("/dues-forms/run-scan")
async def dues_forms_run_scan(request: Request):
    redirect = await _require_dues_forms_access(request, next_path="/dues-forms")
    if redirect is not None:
        return redirect
    try:
        summary = await asyncio.to_thread(
            scanner.scan_once,
            stable_delay_seconds=0,
            sharepoint_settings=_manual_sharepoint_settings(),
        )
        message = _scan_result_summary(summary)
    except Exception as exc:
        message = f"Manual scan failed: {exc}"
    return RedirectResponse(url=f"/dues-forms?{urlencode({'scan_result': message})}", status_code=303)


@router.get("/dues-forms/export.csv")
async def dues_forms_export_csv(request: Request):
    redirect = await _require_dues_forms_access(request, next_path="/dues-forms")
    if redirect is not None:
        return redirect
    paths = exporter.regenerate_exports()
    return FileResponse(
        paths["csv"],
        media_type="text/csv; charset=utf-8",
        filename="dues_deduction_forms.csv",
    )


@router.get("/dues-forms/export.xlsx")
async def dues_forms_export_xlsx(request: Request):
    redirect = await _require_dues_forms_access(request, next_path="/dues-forms")
    if redirect is not None:
        return redirect
    paths = exporter.regenerate_exports()
    if paths["xlsx"] is None:
        raise HTTPException(status_code=404, detail="XLSX export is not available because openpyxl is not installed")
    return FileResponse(
        paths["xlsx"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="dues_deduction_forms.xlsx",
    )


@router.get("/dues-forms/ignored", response_class=HTMLResponse)
async def dues_forms_ignored(request: Request):
    redirect = await _require_dues_forms_access(request, next_path="/dues-forms/ignored")
    if redirect is not None:
        return redirect
    records = database.list_ignored_files()
    return HTMLResponse(_render_ignored(records))


@router.get("/dues-forms/{record_id}", response_class=HTMLResponse)
async def dues_forms_detail(record_id: int, request: Request):
    redirect = await _require_dues_forms_access(request, next_path=f"/dues-forms/{record_id}")
    if redirect is not None:
        return redirect
    record = database.get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="dues form not found")
    return HTMLResponse(_render_detail(record))


@router.post("/dues-forms/{record_id}/status")
async def dues_forms_update_status(record_id: int, request: Request):
    redirect = await _require_dues_forms_access(request, next_path=f"/dues-forms/{record_id}")
    if redirect is not None:
        return redirect
    body = (await request.body()).decode("utf-8", errors="replace")
    params = parse_qs(body)
    review_status = (params.get("review_status") or params.get("status") or [""])[0]
    if review_status not in database.ALLOWED_REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="invalid review_status")
    try:
        database.update_review_status(record_id, review_status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="dues form not found") from exc
    exporter.regenerate_exports()
    return RedirectResponse(url=f"/dues-forms/{record_id}", status_code=303)
