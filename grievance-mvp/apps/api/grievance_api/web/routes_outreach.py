from __future__ import annotations

import html

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from ..services.outreach_service import OutreachRenderedMessage, OutreachService
from .officer_auth import require_admin_user, require_ops_page_access
from .outreach_models import (
    OutreachContactListResponse,
    OutreachContactRow,
    OutreachContactUpsertRequest,
    OutreachImportInspectRequest,
    OutreachImportInspectResponse,
    OutreachImportRequest,
    OutreachImportResponse,
    OutreachPageBootstrap,
    OutreachPreviewRequest,
    OutreachPreviewResponse,
    OutreachQuickMessageRequest,
    OutreachRunDueResponse,
    OutreachSendLogListResponse,
    OutreachSendLogRow,
    OutreachOneOffSendRequest,
    OutreachSendResult,
    OutreachStopListResponse,
    OutreachStopRow,
    OutreachStopUpsertRequest,
    OutreachSummaryResponse,
    OutreachSuppressionListResponse,
    OutreachSuppressionRow,
    OutreachTemplateListResponse,
    OutreachTemplateRow,
    OutreachTemplateUpsertRequest,
    OutreachTestSendRequest,
    OutreachUnsubscribeResult,
    OutreachSendReadiness,
)

router = APIRouter()

_OUTREACH_UI_PAGES: dict[str, dict[str, str]] = {
    "overview": {
        "label": "Overview",
        "section": "home",
        "title": "Outreach Overview",
        "description": "Start here for the high-level picture. Check send readiness, refresh outreach data, and run due sends without being distracted by forms from other workflows.",
    },
    "compose": {
        "label": "Compose",
        "section": "compose",
        "title": "Outreach Compose",
        "description": "This page is only for message work: quick tests, template previews, test sends, and one-off live emails. Nothing else is mixed into the screen.",
    },
    "contacts": {
        "label": "Contacts",
        "section": "contacts",
        "title": "Outreach Contacts",
        "description": "Manage the contact list, import CSV/XLSX files with guided mapping, and review status-bucket counts in one place.",
    },
    "templates": {
        "label": "Templates",
        "section": "templates",
        "title": "Outreach Templates",
        "description": "Edit reusable notice and reminder templates here without contact, stop, or analytics panels competing for attention.",
    },
    "stops": {
        "label": "Stops",
        "section": "stops",
        "title": "Outreach Stops",
        "description": "Plan outreach stops, schedule notice and reminder sends, and target by location, work group, or status bucket on a dedicated page.",
    },
    "analytics": {
        "label": "Analytics",
        "section": "analytics",
        "title": "Outreach Analytics",
        "description": "Review results, export outreach reports, and inspect recent activity without compose or import controls in the way.",
    },
    "history": {
        "label": "History",
        "section": "history",
        "title": "Outreach History",
        "description": "See suppression records and send history on a separate page so follow-up review is simpler and easier to scan.",
    },
}


def _service(request: Request) -> OutreachService:
    service = getattr(request.app.state, "outreach", None)
    if service is None:
        raise HTTPException(status_code=503, detail="outreach service is unavailable")
    return service


def _handle_runtime_error(exc: RuntimeError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "not enabled" in message:
        return HTTPException(status_code=503, detail=message)
    return HTTPException(status_code=400, detail=message)


def _request_client_ip(request: Request) -> str | None:
    for header_name in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        raw = str(request.headers.get(header_name, "")).strip()
        if raw:
            return raw.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return str(request.client.host)
    return None


def _request_purpose(request: Request) -> str | None:
    for header_name in ("purpose", "x-purpose", "sec-purpose", "x-moz"):
        raw = str(request.headers.get(header_name, "")).strip()
        if raw:
            return raw
    return None


def _outreach_page_config(page_name: str) -> dict[str, str]:
    page = _OUTREACH_UI_PAGES.get(str(page_name or "").strip().lower())
    if page is None:
        raise HTTPException(status_code=404, detail="outreach page not found")
    return page


def _outreach_nav_links(current_page: str) -> str:
    links: list[str] = []
    for page_key, page in _OUTREACH_UI_PAGES.items():
        active_class = " active" if page_key == current_page else ""
        links.append(
            f'<a class="nav-link{active_class}" href="/officers/outreach/ui/{page_key}">{html.escape(page["label"])}</a>'
        )
    return "".join(links)


@router.get("/officers/outreach", response_class=HTMLResponse)
async def outreach_page(request: Request):
    gate = await require_ops_page_access(request, next_path="/officers/outreach/ui/overview")
    if isinstance(gate, RedirectResponse):
        return gate
    return RedirectResponse(url="/officers/outreach/ui/overview", status_code=303)


@router.get("/officers/outreach/ui/{page_name}", response_class=HTMLResponse)
async def outreach_ui_page(page_name: str, request: Request):
    normalized_page = str(page_name or "").strip().lower()
    page = _OUTREACH_UI_PAGES.get(normalized_page)
    if page is None:
        raise HTTPException(status_code=404, detail="outreach page not found")
    gate = await require_ops_page_access(request, next_path=f"/officers/outreach/ui/{normalized_page}")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(
        _render_outreach_page(page_name=normalized_page),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, private",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/officers/outreach/bootstrap", response_model=OutreachPageBootstrap)
async def outreach_bootstrap(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    contacts = await service.list_contacts()
    templates = await service.list_templates()
    stops = await service.list_stops()
    suppressions = await service.list_suppressions()
    send_log = await service.list_send_log()
    summary = await service.summary()
    return OutreachPageBootstrap(
        contacts=OutreachContactListResponse(rows=[OutreachContactRow(**row) for row in contacts], count=len(contacts)),
        templates=OutreachTemplateListResponse(rows=[OutreachTemplateRow(**row) for row in templates], count=len(templates)),
        stops=OutreachStopListResponse(rows=[OutreachStopRow(**row) for row in stops], count=len(stops)),
        suppressions=OutreachSuppressionListResponse(
            rows=[OutreachSuppressionRow(**row) for row in suppressions],
            count=len(suppressions),
        ),
        send_log=OutreachSendLogListResponse(rows=[OutreachSendLogRow(**row) for row in send_log], count=len(send_log)),
        summary=OutreachSummaryResponse(**summary),
        send_readiness=OutreachSendReadiness(**service.send_readiness()),
        placeholder_catalog=service.placeholder_catalog(),
    )


@router.get("/officers/outreach/contacts", response_model=OutreachContactListResponse)
async def outreach_contacts(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    rows = [OutreachContactRow(**row) for row in await service.list_contacts()]
    return OutreachContactListResponse(rows=rows, count=len(rows))


@router.post("/officers/outreach/contacts", response_model=OutreachContactRow)
async def outreach_create_contact(body: OutreachContactUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_contact(contact_id=None, payload=body.model_dump(exclude_none=True))
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachContactRow(**row)


@router.patch("/officers/outreach/contacts/{contact_id}", response_model=OutreachContactRow)
async def outreach_update_contact(contact_id: int, body: OutreachContactUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_contact(contact_id=contact_id, payload=body.model_dump(exclude_none=True))
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachContactRow(**row)


@router.delete("/officers/outreach/contacts/{contact_id}")
async def outreach_delete_contact(contact_id: int, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    await _service(request).delete_contact(contact_id)
    return {"deleted": True, "contact_id": int(contact_id)}


@router.post("/officers/outreach/contacts/import/inspect", response_model=OutreachImportInspectResponse)
async def outreach_inspect_contact_import(body: OutreachImportInspectRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        result = await service.inspect_contacts_import(
            filename=body.filename,
            content_base64=body.content_base64,
            sheet_name=body.sheet_name,
            mapping=body.mapping.model_dump(exclude_none=True) if body.mapping is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachImportInspectResponse(**result)


@router.post("/officers/outreach/contacts/import", response_model=OutreachImportResponse)
async def outreach_import_contacts(body: OutreachImportRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        result = await service.import_contacts(
            filename=body.filename,
            content_base64=body.content_base64,
            sheet_name=body.sheet_name,
            mapping=body.mapping.model_dump(exclude_none=True) if body.mapping is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachImportResponse(**result)


@router.get("/officers/outreach/templates", response_model=OutreachTemplateListResponse)
async def outreach_templates(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    rows = [OutreachTemplateRow(**row) for row in await service.list_templates()]
    return OutreachTemplateListResponse(rows=rows, count=len(rows))


@router.post("/officers/outreach/templates", response_model=OutreachTemplateRow)
async def outreach_create_template(body: OutreachTemplateUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_template(template_id=None, payload=body.model_dump(exclude_none=True))
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachTemplateRow(**row)


@router.patch("/officers/outreach/templates/{template_id}", response_model=OutreachTemplateRow)
async def outreach_update_template(template_id: int, body: OutreachTemplateUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_template(template_id=template_id, payload=body.model_dump(exclude_none=True))
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachTemplateRow(**row)


@router.delete("/officers/outreach/templates/{template_id}")
async def outreach_delete_template(template_id: int, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    await _service(request).delete_template(template_id)
    return {"deleted": True, "template_id": int(template_id)}


@router.get("/officers/outreach/stops", response_model=OutreachStopListResponse)
async def outreach_stops(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    rows = [OutreachStopRow(**row) for row in await service.list_stops()]
    return OutreachStopListResponse(rows=rows, count=len(rows))


@router.post("/officers/outreach/stops", response_model=OutreachStopRow)
async def outreach_create_stop(body: OutreachStopUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_stop(stop_id=None, payload=body.model_dump(exclude_none=True))
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachStopRow(**row)


@router.patch("/officers/outreach/stops/{stop_id}", response_model=OutreachStopRow)
async def outreach_update_stop(stop_id: int, body: OutreachStopUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_stop(stop_id=stop_id, payload=body.model_dump(exclude_none=True))
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachStopRow(**row)


@router.delete("/officers/outreach/stops/{stop_id}")
async def outreach_delete_stop(stop_id: int, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    await _service(request).delete_stop(stop_id)
    return {"deleted": True, "stop_id": int(stop_id)}


@router.post("/officers/outreach/preview", response_model=OutreachPreviewResponse)
async def outreach_preview(body: OutreachPreviewRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        if body.manual_contact is not None or (body.recipient_email and body.contact_id is None):
            preview = await service.preview_one_off(
                template_id=body.template_id,
                stop_id=body.stop_id,
                contact_id=body.contact_id,
                recipient_email=body.recipient_email or "",
                manual_contact=body.manual_contact.model_dump() if body.manual_contact is not None else None,
            )
        else:
            preview = await service.preview(
                template_id=body.template_id,
                stop_id=body.stop_id,
                contact_id=body.contact_id,
                recipient_email=body.recipient_email,
            )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachPreviewResponse(
        subject=preview.subject,
        text_body=preview.text_body,
        html_body=preview.html_body,
        missing_fields=preview.unknown_placeholders,
        placeholder_catalog=service.placeholder_catalog(),
    )


@router.post("/officers/outreach/test-send", response_model=OutreachSendResult)
async def outreach_test_send(body: OutreachTestSendRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        if body.manual_contact is not None:
            result = await service.send_test_one_off(
                template_id=body.template_id,
                stop_id=body.stop_id,
                contact_id=body.contact_id,
                recipient_email=body.recipient_email,
                manual_contact=body.manual_contact.model_dump(exclude_none=True),
            )
        else:
            result = await service.send_test(
                template_id=body.template_id,
                stop_id=body.stop_id,
                contact_id=body.contact_id,
                recipient_email=body.recipient_email,
            )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachSendResult(
        send_log_id=result.send_log_id,
        recipient_email=result.recipient_email,
        status=result.status,
        graph_message_id=result.graph_message_id,
        error_text=result.error_text,
    )


@router.post("/officers/outreach/one-off-send", response_model=OutreachSendResult)
async def outreach_one_off_send(body: OutreachOneOffSendRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        result = await service.send_one_off(
            template_id=body.template_id,
            stop_id=body.stop_id,
            contact_id=body.contact_id,
            recipient_email=body.recipient_email,
            manual_contact=body.manual_contact.model_dump(exclude_none=True) if body.manual_contact is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachSendResult(
        send_log_id=result.send_log_id,
        recipient_email=result.recipient_email,
        status=result.status,
        graph_message_id=result.graph_message_id,
        error_text=result.error_text,
    )


@router.post("/officers/outreach/quick-preview", response_model=OutreachPreviewResponse)
async def outreach_quick_preview(body: OutreachQuickMessageRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        preview = await service.preview_quick_message(
            stop_id=body.stop_id,
            recipient_email=body.recipient_email,
            subject_template=body.subject_template,
            body_template=body.body_template,
            contact_id=body.contact_id,
            manual_contact=body.manual_contact.model_dump(exclude_none=True) if body.manual_contact is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachPreviewResponse(
        subject=preview.subject,
        text_body=preview.text_body,
        html_body=preview.html_body,
        missing_fields=preview.unknown_placeholders,
        placeholder_catalog=service.placeholder_catalog(),
    )


@router.post("/officers/outreach/quick-test-send", response_model=OutreachSendResult)
async def outreach_quick_test_send(body: OutreachQuickMessageRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        result = await service.send_test_quick_message(
            stop_id=body.stop_id,
            recipient_email=body.recipient_email,
            subject_template=body.subject_template,
            body_template=body.body_template,
            contact_id=body.contact_id,
            manual_contact=body.manual_contact.model_dump(exclude_none=True) if body.manual_contact is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachSendResult(
        send_log_id=result.send_log_id,
        recipient_email=result.recipient_email,
        status=result.status,
        graph_message_id=result.graph_message_id,
        error_text=result.error_text,
    )


@router.post("/officers/outreach/run-due", response_model=OutreachRunDueResponse)
async def outreach_run_due(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        result = await service.run_due()
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachRunDueResponse(
        processed_count=result["processed_count"],
        sent_count=result["sent_count"],
        failed_count=result["failed_count"],
        skipped_suppressed_count=result["skipped_suppressed_count"],
        skipped_existing_count=result["skipped_existing_count"],
        rows=[OutreachSendResult(**row) for row in result["rows"]],
    )


@router.get("/officers/outreach/send-log", response_model=OutreachSendLogListResponse)
async def outreach_send_log(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    rows = await service.list_send_log()
    return OutreachSendLogListResponse(rows=[OutreachSendLogRow(**row) for row in rows], count=len(rows))


@router.get("/officers/outreach/suppressions", response_model=OutreachSuppressionListResponse)
async def outreach_suppressions(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    rows = [OutreachSuppressionRow(**row) for row in await service.list_suppressions()]
    return OutreachSuppressionListResponse(rows=rows, count=len(rows))


@router.delete("/officers/outreach/suppressions/{suppression_id}")
async def outreach_delete_suppression(suppression_id: int, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    await _service(request).delete_suppression(suppression_id)
    return {"deleted": True, "suppression_id": int(suppression_id)}


@router.get("/officers/outreach/analytics")
async def outreach_analytics(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    stop_id: int | None = None,
    location: str | None = None,
    template_id: int | None = None,
    recipient_email: str | None = None,
    work_group: str | None = None,
):
    await require_admin_user(request, allow_local_fallback=True)
    return await _service(request).analytics_dashboard(
        date_from=date_from,
        date_to=date_to,
        stop_id=stop_id,
        location=location,
        template_id=template_id,
        recipient_email=recipient_email,
        work_group=work_group,
    )


@router.get("/officers/outreach/analytics/export/summary.csv")
async def outreach_export_campaign_summary(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    stop_id: int | None = None,
    location: str | None = None,
    template_id: int | None = None,
    recipient_email: str | None = None,
    work_group: str | None = None,
):
    await require_admin_user(request, allow_local_fallback=True)
    csv_text = await _service(request).export_campaign_summary_csv(
        date_from=date_from,
        date_to=date_to,
        stop_id=stop_id,
        location=location,
        template_id=template_id,
        recipient_email=recipient_email,
        work_group=work_group,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=outreach-campaign-summary.csv"},
    )


@router.get("/officers/outreach/analytics/export/clicks.csv")
async def outreach_export_click_activity(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    stop_id: int | None = None,
    location: str | None = None,
    template_id: int | None = None,
    recipient_email: str | None = None,
    work_group: str | None = None,
):
    await require_admin_user(request, allow_local_fallback=True)
    csv_text = await _service(request).export_click_activity_csv(
        date_from=date_from,
        date_to=date_to,
        stop_id=stop_id,
        location=location,
        template_id=template_id,
        recipient_email=recipient_email,
        work_group=work_group,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=outreach-click-activity.csv"},
    )


@router.get("/officers/outreach/analytics/export/send-history.csv")
async def outreach_export_send_history(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    stop_id: int | None = None,
    location: str | None = None,
    template_id: int | None = None,
    recipient_email: str | None = None,
    work_group: str | None = None,
):
    await require_admin_user(request, allow_local_fallback=True)
    csv_text = await _service(request).export_send_history_csv(
        date_from=date_from,
        date_to=date_to,
        stop_id=stop_id,
        location=location,
        template_id=template_id,
        recipient_email=recipient_email,
        work_group=work_group,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=outreach-send-history.csv"},
    )


@router.get("/officers/outreach/analytics/export/suppressions.csv")
async def outreach_export_suppressions(request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    csv_text = await _service(request).export_suppressions_csv()
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=outreach-suppressions.csv"},
    )


@router.get("/r/{token}")
async def outreach_tracked_redirect(token: str, request: Request):
    destination_url = await _service(request).record_click(
        token,
        client_ip=_request_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        purpose=_request_purpose(request),
    )
    if not destination_url:
        raise HTTPException(status_code=404, detail="tracked link not found")
    return RedirectResponse(destination_url, status_code=307)


@router.get("/o/{token}.gif")
async def outreach_open_pixel(token: str, request: Request):
    await _service(request).record_open(
        token,
        client_ip=_request_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        purpose=_request_purpose(request),
    )
    return Response(
        content=_service(request).tracking_pixel_bytes(),
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, private",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/unsubscribe/{token}", response_model=OutreachUnsubscribeResult, response_class=HTMLResponse)
async def outreach_unsubscribe_get(token: str, request: Request):
    service = _service(request)
    try:
        result = await service.unsubscribe(token)
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return HTMLResponse(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Unsubscribed</title>
  <style>
    body {{ font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif; margin: 0; background: linear-gradient(180deg, #f8fbfd 0%, #eef4f8 100%); color: #1f2937; }}
    .wrap {{ max-width: 640px; margin: 64px auto; padding: 0 16px; }}
    .card {{ background: white; border: 1px solid #d9e3ea; border-radius: 18px; padding: 28px; box-shadow: 0 18px 40px rgba(15, 23, 42, 0.07); }}
    h1 {{ margin-top: 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>You have been unsubscribed</h1>
      <p><strong>{result["email"]}</strong> has been added to the suppression list.</p>
      <p>You should not receive future outreach emails from this module unless an administrator removes the suppression.</p>
    </div>
  </div>
</body>
</html>"""
    )


@router.post("/unsubscribe/{token}", response_class=PlainTextResponse)
async def outreach_unsubscribe_post(token: str, request: Request):
    service = _service(request)
    try:
        result = await service.unsubscribe(token)
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return PlainTextResponse(f"Unsubscribed {result['email']}")


def _render_outreach_page(*, page_name: str) -> str:
    page = _outreach_page_config(page_name)
    current_section = page["section"]
    rendered = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__OUTREACH_PAGE_TITLE__</title>
  <style>
    :root {
      --ink: #1f2937;
      --muted: #526272;
      --accent: #0b6e75;
      --accent-dark: #064e53;
      --card: rgba(255,255,255,0.95);
      --border: #d8e2e8;
      --bg: #edf3f7;
      --danger: #a11d2d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      color: var(--ink);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(11, 110, 117, 0.16), transparent 20%),
        linear-gradient(180deg, #f8fbfd 0%, var(--bg) 100%);
    }
    .shell { max-width: 1600px; margin: 0 auto; }
    .hero {
      background: linear-gradient(180deg, #0b6e75 0%, var(--accent-dark) 100%);
      color: white;
      border-radius: 18px;
      padding: 24px 26px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.09);
      margin-bottom: 18px;
    }
    .hero h1 { margin: 0 0 10px; font-size: 36px; }
    .hero p { margin: 0; max-width: 76ch; line-height: 1.5; }
    .nav-tabs {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 18px 0;
    }
    .nav-link {
      display: inline-block;
      width: auto;
      min-width: 120px;
      background: white;
      color: var(--ink);
      border-color: var(--border);
      text-decoration: none;
      text-align: center;
      border: 1px solid #c7d5df;
      border-radius: 10px;
      padding: 10px 12px;
      font-weight: 700;
    }
    .nav-link.active {
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }
    .route-section { display: none; }
    .route-section.active { display: block; }
    .route-section.grid.active { display: grid; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
    }
    .panel {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 14px 30px rgba(15, 23, 42, 0.06);
      margin-bottom: 16px;
    }
    .panel h2 { margin-top: 0; font-size: 21px; }
    .panel h3 { margin: 0 0 10px; font-size: 17px; }
    .summary,
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
    }
    .stat, .mini-stat {
      background: white;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
    }
    .stat strong, .mini-stat strong { display: block; font-size: 28px; margin-bottom: 4px; }
    .mini-stat strong { font-size: 22px; }
    .muted { color: var(--muted); }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
    }
    .quick-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
    }
    .mapping-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 10px;
    }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: end;
      margin-bottom: 12px;
    }
    .toolbar > * { flex: 1 1 180px; }
    label { display: block; font-size: 14px; font-weight: 600; margin-bottom: 4px; }
    input, select, textarea, button {
      width: 100%;
      font: inherit;
      border-radius: 10px;
      border: 1px solid #c7d5df;
      padding: 10px 12px;
      background: white;
    }
    textarea { min-height: 110px; resize: vertical; }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary { background: #6b7d8d; border-color: #6b7d8d; }
    button.danger { background: var(--danger); border-color: var(--danger); }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
      align-items: center;
    }
    .actions button { width: auto; min-width: 140px; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 820px; background: white; }
    th, td { border: 1px solid #d7e1e7; padding: 9px 10px; text-align: left; vertical-align: top; }
    th { background: #eef4f7; }
    tr:hover td { background: #f8fbfd; }
    .note {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      background: #eef6f7;
      color: #26434d;
      font-size: 14px;
      line-height: 1.45;
    }
    .note[data-tone="error"] {
      background: #fde8ea;
      color: #8f1d2c;
    }
    .note[data-tone="success"] {
      background: #e8f6ee;
      color: #146c43;
    }
    .note[data-tone="warning"] {
      background: #fff3d6;
      color: #8a5a00;
    }
    .note strong { display: inline-block; margin-right: 6px; }
    .subtle-card {
      background: rgba(238, 244, 247, 0.8);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      margin-top: 12px;
    }
    .subtle-card h3 { margin-bottom: 8px; }
    .pre {
      white-space: pre-wrap;
      background: #0f1720;
      color: #e6edf3;
      border-radius: 12px;
      padding: 14px;
      min-height: 180px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 13px;
      overflow: auto;
    }
    .preview-frame {
      width: 100%;
      min-height: 280px;
      border: 1px solid #d7e1e7;
      border-radius: 12px;
      background: white;
      margin-top: 12px;
    }
    .pill {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 9px;
      background: #e7f2f3;
      font-size: 12px;
      font-weight: 700;
      color: #0c5960;
      white-space: nowrap;
    }
    .pill.inactive {
      background: #f1e8e9;
      color: #8f1d2c;
    }
    .stack {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .inline-meta {
      font-size: 13px;
      color: var(--muted);
    }
    .error { color: var(--danger); }
    .hidden { display: none !important; }
    @media (max-width: 720px) {
      body { padding: 14px; }
      .hero h1 { font-size: 28px; }
      .actions button { width: 100%; }
      .nav-link { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <h1>__OUTREACH_PAGE_HEADING__</h1>
      <p>__OUTREACH_PAGE_DESCRIPTION__</p>
      <div class="note" id="sendReadinessNote" data-tone="warning">Checking send readiness…</div>
    </section>

    <section class="nav-tabs panel">
      __OUTREACH_NAV_LINKS__
    </section>

    <section class="panel route-section" data-section="home">
      <h2>Summary</h2>
      <div class="summary" id="summaryGrid"></div>
      <div class="actions">
        <button id="refreshBtn" type="button">Refresh Data</button>
        <button id="runDueBtn" type="button">Run Due Sends Now</button>
      </div>
      <div class="note" id="runDueNote">Use this to process active notice and reminder sends immediately.</div>
    </section>

    <section class="panel route-section" data-section="analytics">
      <h2>Analytics</h2>
      <div class="form-grid">
        <div><label>Date From<input id="analyticsDateFrom" type="date" /></label></div>
        <div><label>Date To<input id="analyticsDateTo" type="date" /></label></div>
        <div><label>Stop<select id="analyticsStopId"><option value="">All stops</option></select></label></div>
        <div><label>Template<select id="analyticsTemplateId"><option value="">All templates</option></select></label></div>
        <div><label>Location<input id="analyticsLocation" placeholder="All locations" /></label></div>
        <div><label>Work Group<input id="analyticsWorkGroup" placeholder="All work groups" /></label></div>
        <div><label>Recipient<input id="analyticsRecipientEmail" placeholder="member@example.org" /></label></div>
      </div>
      <div class="actions">
        <button id="analyticsApplyBtn" type="button">Apply Analytics Filters</button>
        <button id="analyticsResetBtn" class="secondary" type="button">Reset Analytics Filters</button>
        <button id="analyticsExportSummaryBtn" class="secondary" type="button">Export Summary CSV</button>
        <button id="analyticsExportClicksBtn" class="secondary" type="button">Export Clicks CSV</button>
        <button id="analyticsExportSendsBtn" class="secondary" type="button">Export Send History CSV</button>
        <button id="analyticsExportSuppressionsBtn" class="secondary" type="button">Export Suppressions CSV</button>
      </div>
      <div class="note" id="analyticsNote" data-tone="info">
        Estimated opens are estimates only. Clicks are generally more reliable. Clearly automated or prefetch-style traffic is flagged and excluded from topline metrics when detected.
      </div>
      <div class="summary" id="analyticsGrid"></div>
    </section>

    <section class="grid route-section" data-section="analytics">
      <section class="panel">
        <h2>Campaign Engagement</h2>
        <div class="table-wrap"><table id="analyticsCampaignsTable"></table></div>
      </section>
      <section class="panel">
        <h2>Top Clicked Links</h2>
        <div class="table-wrap"><table id="analyticsTopLinksTable"></table></div>
      </section>
    </section>

    <section class="panel route-section" data-section="analytics">
      <h2>Recent Activity</h2>
      <div class="table-wrap"><table id="analyticsActivityTable"></table></div>
    </section>

    <section class="grid route-section" data-section="contacts">
      <section class="panel">
        <h2>Contact Editor</h2>
        <div class="form-grid">
          <div><label>Email<input id="contactEmail" /></label></div>
          <div><label>First Name<input id="contactFirstName" /></label></div>
          <div><label>Last Name<input id="contactLastName" /></label></div>
          <div><label>Full Name<input id="contactFullName" /></label></div>
          <div><label>Work Location<input id="contactLocation" /></label></div>
          <div><label>Work Group<input id="contactWorkGroup" /></label></div>
          <div><label>Group<input id="contactGroupName" list="knownGroupOptions" /></label></div>
          <div><label>Subgroup<input id="contactSubgroupName" list="knownSubgroupOptions" /></label></div>
          <div><label>Department<input id="contactDepartment" /></label></div>
          <div><label>Bargaining Unit<input id="contactBargainingUnit" /></label></div>
          <div><label>Local Number<input id="contactLocalNumber" /></label></div>
          <div><label>Steward Name<input id="contactStewardName" /></label></div>
          <div><label>Rep Name<input id="contactRepName" /></label></div>
          <div><label>Source<input id="contactSource" placeholder="manual, import:csv, import:sheet" /></label></div>
          <div><label>Active<select id="contactActive"><option value="true">Active</option><option value="false">Inactive</option></select></label></div>
        </div>
        <label style="margin-top:10px;">Notes<textarea id="contactNotes"></textarea></label>
        <label>Extra Fields JSON<textarea id="contactExtraFields" placeholder='{"assigned_officer":"Nick Craig"}'></textarea></label>
        <div class="actions">
          <button id="saveContactBtn" type="button">Save Contact</button>
          <button id="clearContactBtn" class="secondary" type="button">Clear Form</button>
          <button id="deleteContactBtn" class="danger" type="button">Delete Selected</button>
        </div>
        <div class="note" id="contactFormNote">Select a row below to edit an existing contact or save a new one.</div>
      </section>

      <section class="panel">
        <h2>Guided Import</h2>
        <div class="note">Upload a CSV or XLSX, review the detected mapping, preview eligible rows, then commit. Only the four active outreach buckets are imported.</div>
        <div class="form-grid" style="margin-top:12px;">
          <div><label>File<input id="contactImportFile" type="file" accept=".csv,.xlsx" /></label></div>
          <div><label>Worksheet<select id="contactImportSheet"><option value="">Choose a file first</option></select></label></div>
          <div><label>Status Mapping Mode<select id="importStatusMode"><option value="combined">One status column</option><option value="split">Three status columns</option></select></label></div>
        </div>
        <div class="actions">
          <button id="inspectImportBtn" type="button">Inspect File</button>
          <button id="refreshImportPreviewBtn" class="secondary" type="button">Refresh Preview</button>
          <button id="applyImportBtn" type="button">Apply Import</button>
          <button id="resetImportBtn" class="secondary" type="button">Reset Import</button>
        </div>
        <div class="subtle-card">
          <h3>Field Mapping</h3>
          <div class="mapping-grid">
            <div><label>Email<select id="importEmailColumn"></select></label></div>
            <div><label>First Name<select id="importFirstNameColumn"></select></label></div>
            <div><label>Last Name<select id="importLastNameColumn"></select></label></div>
            <div><label>Full Name<select id="importFullNameColumn"></select></label></div>
            <div><label>Work Location<select id="importWorkLocationColumn"></select></label></div>
            <div><label>Work Group<select id="importWorkGroupColumn"></select></label></div>
            <div><label>Group<select id="importGroupNameColumn"></select></label></div>
            <div><label>Subgroup<select id="importSubgroupNameColumn"></select></label></div>
            <div><label>Department<select id="importDepartmentColumn"></select></label></div>
            <div><label>Bargaining Unit<select id="importBargainingUnitColumn"></select></label></div>
            <div><label>Local Number<select id="importLocalNumberColumn"></select></label></div>
            <div><label>Steward Name<select id="importStewardNameColumn"></select></label></div>
            <div><label>Rep Name<select id="importRepNameColumn"></select></label></div>
          </div>
        </div>
        <div class="subtle-card">
          <h3>Status Mapping</h3>
          <div class="mapping-grid">
            <div><label>Combined Status<select id="importCombinedStatusColumn"></select></label></div>
            <div><label>Membership Type<select id="importMembershipTypeColumn"></select></label></div>
            <div><label>Employment Status<select id="importEmploymentStatusColumn"></select></label></div>
            <div><label>Status Detail<select id="importStatusDetailColumn"></select></label></div>
          </div>
        </div>
        <div class="note" id="importResult">Import preview will appear after inspection.</div>
        <div class="summary-grid" id="importPreviewSummary"></div>
        <div class="note" id="importPreviewMeta">Preview counts and ignored-row reasons will appear here.</div>
        <div class="table-wrap"><table id="importSampleTable"></table></div>
      </section>
    </section>

    <section class="panel route-section" data-section="contacts">
      <h2>Contacts Table</h2>
      <div class="toolbar">
        <div>
          <label>Status Bucket Filter
            <select id="contactStatusBucketFilter">
              <option value="">All buckets</option>
              <option value="Member - Active - Active">Member - Active - Active</option>
              <option value="Member - Active - Pending">Member - Active - Pending</option>
              <option value="Non Member - Active - Active">Non Member - Active - Active</option>
              <option value="Non Member - Active - Non fr Mem">Non Member - Active - Non fr Mem</option>
            </select>
          </label>
        </div>
        <div><label>Group Filter<input id="contactGroupFilter" list="knownGroupOptions" placeholder="All groups or comma list" /></label></div>
        <div><label>Subgroup Filter<input id="contactSubgroupFilter" list="knownSubgroupOptions" placeholder="All subgroups or comma list" /></label></div>
      </div>
      <div class="note">Use commas to filter more than one group or subgroup at the same time.</div>
      <div class="summary-grid" id="contactBucketCounts"></div>
      <div class="table-wrap"><table id="contactsTable"></table></div>
    </section>

    <datalist id="knownGroupOptions"></datalist>
    <datalist id="knownSubgroupOptions"></datalist>

    <section class="panel route-section" data-section="templates">
      <h2>Templates</h2>
      <div class="form-grid">
        <div><label>Template Key<input id="templateKey" /></label></div>
        <div><label>Name<input id="templateName" /></label></div>
        <div><label>Type<select id="templateType"><option value="notice">Notice</option><option value="reminder">Reminder</option></select></label></div>
        <div><label>Active<select id="templateActive"><option value="true">Active</option><option value="false">Inactive</option></select></label></div>
      </div>
      <label style="margin-top:10px;">Subject Template<textarea id="templateSubject" style="min-height:90px;"></textarea></label>
      <label>Body Template<textarea id="templateBody" style="min-height:220px;"></textarea></label>
      <div class="actions">
        <button id="saveTemplateBtn" type="button">Save Template</button>
        <button id="clearTemplateBtn" class="secondary" type="button">Clear Form</button>
        <button id="deleteTemplateBtn" class="danger" type="button">Delete Selected</button>
      </div>
      <div class="note" id="templateFormNote">Template saves keep the current compose workflows intact.</div>
      <div class="note" id="placeholderList"></div>
    </section>

    <section class="panel route-section" data-section="templates">
      <h2>Templates Table</h2>
      <div class="table-wrap"><table id="templatesTable"></table></div>
    </section>

    <section class="panel route-section" data-section="stops">
      <h2>Campaign Stops</h2>
      <div class="form-grid">
        <div><label>Location<input id="stopLocationName" /></label></div>
        <div><label>Visit Date<input id="stopVisitDate" type="date" /></label></div>
        <div><label>Start Time<input id="stopStartTime" type="time" /></label></div>
        <div><label>End Time<input id="stopEndTime" type="time" /></label></div>
        <div><label>Timezone<input id="stopTimezone" value="America/New_York" /></label></div>
        <div><label>Status<select id="stopStatus"><option value="draft">Draft</option><option value="active">Active</option><option value="paused">Paused</option><option value="archived">Archived</option></select></label></div>
        <div><label>Audience Location<input id="stopAudienceLocation" /></label></div>
        <div><label>Audience Work Group<input id="stopAudienceWorkGroup" /></label></div>
        <div><label>Audience Group<input id="stopAudienceGroupName" list="knownGroupOptions" placeholder="One or more groups" /></label></div>
        <div><label>Audience Subgroup<input id="stopAudienceSubgroupName" list="knownSubgroupOptions" placeholder="One or more subgroups" /></label></div>
        <div>
          <label>Audience Status Bucket
            <select id="stopAudienceStatusBucket">
              <option value="">All eligible buckets</option>
              <option value="Member - Active - Active">Member - Active - Active</option>
              <option value="Member - Active - Pending">Member - Active - Pending</option>
              <option value="Non Member - Active - Active">Non Member - Active - Active</option>
              <option value="Non Member - Active - Non fr Mem">Non Member - Active - Non fr Mem</option>
            </select>
          </label>
        </div>
        <div><label>Notice Send Local<input id="stopNoticeSendLocal" type="datetime-local" /></label></div>
        <div><label>Reminder Send Local<input id="stopReminderSendLocal" type="datetime-local" /></label></div>
      </div>
      <label style="margin-top:10px;">Notice Subject Override<textarea id="stopNoticeSubject" style="min-height:90px;"></textarea></label>
      <label>Reminder Subject Override<textarea id="stopReminderSubject" style="min-height:90px;"></textarea></label>
      <div class="actions">
        <button id="saveStopBtn" type="button">Save Stop</button>
        <button id="clearStopBtn" class="secondary" type="button">Clear Form</button>
        <button id="deleteStopBtn" class="danger" type="button">Delete Selected</button>
      </div>
      <div class="note" id="stopFormNote">Use audience filters to narrow a stop to a location, work group, group, subgroup, status bucket, or any combination of those. Separate multiple groups or subgroups with commas.</div>
    </section>

    <section class="panel route-section" data-section="stops">
      <h2>Stops Table</h2>
      <div class="table-wrap"><table id="stopsTable"></table></div>
    </section>

    <section class="panel route-section" data-section="compose">
      <h2>Quick Test Message</h2>
      <div class="note">Fastest path for testing: type a subject and message here, preview it, then send a test to the mailbox shown below. If delivery fails, the exact failure text will stay on this panel.</div>
      <div class="form-grid" style="margin-top:12px;">
        <div><label>Stop<select id="quickMessageStopId"></select></label></div>
        <div><label>Recipient Email<input id="quickMessageRecipientEmail" value="ncraig@cwa3106.com" /></label></div>
      </div>
      <label style="margin-top:10px;">Subject<textarea id="quickMessageSubject" style="min-height:90px;">Quick outreach test for {{ location }}</textarea></label>
      <label>Message<textarea id="quickMessageBody" style="min-height:220px;">Hi {{ first_name | default('Nick') }},

This is a quick test message from the outreach mailer for {{ location }} on {{ visit_date }} from {{ visit_time }}.

Thank you,
{{ sender_name }}</textarea></label>
      <div class="actions">
        <button id="quickPreviewBtn" type="button">Preview Quick Test</button>
        <button id="quickSendBtn" type="button">Send Quick Test</button>
      </div>
      <div class="note" id="quickMessageMeta">Quick test preview and send status will appear below.</div>
      <div class="pre" id="quickMessageSubjectBox">Quick test subject preview</div>
      <div class="pre" id="quickMessageBodyBox">Quick test body preview</div>
      <iframe id="quickMessageHtmlFrame" class="preview-frame" title="Quick test rendered HTML preview"></iframe>
    </section>

    <section class="panel route-section" data-section="compose">
      <h2>Preview and Test Send</h2>
      <div class="subtle-card">
        <h3>Saved Contact Sort</h3>
        <div class="form-grid">
          <div>
            <label>Sort Contacts By
              <select id="composeContactSortField">
                <option value="full_name">Name</option>
                <option value="email">Email</option>
                <option value="work_location">Work Location</option>
                <option value="work_group">Work Group</option>
                <option value="group_name">Group</option>
                <option value="subgroup_name">Subgroup</option>
                <option value="department">Department</option>
                <option value="bargaining_unit">Bargaining Unit</option>
                <option value="local_number">Local Number</option>
                <option value="steward_name">Steward Name</option>
                <option value="rep_name">Rep Name</option>
                <option value="status_bucket">Status Bucket</option>
                <option value="source">Source</option>
                <option value="updated_at_utc">Last Updated</option>
              </select>
            </label>
          </div>
          <div>
            <label>Direction
              <select id="composeContactSortDirection">
                <option value="asc">Ascending</option>
                <option value="desc">Descending</option>
              </select>
            </label>
          </div>
          <div>
            <label>Search Contacts
              <input id="composeContactSearch" placeholder="Search name, email, location, group, subgroup, status" />
            </label>
          </div>
          <div><label>Group Filter<input id="composeContactGroupFilter" list="knownGroupOptions" placeholder="All groups or comma list" /></label></div>
          <div><label>Subgroup Filter<input id="composeContactSubgroupFilter" list="knownSubgroupOptions" placeholder="All subgroups or comma list" /></label></div>
        </div>
        <div class="note" id="composeContactSortNote">This controls the saved contact order used by Preview/Test Send and One-Off Send. Use commas to filter more than one group or subgroup.</div>
      </div>
      <div class="form-grid">
        <div><label>Template<select id="previewTemplateId"></select></label></div>
        <div><label>Stop<select id="previewStopId"></select></label></div>
        <div><label>Contact<select id="previewContactId"></select></label></div>
        <div><label>Test Recipient<input id="previewRecipientEmail" value="ncraig@cwa3106.com" /></label></div>
      </div>
      <div class="actions">
        <button id="previewBtn" type="button">Render Preview</button>
        <button id="testSendBtn" type="button">Send Test</button>
      </div>
      <div class="note" id="previewMeta">Rendered preview and missing placeholders will appear below.</div>
      <div class="pre" id="previewSubjectBox">Subject preview</div>
      <div class="pre" id="previewBodyBox">Body preview</div>
      <iframe id="previewHtmlFrame" class="preview-frame" title="Rendered HTML preview"></iframe>
    </section>

    <section class="panel route-section" data-section="compose">
      <h2>One-Off Live Send</h2>
      <div class="note">Use this when you need to send one real outreach email right now without importing a file first. This creates a tracked outreach message with unsubscribe support for one recipient.</div>
      <div class="quick-grid" style="margin-top:12px;">
        <div><label>Template<select id="oneOffTemplateId"></select></label></div>
        <div><label>Stop<select id="oneOffStopId"></select></label></div>
        <div><label>Saved Contact<select id="oneOffContactId"></select></label></div>
        <div><label>Recipient Email<input id="oneOffRecipientEmail" value="ncraig@cwa3106.com" /></label></div>
        <div><label>First Name<input id="oneOffFirstName" /></label></div>
        <div><label>Last Name<input id="oneOffLastName" /></label></div>
        <div><label>Full Name<input id="oneOffFullName" /></label></div>
        <div><label>Work Location<input id="oneOffWorkLocation" /></label></div>
        <div><label>Work Group<input id="oneOffWorkGroup" /></label></div>
        <div><label>Group<input id="oneOffGroupName" list="knownGroupOptions" /></label></div>
        <div><label>Subgroup<input id="oneOffSubgroupName" list="knownSubgroupOptions" /></label></div>
        <div><label>Department<input id="oneOffDepartment" /></label></div>
        <div><label>Bargaining Unit<input id="oneOffBargainingUnit" /></label></div>
        <div><label>Local Number<input id="oneOffLocalNumber" /></label></div>
        <div><label>Steward Name<input id="oneOffStewardName" /></label></div>
        <div><label>Rep Name<input id="oneOffRepName" /></label></div>
      </div>
      <label style="margin-top:10px;">Extra Fields JSON<textarea id="oneOffExtraFields" placeholder='{"assigned_officer":"Nick Craig"}'></textarea></label>
      <div class="actions">
        <button id="oneOffPreviewBtn" type="button">Preview One-Off</button>
        <button id="oneOffSendBtn" type="button">Send One-Off Live Email</button>
        <button id="oneOffClearBtn" class="secondary" type="button">Clear One-Off Form</button>
      </div>
      <div class="note" id="oneOffMeta">This sends one live message immediately. Use the normal Preview/Test Send panel for non-live testing.</div>
      <div class="pre" id="oneOffSubjectBox">One-off subject preview</div>
      <div class="pre" id="oneOffBodyBox">One-off body preview</div>
      <iframe id="oneOffHtmlFrame" class="preview-frame" title="One-off rendered HTML preview"></iframe>
    </section>

    <section class="grid route-section" data-section="history">
      <section class="panel">
        <h2>Suppression List</h2>
        <div class="table-wrap"><table id="suppressionsTable"></table></div>
      </section>
      <section class="panel">
        <h2>Send History</h2>
        <div class="table-wrap"><table id="sendLogTable"></table></div>
      </section>
    </section>
  </div>

  <script>
    const CURRENT_SECTION = '__OUTREACH_CURRENT_SECTION__';

    const STATUS_BUCKETS = [
      'Member - Active - Active',
      'Member - Active - Pending',
      'Non Member - Active - Active',
      'Non Member - Active - Non fr Mem',
    ];

    const CONTACT_SORT_FIELDS = new Set([
      'full_name',
      'email',
      'work_location',
      'work_group',
      'group_name',
      'subgroup_name',
      'department',
      'bargaining_unit',
      'local_number',
      'steward_name',
      'rep_name',
      'status_bucket',
      'source',
      'updated_at_utc',
    ]);

    const IMPORT_FIELD_SELECTS = [
      ['email', 'importEmailColumn'],
      ['first_name', 'importFirstNameColumn'],
      ['last_name', 'importLastNameColumn'],
      ['full_name', 'importFullNameColumn'],
      ['work_location', 'importWorkLocationColumn'],
      ['work_group', 'importWorkGroupColumn'],
      ['group_name', 'importGroupNameColumn'],
      ['subgroup_name', 'importSubgroupNameColumn'],
      ['department', 'importDepartmentColumn'],
      ['bargaining_unit', 'importBargainingUnitColumn'],
      ['local_number', 'importLocalNumberColumn'],
      ['steward_name', 'importStewardNameColumn'],
      ['rep_name', 'importRepNameColumn'],
    ];

    let state = {
      contacts: [],
      templates: [],
      stops: [],
      suppressions: [],
      send_log: [],
      summary: {},
      send_readiness: null,
      analytics: { totals: {}, campaigns: [], top_links: [], recent_activity: [], notes: [] },
      placeholder_catalog: [],
      selectedContactId: null,
      selectedTemplateId: null,
      selectedStopId: null,
      composeContactSort: {
        field: 'full_name',
        direction: 'asc',
        search: '',
        group_name: '',
        subgroup_name: '',
      },
      currentSection: CURRENT_SECTION,
      importInspector: null,
      importUpload: null,
    };

    async function call(url, options = {}) {
      const response = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
      });
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const parsed = await response.json();
          detail = parsed.detail || JSON.stringify(parsed);
        } catch (_) {
          detail = await response.text();
        }
        throw new Error(detail || 'Request failed');
      }
      const text = await response.text();
      return text ? JSON.parse(text) : {};
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function splitFilterValues(value) {
      const seen = new Set();
      return String(value || '')
        .split(/[\r\n,;|]+/)
        .map((entry) => entry.trim().toLowerCase())
        .filter((entry) => {
          if (!entry || seen.has(entry)) return false;
          seen.add(entry);
          return true;
        });
    }

    function matchesFilterValues(values, filters) {
      if (!filters.length) return true;
      return values.some((value) => filters.includes(String(value || '').trim().toLowerCase()));
    }

    function jsonOrEmpty(text) {
      try {
        return text.trim() ? JSON.parse(text) : {};
      } catch (_) {
        throw new Error('Extra fields JSON must be valid JSON');
      }
    }

    function setNote(id, text, tone = 'info') {
      const node = document.getElementById(id);
      if (!node) return;
      node.textContent = text;
      node.dataset.tone = tone;
    }

    function setHtml(id, html) {
      const node = document.getElementById(id);
      if (node) node.innerHTML = html;
    }

    async function runAction(noteId, action, pendingText = 'Working…') {
      if (noteId) setNote(noteId, pendingText, 'warning');
      try {
        return await action();
      } catch (err) {
        if (noteId) setNote(noteId, err.message || String(err), 'error');
        return null;
      }
    }

    function setSelectValueIfPresent(id, desiredValue, fallbackValue = '') {
      const select = document.getElementById(id);
      if (!select) return;
      const values = Array.from(select.options || []).map((option) => option.value);
      if (desiredValue !== null && desiredValue !== undefined && values.includes(String(desiredValue))) {
        select.value = String(desiredValue);
        return;
      }
      if (values.includes(String(fallbackValue))) {
        select.value = String(fallbackValue);
        return;
      }
      if (values.length > 0) {
        select.value = values[0];
      }
    }

    function showSection(sectionName) {
      state.currentSection = sectionName;
      document.querySelectorAll('[data-target-section]').forEach((button) => {
        button.classList.toggle('active', button.dataset.targetSection === sectionName);
      });
      document.querySelectorAll('.route-section').forEach((section) => {
        section.classList.toggle('active', section.dataset.section === sectionName);
      });
    }

    function fillSummary() {
      const summary = state.summary || {};
      const items = [
        ['Sent', summary.sent_count || 0],
        ['Failed', summary.failed_count || 0],
        ['Suppressed', summary.suppressed_count || 0],
        ['Stops', summary.stop_count || 0],
        ['Active Contacts', summary.active_contact_count || 0],
      ];
      setHtml('summaryGrid', items.map(([label, value]) => `
        <div class="stat">
          <strong>${escapeHtml(value)}</strong>
          <span class="muted">${escapeHtml(label)}</span>
        </div>`).join(''));
    }

    function renderSendReadiness() {
      const readiness = state.send_readiness || {};
      if (readiness.ready) {
        const dryRunText = readiness.dry_run ? ' Dry run is enabled.' : '';
        setNote(
          'sendReadinessNote',
          `Ready to send from ${readiness.sender_user_id || 'configured mailbox'}${readiness.reply_to_address ? ` with reply-to ${readiness.reply_to_address}` : ''}.${dryRunText}`,
          'success',
        );
        return;
      }
      const issues = Array.isArray(readiness.issues) && readiness.issues.length ? readiness.issues.join(' | ') : 'Outreach sending is not ready.';
      const sender = readiness.sender_user_id ? ` Sender: ${readiness.sender_user_id}.` : '';
      setNote('sendReadinessNote', `${issues}.${sender}`, 'error');
    }

    function analyticsFilters() {
      return {
        date_from: document.getElementById('analyticsDateFrom').value || '',
        date_to: document.getElementById('analyticsDateTo').value || '',
        stop_id: document.getElementById('analyticsStopId').value || '',
        template_id: document.getElementById('analyticsTemplateId').value || '',
        location: document.getElementById('analyticsLocation').value || '',
        work_group: document.getElementById('analyticsWorkGroup').value || '',
        recipient_email: document.getElementById('analyticsRecipientEmail').value || '',
      };
    }

    function queryStringFromFilters(filters) {
      const params = new URLSearchParams();
      Object.entries(filters || {}).forEach(([key, value]) => {
        if (value !== null && value !== undefined && String(value).trim() !== '') params.set(key, String(value));
      });
      const rendered = params.toString();
      return rendered ? `?${rendered}` : '';
    }

    function requiredNumericValue(id, label) {
      const raw = String(document.getElementById(id).value || '').trim();
      const parsed = Number(raw);
      if (!raw || !Number.isFinite(parsed) || parsed <= 0) {
        throw new Error(`${label} is required.`);
      }
      return parsed;
    }

    function describeSendResult(prefix, result) {
      if (!result) return `${prefix}: no result returned.`;
      const errorText = result.error_text ? ` Error: ${result.error_text}` : '';
      return `${prefix}: ${result.status} to ${result.recipient_email || 'unknown recipient'}${errorText}`;
    }

    function renderAnalytics() {
      const analytics = state.analytics || {};
      const totals = analytics.totals || {};
      const metrics = [
        ['Clicks', totals.click_count || 0],
        ['Unique Clicks', totals.unique_click_count || 0],
        ['Estimated Opens', totals.estimated_open_count || 0],
        ['Unique Estimated Opens', totals.unique_estimated_open_count || 0],
        ['Unsubscribes', totals.unsubscribe_count || 0],
        ['Sent', totals.sent_count || 0],
        ['Failed', totals.failed_count || 0],
        ['Suppressed', totals.suppressed_count || 0],
      ];
      setHtml('analyticsGrid', metrics.map(([label, value]) => `
        <div class="stat">
          <strong>${escapeHtml(value)}</strong>
          <span class="muted">${escapeHtml(label)}</span>
        </div>`).join(''));
      if (Array.isArray(analytics.notes) && analytics.notes.length) {
        setNote('analyticsNote', analytics.notes.join(' '), 'info');
      }

      setHtml('analyticsCampaignsTable',
        '<tr><th>Location</th><th>Visit</th><th>Sent</th><th>Failed</th><th>Suppressed</th><th>Unsubscribes</th><th>Unique Opens</th><th>Unique Clicks</th></tr>' +
        (analytics.campaigns || []).map((row) => `
          <tr>
            <td>${escapeHtml(row.location_name || '')}</td>
            <td>${escapeHtml(row.visit_date_local || '')}</td>
            <td>${escapeHtml(row.sent_count || 0)}</td>
            <td>${escapeHtml(row.failed_count || 0)}</td>
            <td>${escapeHtml(row.suppressed_count || 0)}</td>
            <td>${escapeHtml(row.unsubscribe_count || 0)}</td>
            <td>${escapeHtml(row.unique_estimated_open_count || 0)}</td>
            <td>${escapeHtml(row.unique_click_count || 0)}</td>
          </tr>`).join(''),
      );

      setHtml('analyticsTopLinksTable',
        '<tr><th>Destination</th><th>Clicks</th><th>Unique Clicks</th></tr>' +
        (analytics.top_links || []).map((row) => `
          <tr>
            <td><a href="${escapeHtml(row.destination_url || '')}" target="_blank" rel="noreferrer">${escapeHtml(row.destination_url || '')}</a></td>
            <td>${escapeHtml(row.click_count || 0)}</td>
            <td>${escapeHtml(row.unique_click_count || 0)}</td>
          </tr>`).join(''),
      );

      setHtml('analyticsActivityTable',
        '<tr><th>Time</th><th>Type</th><th>Recipient</th><th>Location</th><th>Detail</th><th>Flag</th></tr>' +
        (analytics.recent_activity || []).map((row) => `
          <tr>
            <td>${escapeHtml(row.occurred_at_utc || '')}</td>
            <td>${escapeHtml(row.event_type || '')}</td>
            <td>${escapeHtml(row.recipient_email || '')}</td>
            <td>${escapeHtml(row.location_name || '')}</td>
            <td>${escapeHtml(row.destination_url || row.automation_reason || '')}</td>
            <td>${row.suspected_automation ? '<span class="pill">Flagged</span>' : ''}</td>
          </tr>`).join(''),
      );
    }

    async function loadAnalytics() {
      const query = queryStringFromFilters(analyticsFilters());
      state.analytics = await call(`/officers/outreach/analytics${query}`);
      renderAnalytics();
    }

    function contactPayload() {
      return {
        email: document.getElementById('contactEmail').value,
        first_name: document.getElementById('contactFirstName').value,
        last_name: document.getElementById('contactLastName').value,
        full_name: document.getElementById('contactFullName').value,
        work_location: document.getElementById('contactLocation').value,
        work_group: document.getElementById('contactWorkGroup').value,
        group_name: document.getElementById('contactGroupName').value,
        subgroup_name: document.getElementById('contactSubgroupName').value,
        department: document.getElementById('contactDepartment').value,
        bargaining_unit: document.getElementById('contactBargainingUnit').value,
        local_number: document.getElementById('contactLocalNumber').value,
        steward_name: document.getElementById('contactStewardName').value,
        rep_name: document.getElementById('contactRepName').value,
        active: document.getElementById('contactActive').value === 'true',
        notes: document.getElementById('contactNotes').value,
        source: document.getElementById('contactSource').value,
        extra_fields: jsonOrEmpty(document.getElementById('contactExtraFields').value),
      };
    }

    function templatePayload() {
      return {
        template_key: document.getElementById('templateKey').value,
        name: document.getElementById('templateName').value,
        template_type: document.getElementById('templateType').value,
        subject_template: document.getElementById('templateSubject').value,
        body_template: document.getElementById('templateBody').value,
        active: document.getElementById('templateActive').value === 'true',
      };
    }

    function stopPayload() {
      return {
        location_name: document.getElementById('stopLocationName').value,
        visit_date_local: document.getElementById('stopVisitDate').value,
        start_time_local: document.getElementById('stopStartTime').value,
        end_time_local: document.getElementById('stopEndTime').value,
        timezone: document.getElementById('stopTimezone').value,
        audience_location: document.getElementById('stopAudienceLocation').value,
        audience_work_group: document.getElementById('stopAudienceWorkGroup').value,
        audience_group_name: document.getElementById('stopAudienceGroupName').value,
        audience_subgroup_name: document.getElementById('stopAudienceSubgroupName').value,
        audience_status_bucket: document.getElementById('stopAudienceStatusBucket').value || null,
        notice_subject: document.getElementById('stopNoticeSubject').value,
        reminder_subject: document.getElementById('stopReminderSubject').value,
        notice_send_at_local: document.getElementById('stopNoticeSendLocal').value || null,
        reminder_send_at_local: document.getElementById('stopReminderSendLocal').value || null,
        status: document.getElementById('stopStatus').value,
      };
    }

    function uniqueContactValues(...fieldNames) {
      const seen = new Set();
      const values = [];
      (state.contacts || []).forEach((row) => {
        fieldNames.forEach((fieldName) => {
          const value = String(row[fieldName] || '').trim();
          if (!value) return;
          const key = value.toLowerCase();
          if (seen.has(key)) return;
          seen.add(key);
          values.push(value);
        });
      });
      return values.sort((left, right) => left.localeCompare(right, undefined, { numeric: true, sensitivity: 'base' }));
    }

    function renderContactValueOptions() {
      const groupValues = uniqueContactValues('group_name', 'work_group');
      const subgroupValues = uniqueContactValues('subgroup_name', 'department');
      setHtml('knownGroupOptions', groupValues.map((value) => `<option value="${escapeHtml(value)}"></option>`).join(''));
      setHtml('knownSubgroupOptions', subgroupValues.map((value) => `<option value="${escapeHtml(value)}"></option>`).join(''));
    }

    function contactSortValue(row, fieldName) {
      if (fieldName === 'full_name') return row.full_name || row.first_name || row.last_name || row.email || '';
      if (fieldName === 'status_bucket') return row.status_bucket || [row.membership_type, row.employment_status, row.status_detail].filter(Boolean).join(' - ');
      if (fieldName === 'updated_at_utc') return row.updated_at_utc || '';
      return row[fieldName] || '';
    }

    function updateComposeContactSortState() {
      const fieldNode = document.getElementById('composeContactSortField');
      const directionNode = document.getElementById('composeContactSortDirection');
      const searchNode = document.getElementById('composeContactSearch');
      const groupNode = document.getElementById('composeContactGroupFilter');
      const subgroupNode = document.getElementById('composeContactSubgroupFilter');
      if (fieldNode && CONTACT_SORT_FIELDS.has(fieldNode.value || '')) {
        state.composeContactSort.field = fieldNode.value;
      }
      if (directionNode && ['asc', 'desc'].includes(directionNode.value || '')) {
        state.composeContactSort.direction = directionNode.value;
      }
      if (searchNode) {
        state.composeContactSort.search = searchNode.value || '';
      }
      if (groupNode) {
        state.composeContactSort.group_name = groupNode.value || '';
      }
      if (subgroupNode) {
        state.composeContactSort.subgroup_name = subgroupNode.value || '';
      }
    }

    function searchableContactText(row) {
      return [
        row.full_name,
        row.first_name,
        row.last_name,
        row.email,
        row.work_location,
        row.work_group,
        row.group_name,
        row.subgroup_name,
        row.department,
        row.bargaining_unit,
        row.local_number,
        row.steward_name,
        row.rep_name,
        row.status_bucket,
        row.membership_type,
        row.employment_status,
        row.status_detail,
        row.source,
      ].filter(Boolean).join(' ').toLowerCase();
    }

    function sortedComposeContacts() {
      updateComposeContactSortState();
      const sortField = state.composeContactSort.field || 'full_name';
      const sortDirection = state.composeContactSort.direction === 'desc' ? 'desc' : 'asc';
      const search = (state.composeContactSort.search || '').trim().toLowerCase();
      const groupFilters = splitFilterValues(state.composeContactSort.group_name || '');
      const subgroupFilters = splitFilterValues(state.composeContactSort.subgroup_name || '');
      const rows = [...(state.contacts || [])]
        .filter((row) => matchesFilterValues([row.group_name, row.work_group], groupFilters))
        .filter((row) => matchesFilterValues([row.subgroup_name, row.department], subgroupFilters))
        .filter((row) => !search || searchableContactText(row).includes(search))
        .sort((left, right) => {
          const leftValue = String(contactSortValue(left, sortField)).toLowerCase();
          const rightValue = String(contactSortValue(right, sortField)).toLowerCase();
          const compared = leftValue.localeCompare(rightValue, undefined, { numeric: true, sensitivity: 'base' });
          if (compared !== 0) return sortDirection === 'desc' ? -compared : compared;
          return String(left.email || '').localeCompare(String(right.email || ''), undefined, { numeric: true, sensitivity: 'base' });
        });
      return rows;
    }

    function contactOptionLabel(row) {
      const locationBits = [row.work_location, row.work_group, row.group_name, row.subgroup_name].filter(Boolean).join(' / ');
      const statusText = row.status_bucket || [row.membership_type, row.employment_status, row.status_detail].filter(Boolean).join(' / ');
      const detailBits = [locationBits, statusText].filter(Boolean).join(' • ');
      return detailBits
        ? `${row.full_name || row.email || ''} - ${row.email || ''} - ${detailBits}`
        : `${row.full_name || row.email || ''} - ${row.email || ''}`;
    }

    function renderComposeContactSortNote(sortedRows) {
      const rows = Array.isArray(sortedRows) ? sortedRows : sortedComposeContacts();
      const fieldLabel = document.getElementById('composeContactSortField') ? document.getElementById('composeContactSortField').selectedOptions[0].textContent : 'Name';
      const directionLabel = state.composeContactSort.direction === 'desc' ? 'descending' : 'ascending';
      const searchText = (state.composeContactSort.search || '').trim();
      const groupText = (state.composeContactSort.group_name || '').trim();
      const subgroupText = (state.composeContactSort.subgroup_name || '').trim();
      const filters = [
        searchText ? `search "${searchText}"` : '',
        groupText ? `group "${groupText}"` : '',
        subgroupText ? `subgroup "${subgroupText}"` : '',
      ].filter(Boolean).join(', ');
      setNote(
        'composeContactSortNote',
        `${rows.length} saved contacts available for send selection, sorted by ${fieldLabel} (${directionLabel})${filters ? ` and filtered by ${filters}` : ''}.`,
        'info',
      );
    }

    function filteredContacts() {
      const selectedBucket = document.getElementById('contactStatusBucketFilter').value || '';
      const selectedGroups = splitFilterValues(document.getElementById('contactGroupFilter').value || '');
      const selectedSubgroups = splitFilterValues(document.getElementById('contactSubgroupFilter').value || '');
      return (state.contacts || [])
        .filter((row) => !selectedBucket || row.status_bucket === selectedBucket)
        .filter((row) => matchesFilterValues([row.group_name, row.work_group], selectedGroups))
        .filter((row) => matchesFilterValues([row.subgroup_name, row.department], selectedSubgroups));
    }

    function renderContactBucketCounts() {
      const counts = STATUS_BUCKETS.map((bucket) => [
        bucket,
        (state.contacts || []).filter((row) => row.status_bucket === bucket).length,
      ]);
      setHtml('contactBucketCounts', counts.map(([bucket, count]) => `
        <div class="mini-stat">
          <strong>${escapeHtml(count)}</strong>
          <span class="muted">${escapeHtml(bucket)}</span>
        </div>`).join(''));
    }

    function renderContacts() {
      const rows = filteredContacts();
      setHtml('contactsTable',
        '<tr><th>Email</th><th>Name</th><th>Location</th><th>Work Group</th><th>Group</th><th>Subgroup</th><th>Status Bucket</th><th>Status Detail</th><th>Source</th><th>Record</th></tr>' +
        rows.map((row) => `
          <tr data-contact-id="${escapeHtml(row.id)}">
            <td>${escapeHtml(row.email || '')}</td>
            <td>${escapeHtml(row.full_name || row.first_name || '')}</td>
            <td>${escapeHtml(row.work_location || '')}</td>
            <td>${escapeHtml(row.work_group || '')}</td>
            <td>${escapeHtml(row.group_name || '')}</td>
            <td>${escapeHtml(row.subgroup_name || '')}</td>
            <td>${row.status_bucket ? `<span class="pill">${escapeHtml(row.status_bucket)}</span>` : ''}</td>
            <td>${escapeHtml([row.membership_type, row.employment_status, row.status_detail].filter(Boolean).join(' / '))}</td>
            <td>${escapeHtml(row.source || '')}</td>
            <td><span class="pill ${row.active ? '' : 'inactive'}">${row.active ? 'Active' : 'Inactive'}</span></td>
          </tr>`).join(''),
      );
      document.querySelectorAll('#contactsTable tr[data-contact-id]').forEach((tr) => {
        tr.addEventListener('click', () => selectContact(Number(tr.dataset.contactId)));
      });
    }

    function renderTemplates() {
      setHtml('templatesTable',
        '<tr><th>Name</th><th>Key</th><th>Type</th><th>Status</th><th>Seeded</th></tr>' +
        (state.templates || []).map((row) => `
          <tr data-template-id="${escapeHtml(row.id)}">
            <td>${escapeHtml(row.name || '')}</td>
            <td>${escapeHtml(row.template_key || '')}</td>
            <td>${escapeHtml(row.template_type || '')}</td>
            <td><span class="pill ${row.active ? '' : 'inactive'}">${row.active ? 'Active' : 'Inactive'}</span></td>
            <td>${row.seeded ? 'Yes' : 'No'}</td>
          </tr>`).join(''),
      );
      document.querySelectorAll('#templatesTable tr[data-template-id]').forEach((tr) => {
        tr.addEventListener('click', () => selectTemplate(Number(tr.dataset.templateId)));
      });
    }

    function renderStops() {
      setHtml('stopsTable',
        '<tr><th>Location</th><th>Visit</th><th>Audience</th><th>Notice Send</th><th>Reminder Send</th><th>Status</th></tr>' +
        (state.stops || []).map((row) => {
          const audienceParts = [row.audience_location, row.audience_work_group, row.audience_group_name, row.audience_subgroup_name, row.audience_status_bucket].filter(Boolean);
          return `
            <tr data-stop-id="${escapeHtml(row.id)}">
              <td>${escapeHtml(row.location_name || '')}</td>
              <td>${escapeHtml(`${row.visit_date_local || ''} ${row.start_time_local || ''}-${row.end_time_local || ''}`)}</td>
              <td>${escapeHtml(audienceParts.join(' / '))}</td>
              <td>${escapeHtml(row.notice_send_at_local || '')}</td>
              <td>${escapeHtml(row.reminder_send_at_local || '')}</td>
              <td><span class="pill">${escapeHtml(row.status || '')}</span></td>
            </tr>`;
        }).join(''),
      );
      document.querySelectorAll('#stopsTable tr[data-stop-id]').forEach((tr) => {
        tr.addEventListener('click', () => selectStop(Number(tr.dataset.stopId)));
      });
    }

    function renderSuppressions() {
      setHtml('suppressionsTable',
        '<tr><th>Email</th><th>Reason</th><th>Created</th><th></th></tr>' +
        (state.suppressions || []).map((row) => `
          <tr>
            <td>${escapeHtml(row.email || '')}</td>
            <td>${escapeHtml(row.reason || '')}</td>
            <td>${escapeHtml(row.created_at_utc || '')}</td>
            <td><button class="danger" data-unsuppress-id="${escapeHtml(row.id)}" type="button">Remove</button></td>
          </tr>`).join(''),
      );
      document.querySelectorAll('button[data-unsuppress-id]').forEach((button) => {
        button.addEventListener('click', async () => {
          const result = await runAction('runDueNote', async () => {
            await call(`/officers/outreach/suppressions/${button.dataset.unsuppressId}`, { method: 'DELETE' });
            await loadBootstrap();
            return true;
          }, 'Removing suppression…');
          if (result) setNote('runDueNote', 'Suppression removed.', 'success');
        });
      });
    }

    function renderSendLog() {
      setHtml('sendLogTable',
        '<tr><th>Recipient</th><th>Type</th><th>Subject</th><th>Status</th><th>Stop</th><th>Sent</th><th>Error</th></tr>' +
        (state.send_log || []).map((row) => `
          <tr>
            <td>${escapeHtml(row.recipient_email || '')}</td>
            <td>${escapeHtml(row.email_type || '')}</td>
            <td>${escapeHtml(row.subject || '')}</td>
            <td><span class="pill ${row.status === 'failed' ? 'inactive' : ''}">${escapeHtml(row.status || '')}</span></td>
            <td>${escapeHtml([row.location_name, row.visit_date_local].filter(Boolean).join(' '))}</td>
            <td>${escapeHtml(row.sent_at_utc || '')}</td>
            <td class="error">${escapeHtml(row.error_text || '')}</td>
          </tr>`).join(''),
      );
    }

    function optionMarkup(options, placeholder = '') {
      return [placeholder ? `<option value="">${escapeHtml(placeholder)}</option>` : '<option value="">Not mapped</option>']
        .concat(options.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`))
        .join('');
    }

    function refreshSelects() {
      const sortedContactRows = sortedComposeContacts();
      renderContactValueOptions();
      const contactOptions = ['<option value="">No saved contact</option>']
        .concat(sortedContactRows.map((row) => `<option value="${escapeHtml(row.id)}">${escapeHtml(contactOptionLabel(row))}</option>`))
        .join('');
      const templateOptions = (state.templates || []).map((row) => `<option value="${escapeHtml(row.id)}">${escapeHtml(row.name || '')}</option>`).join('');
      const stopOptions = (state.stops || []).map((row) => `<option value="${escapeHtml(row.id)}">${escapeHtml(`${row.location_name || ''} ${row.visit_date_local || ''}`)}</option>`).join('');

      document.getElementById('previewContactId').innerHTML = contactOptions;
      document.getElementById('previewTemplateId').innerHTML = templateOptions;
      document.getElementById('previewStopId').innerHTML = stopOptions;
      document.getElementById('quickMessageStopId').innerHTML = stopOptions;
      document.getElementById('oneOffContactId').innerHTML = contactOptions;
      document.getElementById('oneOffTemplateId').innerHTML = templateOptions;
      document.getElementById('oneOffStopId').innerHTML = stopOptions;
      document.getElementById('analyticsTemplateId').innerHTML = '<option value="">All templates</option>' + templateOptions;
      document.getElementById('analyticsStopId').innerHTML = '<option value="">All stops</option>' + stopOptions;
      document.getElementById('placeholderList').textContent = 'Available placeholders: ' + (state.placeholder_catalog || []).join(', ');

      setSelectValueIfPresent('previewContactId', state.selectedContactId, '');
      setSelectValueIfPresent('oneOffContactId', state.selectedContactId, '');
      setSelectValueIfPresent('previewTemplateId', state.selectedTemplateId);
      setSelectValueIfPresent('oneOffTemplateId', state.selectedTemplateId);
      setSelectValueIfPresent('previewStopId', state.selectedStopId);
      setSelectValueIfPresent('oneOffStopId', state.selectedStopId);
      setSelectValueIfPresent('quickMessageStopId', state.selectedStopId);
      renderComposeContactSortNote(sortedContactRows);
    }

    function selectContact(contactId) {
      state.selectedContactId = contactId;
      const row = (state.contacts || []).find((item) => item.id === contactId);
      if (!row) return;
      document.getElementById('contactEmail').value = row.email || '';
      document.getElementById('contactFirstName').value = row.first_name || '';
      document.getElementById('contactLastName').value = row.last_name || '';
      document.getElementById('contactFullName').value = row.full_name || '';
      document.getElementById('contactLocation').value = row.work_location || '';
      document.getElementById('contactWorkGroup').value = row.work_group || '';
      document.getElementById('contactGroupName').value = row.group_name || '';
      document.getElementById('contactSubgroupName').value = row.subgroup_name || '';
      document.getElementById('contactDepartment').value = row.department || '';
      document.getElementById('contactBargainingUnit').value = row.bargaining_unit || '';
      document.getElementById('contactLocalNumber').value = row.local_number || '';
      document.getElementById('contactStewardName').value = row.steward_name || '';
      document.getElementById('contactRepName').value = row.rep_name || '';
      document.getElementById('contactActive').value = row.active ? 'true' : 'false';
      document.getElementById('contactNotes').value = row.notes || '';
      document.getElementById('contactSource').value = row.source || '';
      document.getElementById('contactExtraFields').value = JSON.stringify(row.extra_fields || {}, null, 2);
      setSelectValueIfPresent('previewContactId', row.id, '');
      setSelectValueIfPresent('oneOffContactId', row.id, '');
      setNote('contactFormNote', `Editing ${row.email || row.full_name || 'contact'}.`, 'success');
    }

    function selectTemplate(templateId) {
      state.selectedTemplateId = templateId;
      const row = (state.templates || []).find((item) => item.id === templateId);
      if (!row) return;
      document.getElementById('templateKey').value = row.template_key || '';
      document.getElementById('templateName').value = row.name || '';
      document.getElementById('templateType').value = row.template_type || 'notice';
      document.getElementById('templateSubject').value = row.subject_template || '';
      document.getElementById('templateBody').value = row.body_template || '';
      document.getElementById('templateActive').value = row.active ? 'true' : 'false';
      setSelectValueIfPresent('previewTemplateId', row.id, '');
      setSelectValueIfPresent('oneOffTemplateId', row.id, '');
      setNote('templateFormNote', `Editing template ${row.name || row.template_key || row.id}.`, 'success');
    }

    function selectStop(stopId) {
      state.selectedStopId = stopId;
      const row = (state.stops || []).find((item) => item.id === stopId);
      if (!row) return;
      document.getElementById('stopLocationName').value = row.location_name || '';
      document.getElementById('stopVisitDate').value = row.visit_date_local || '';
      document.getElementById('stopStartTime').value = row.start_time_local || '';
      document.getElementById('stopEndTime').value = row.end_time_local || '';
      document.getElementById('stopTimezone').value = row.timezone || 'America/New_York';
      document.getElementById('stopAudienceLocation').value = row.audience_location || '';
      document.getElementById('stopAudienceWorkGroup').value = row.audience_work_group || '';
      document.getElementById('stopAudienceGroupName').value = row.audience_group_name || '';
      document.getElementById('stopAudienceSubgroupName').value = row.audience_subgroup_name || '';
      document.getElementById('stopAudienceStatusBucket').value = row.audience_status_bucket || '';
      document.getElementById('stopNoticeSubject').value = row.notice_subject || '';
      document.getElementById('stopReminderSubject').value = row.reminder_subject || '';
      document.getElementById('stopNoticeSendLocal').value = row.notice_send_at_local || '';
      document.getElementById('stopReminderSendLocal').value = row.reminder_send_at_local || '';
      document.getElementById('stopStatus').value = row.status || 'draft';
      setSelectValueIfPresent('previewStopId', row.id, '');
      setSelectValueIfPresent('quickMessageStopId', row.id, '');
      setSelectValueIfPresent('oneOffStopId', row.id, '');
      setNote('stopFormNote', `Editing stop ${row.location_name || row.id}.`, 'success');
    }

    function clearContactForm() {
      state.selectedContactId = null;
      ['contactEmail','contactFirstName','contactLastName','contactFullName','contactLocation','contactWorkGroup','contactGroupName','contactSubgroupName','contactDepartment','contactBargainingUnit','contactLocalNumber','contactStewardName','contactRepName','contactNotes','contactSource','contactExtraFields'].forEach((id) => {
        document.getElementById(id).value = '';
      });
      document.getElementById('contactActive').value = 'true';
      setSelectValueIfPresent('previewContactId', '', '');
      setSelectValueIfPresent('oneOffContactId', '', '');
      setNote('contactFormNote', 'Contact form cleared.', 'info');
    }

    function clearTemplateForm() {
      state.selectedTemplateId = null;
      ['templateKey','templateName','templateSubject','templateBody'].forEach((id) => {
        document.getElementById(id).value = '';
      });
      document.getElementById('templateType').value = 'notice';
      document.getElementById('templateActive').value = 'true';
      setNote('templateFormNote', 'Template form cleared.', 'info');
    }

    function clearStopForm() {
      state.selectedStopId = null;
      ['stopLocationName','stopVisitDate','stopStartTime','stopEndTime','stopAudienceLocation','stopAudienceWorkGroup','stopAudienceGroupName','stopAudienceSubgroupName','stopNoticeSubject','stopReminderSubject','stopNoticeSendLocal','stopReminderSendLocal'].forEach((id) => {
        document.getElementById(id).value = '';
      });
      document.getElementById('stopAudienceStatusBucket').value = '';
      document.getElementById('stopTimezone').value = 'America/New_York';
      document.getElementById('stopStatus').value = 'draft';
      setNote('stopFormNote', 'Stop form cleared.', 'info');
    }

    function oneOffManualContactPayload() {
      return {
        first_name: document.getElementById('oneOffFirstName').value || null,
        last_name: document.getElementById('oneOffLastName').value || null,
        full_name: document.getElementById('oneOffFullName').value || null,
        work_location: document.getElementById('oneOffWorkLocation').value || null,
        work_group: document.getElementById('oneOffWorkGroup').value || null,
        group_name: document.getElementById('oneOffGroupName').value || null,
        subgroup_name: document.getElementById('oneOffSubgroupName').value || null,
        department: document.getElementById('oneOffDepartment').value || null,
        bargaining_unit: document.getElementById('oneOffBargainingUnit').value || null,
        local_number: document.getElementById('oneOffLocalNumber').value || null,
        steward_name: document.getElementById('oneOffStewardName').value || null,
        rep_name: document.getElementById('oneOffRepName').value || null,
        extra_fields: jsonOrEmpty(document.getElementById('oneOffExtraFields').value),
      };
    }

    function clearOneOffForm() {
      ['oneOffFirstName','oneOffLastName','oneOffFullName','oneOffWorkLocation','oneOffWorkGroup','oneOffGroupName','oneOffSubgroupName','oneOffDepartment','oneOffBargainingUnit','oneOffLocalNumber','oneOffStewardName','oneOffRepName','oneOffExtraFields'].forEach((id) => {
        document.getElementById(id).value = '';
      });
      document.getElementById('oneOffRecipientEmail').value = 'ncraig@cwa3106.com';
      setSelectValueIfPresent('oneOffContactId', '', '');
      setNote('oneOffMeta', 'This sends one live message immediately. Use the normal Preview/Test Send panel for non-live testing.', 'info');
      document.getElementById('oneOffSubjectBox').textContent = 'One-off subject preview';
      document.getElementById('oneOffBodyBox').textContent = 'One-off body preview';
      document.getElementById('oneOffHtmlFrame').srcdoc = '';
    }

    function resetQuickMessage() {
      document.getElementById('quickMessageRecipientEmail').value = 'ncraig@cwa3106.com';
      document.getElementById('quickMessageSubject').value = 'Quick outreach test for {{ location }}';
      document.getElementById('quickMessageBody').value = `Hi {{ first_name | default('Nick') }},

This is a quick test message from the outreach mailer for {{ location }} on {{ visit_date }} from {{ visit_time }}.

Thank you,
{{ sender_name }}`;
      setNote('quickMessageMeta', 'Quick test preview and send status will appear below.', 'info');
      document.getElementById('quickMessageSubjectBox').textContent = 'Quick test subject preview';
      document.getElementById('quickMessageBodyBox').textContent = 'Quick test body preview';
      document.getElementById('quickMessageHtmlFrame').srcdoc = '';
    }

    async function fileToBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = String(reader.result || '');
          const parts = result.split(',', 2);
          resolve(parts.length === 2 ? parts[1] : '');
        };
        reader.onerror = () => reject(new Error('File read failed'));
        reader.readAsDataURL(file);
      });
    }

    function currentImportFileKey(file) {
      return `${file.name}:${file.size}:${file.lastModified}`;
    }

    async function ensureImportUpload() {
      const file = document.getElementById('contactImportFile').files[0];
      if (!file) throw new Error('Choose a CSV or XLSX file first.');
      const fileKey = currentImportFileKey(file);
      if (!state.importUpload || state.importUpload.fileKey !== fileKey) {
        state.importUpload = {
          filename: file.name,
          content_base64: await fileToBase64(file),
          fileKey,
        };
      }
      return state.importUpload;
    }

    function emptyImportMapping() {
      const fieldMapping = {};
      IMPORT_FIELD_SELECTS.forEach(([fieldName]) => {
        fieldMapping[fieldName] = null;
      });
      return {
        field_mapping: fieldMapping,
        status_mapping: {
          mode: 'combined',
          combined_status_column: null,
          membership_type_column: null,
          employment_status_column: null,
          status_detail_column: null,
        },
      };
    }

    function importHeaders() {
      return (state.importInspector && Array.isArray(state.importInspector.headers)) ? state.importInspector.headers : [];
    }

    function populateImportSelect(id, headers, value) {
      const select = document.getElementById(id);
      select.innerHTML = optionMarkup(headers);
      setSelectValueIfPresent(id, value || '', '');
    }

    function applyStatusModeUi() {
      const mode = document.getElementById('importStatusMode').value || 'combined';
      document.getElementById('importCombinedStatusColumn').disabled = mode !== 'combined';
      document.getElementById('importMembershipTypeColumn').disabled = mode !== 'split';
      document.getElementById('importEmploymentStatusColumn').disabled = mode !== 'split';
      document.getElementById('importStatusDetailColumn').disabled = mode !== 'split';
    }

    function currentImportMapping() {
      const mapping = emptyImportMapping();
      IMPORT_FIELD_SELECTS.forEach(([fieldName, id]) => {
        mapping.field_mapping[fieldName] = document.getElementById(id).value || null;
      });
      mapping.status_mapping.mode = document.getElementById('importStatusMode').value || 'combined';
      mapping.status_mapping.combined_status_column = document.getElementById('importCombinedStatusColumn').value || null;
      mapping.status_mapping.membership_type_column = document.getElementById('importMembershipTypeColumn').value || null;
      mapping.status_mapping.employment_status_column = document.getElementById('importEmploymentStatusColumn').value || null;
      mapping.status_mapping.status_detail_column = document.getElementById('importStatusDetailColumn').value || null;
      return mapping;
    }

    function renderImportPreview(preview) {
      const resolvedPreview = preview || {
        imported_count: 0,
        updated_count: 0,
        skipped_count: 0,
        ignored_count: 0,
        bucket_counts: {},
        skipped_reasons: {},
        ignored_reasons: {},
      };
      const cards = [
        ['New Contacts', resolvedPreview.imported_count || 0],
        ['Updates', resolvedPreview.updated_count || 0],
        ['Skipped', resolvedPreview.skipped_count || 0],
        ['Ignored', resolvedPreview.ignored_count || 0],
      ];
      setHtml('importPreviewSummary', cards.map(([label, value]) => `
        <div class="mini-stat">
          <strong>${escapeHtml(value)}</strong>
          <span class="muted">${escapeHtml(label)}</span>
        </div>`).join(''));

      const bucketText = STATUS_BUCKETS
        .map((bucket) => `${bucket}: ${resolvedPreview.bucket_counts && resolvedPreview.bucket_counts[bucket] ? resolvedPreview.bucket_counts[bucket] : 0}`)
        .join(' | ');
      const skippedText = Object.entries(resolvedPreview.skipped_reasons || {})
        .map(([reason, count]) => `${reason}: ${count}`)
        .join(' | ') || 'none';
      const ignoredText = Object.entries(resolvedPreview.ignored_reasons || {})
        .map(([reason, count]) => `${reason}: ${count}`)
        .join(' | ') || 'none';
      setNote('importPreviewMeta', `Buckets: ${bucketText}. Skipped reasons: ${skippedText}. Ignored reasons: ${ignoredText}.`, 'info');
    }

    function renderImportSampleRows(headers, rows) {
      if (!headers.length) {
        setHtml('importSampleTable', '<tr><th>Sample Rows</th></tr><tr><td>No headers detected.</td></tr>');
        return;
      }
      const headerRow = `<tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join('')}</tr>`;
      const sampleRows = (rows || []).map((row) => `
        <tr>
          ${headers.map((header) => `<td>${escapeHtml(row[header] || '')}</td>`).join('')}
        </tr>`).join('');
      setHtml('importSampleTable', headerRow + sampleRows);
    }

    function applyImportInspector(result) {
      state.importInspector = result;
      const headers = Array.isArray(result.headers) ? result.headers : [];
      const mapping = result.effective_mapping || emptyImportMapping();
      const sheets = Array.isArray(result.sheets) ? result.sheets : [];

      document.getElementById('contactImportSheet').innerHTML = sheets.length
        ? sheets.map((sheet) => `<option value="${escapeHtml(sheet.name)}">${escapeHtml(sheet.name)} (${escapeHtml(sheet.row_count || 0)} rows)</option>`).join('')
        : '<option value="">No sheets detected</option>';
      setSelectValueIfPresent('contactImportSheet', result.selected_sheet_name || '', '');
      document.getElementById('importStatusMode').value = (mapping.status_mapping && mapping.status_mapping.mode) || 'combined';

      IMPORT_FIELD_SELECTS.forEach(([fieldName, id]) => {
        populateImportSelect(id, headers, mapping.field_mapping ? mapping.field_mapping[fieldName] : null);
      });
      populateImportSelect('importCombinedStatusColumn', headers, mapping.status_mapping ? mapping.status_mapping.combined_status_column : null);
      populateImportSelect('importMembershipTypeColumn', headers, mapping.status_mapping ? mapping.status_mapping.membership_type_column : null);
      populateImportSelect('importEmploymentStatusColumn', headers, mapping.status_mapping ? mapping.status_mapping.employment_status_column : null);
      populateImportSelect('importStatusDetailColumn', headers, mapping.status_mapping ? mapping.status_mapping.status_detail_column : null);

      applyStatusModeUi();
      renderImportPreview(result.preview);
      renderImportSampleRows(headers, result.sample_rows || []);

      const remembered = result.remembered_mapping ? ' Remembered mapping found for this header set.' : '';
      setNote('importResult', `Inspected ${state.importUpload ? state.importUpload.filename : 'file'} using sheet ${result.selected_sheet_name || 'default'}.${remembered}`, 'success');
    }

    function resetImportWizard(clearFile = false) {
      state.importInspector = null;
      state.importUpload = null;
      document.getElementById('contactImportSheet').innerHTML = '<option value="">Choose a file first</option>';
      document.getElementById('importStatusMode').value = 'combined';
      const headers = [];
      IMPORT_FIELD_SELECTS.forEach(([, id]) => populateImportSelect(id, headers, null));
      populateImportSelect('importCombinedStatusColumn', headers, null);
      populateImportSelect('importMembershipTypeColumn', headers, null);
      populateImportSelect('importEmploymentStatusColumn', headers, null);
      populateImportSelect('importStatusDetailColumn', headers, null);
      applyStatusModeUi();
      setHtml('importPreviewSummary', '');
      setNote('importPreviewMeta', 'Preview counts and ignored-row reasons will appear here.', 'info');
      setHtml('importSampleTable', '');
      setNote('importResult', 'Import preview will appear after inspection.', 'info');
      if (clearFile) document.getElementById('contactImportFile').value = '';
    }

    async function inspectImportFile() {
      const upload = await ensureImportUpload();
      const result = await call('/officers/outreach/contacts/import/inspect', {
        method: 'POST',
        body: JSON.stringify({
          filename: upload.filename,
          content_base64: upload.content_base64,
        }),
      });
      applyImportInspector(result);
    }

    async function refreshImportPreview() {
      const upload = await ensureImportUpload();
      const result = await call('/officers/outreach/contacts/import/inspect', {
        method: 'POST',
        body: JSON.stringify({
          filename: upload.filename,
          content_base64: upload.content_base64,
          sheet_name: document.getElementById('contactImportSheet').value || null,
          mapping: currentImportMapping(),
        }),
      });
      applyImportInspector(result);
    }

    async function applyImportCommit() {
      const upload = await ensureImportUpload();
      const result = await call('/officers/outreach/contacts/import', {
        method: 'POST',
        body: JSON.stringify({
          filename: upload.filename,
          content_base64: upload.content_base64,
          sheet_name: document.getElementById('contactImportSheet').value || null,
          mapping: currentImportMapping(),
        }),
      });
      const summaryText = `Imported ${result.imported_count || 0}, updated ${result.updated_count || 0}, skipped ${result.skipped_count || 0}, ignored ${result.ignored_count || 0}${Array.isArray(result.errors) && result.errors.length ? `. Errors: ${result.errors.join(' | ')}` : ''}`;
      const summaryTone = Array.isArray(result.errors) && result.errors.length ? 'warning' : 'success';
      resetImportWizard(true);
      renderImportPreview(result);
      setNote('importResult', summaryText, summaryTone);
      await loadBootstrap();
      showSection('contacts');
    }

    function rowsFromListPayload(payload) {
      if (Array.isArray(payload)) return payload;
      if (payload && Array.isArray(payload.rows)) return payload.rows;
      return [];
    }

    async function loadBootstrap() {
      const priorSection = state.currentSection || 'compose';
      const priorContactBucket = document.getElementById('contactStatusBucketFilter') ? document.getElementById('contactStatusBucketFilter').value : '';
      const analyticsSnapshot = document.getElementById('analyticsStopId') ? analyticsFilters() : {};
      const data = await call('/officers/outreach/bootstrap');
      state = {
        ...state,
        ...data,
        contacts: rowsFromListPayload(data.contacts),
        templates: rowsFromListPayload(data.templates),
        stops: rowsFromListPayload(data.stops),
        suppressions: rowsFromListPayload(data.suppressions),
        send_log: rowsFromListPayload(data.send_log),
        currentSection: priorSection,
      };
      fillSummary();
      renderSendReadiness();
      refreshSelects();
      setSelectValueIfPresent('contactStatusBucketFilter', priorContactBucket, '');
      renderContactBucketCounts();
      renderContacts();
      renderTemplates();
      renderStops();
      renderSuppressions();
      renderSendLog();
      if (document.getElementById('analyticsDateFrom')) {
        document.getElementById('analyticsDateFrom').value = analyticsSnapshot.date_from || '';
        document.getElementById('analyticsDateTo').value = analyticsSnapshot.date_to || '';
        document.getElementById('analyticsLocation').value = analyticsSnapshot.location || '';
        document.getElementById('analyticsWorkGroup').value = analyticsSnapshot.work_group || '';
        document.getElementById('analyticsRecipientEmail').value = analyticsSnapshot.recipient_email || '';
        setSelectValueIfPresent('analyticsStopId', analyticsSnapshot.stop_id || '', '');
        setSelectValueIfPresent('analyticsTemplateId', analyticsSnapshot.template_id || '', '');
      }
      try {
        await loadAnalytics();
      } catch (err) {
        setNote('analyticsNote', err.message || String(err), 'error');
      }
      showSection(priorSection);
    }

    document.getElementById('saveContactBtn').addEventListener('click', async () => {
      const result = await runAction('contactFormNote', async () => {
        const payload = contactPayload();
        if (state.selectedContactId) {
          await call(`/officers/outreach/contacts/${state.selectedContactId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        } else {
          await call('/officers/outreach/contacts', { method: 'POST', body: JSON.stringify(payload) });
        }
        clearContactForm();
        await loadBootstrap();
        return true;
      }, 'Saving contact…');
      if (result) setNote('contactFormNote', 'Contact saved.', 'success');
    });

    document.getElementById('deleteContactBtn').addEventListener('click', async () => {
      if (!state.selectedContactId) {
        setNote('contactFormNote', 'Select a contact first.', 'warning');
        return;
      }
      const result = await runAction('contactFormNote', async () => {
        await call(`/officers/outreach/contacts/${state.selectedContactId}`, { method: 'DELETE' });
        clearContactForm();
        await loadBootstrap();
        return true;
      }, 'Deleting contact…');
      if (result) setNote('contactFormNote', 'Contact deleted.', 'success');
    });

    document.getElementById('clearContactBtn').addEventListener('click', clearContactForm);
    document.getElementById('contactStatusBucketFilter').addEventListener('change', renderContacts);
    document.getElementById('contactGroupFilter').addEventListener('input', renderContacts);
    document.getElementById('contactSubgroupFilter').addEventListener('input', renderContacts);
    document.getElementById('composeContactSortField').addEventListener('change', () => {
      refreshSelects();
    });
    document.getElementById('composeContactSortDirection').addEventListener('change', () => {
      refreshSelects();
    });
    document.getElementById('composeContactSearch').addEventListener('input', () => {
      refreshSelects();
    });
    document.getElementById('composeContactGroupFilter').addEventListener('input', () => {
      refreshSelects();
    });
    document.getElementById('composeContactSubgroupFilter').addEventListener('input', () => {
      refreshSelects();
    });

    document.getElementById('inspectImportBtn').addEventListener('click', async () => {
      await runAction('importResult', inspectImportFile, 'Inspecting file…');
    });

    document.getElementById('refreshImportPreviewBtn').addEventListener('click', async () => {
      await runAction('importResult', refreshImportPreview, 'Refreshing import preview…');
    });

    document.getElementById('applyImportBtn').addEventListener('click', async () => {
      await runAction('importResult', applyImportCommit, 'Applying import…');
    });

    document.getElementById('resetImportBtn').addEventListener('click', () => {
      resetImportWizard(true);
    });

    document.getElementById('contactImportSheet').addEventListener('change', async () => {
      if (!state.importInspector) return;
      await runAction('importResult', refreshImportPreview, 'Refreshing import preview…');
    });

    document.getElementById('importStatusMode').addEventListener('change', () => {
      applyStatusModeUi();
    });

    document.getElementById('saveTemplateBtn').addEventListener('click', async () => {
      const result = await runAction('templateFormNote', async () => {
        const payload = templatePayload();
        if (state.selectedTemplateId) {
          await call(`/officers/outreach/templates/${state.selectedTemplateId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        } else {
          await call('/officers/outreach/templates', { method: 'POST', body: JSON.stringify(payload) });
        }
        clearTemplateForm();
        await loadBootstrap();
        return true;
      }, 'Saving template…');
      if (result) setNote('templateFormNote', 'Template saved.', 'success');
    });

    document.getElementById('deleteTemplateBtn').addEventListener('click', async () => {
      if (!state.selectedTemplateId) {
        setNote('templateFormNote', 'Select a template first.', 'warning');
        return;
      }
      const result = await runAction('templateFormNote', async () => {
        await call(`/officers/outreach/templates/${state.selectedTemplateId}`, { method: 'DELETE' });
        clearTemplateForm();
        await loadBootstrap();
        return true;
      }, 'Deleting template…');
      if (result) setNote('templateFormNote', 'Template deleted.', 'success');
    });

    document.getElementById('clearTemplateBtn').addEventListener('click', clearTemplateForm);

    document.getElementById('saveStopBtn').addEventListener('click', async () => {
      const result = await runAction('stopFormNote', async () => {
        const payload = stopPayload();
        if (state.selectedStopId) {
          await call(`/officers/outreach/stops/${state.selectedStopId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        } else {
          await call('/officers/outreach/stops', { method: 'POST', body: JSON.stringify(payload) });
        }
        clearStopForm();
        await loadBootstrap();
        return true;
      }, 'Saving stop…');
      if (result) setNote('stopFormNote', 'Stop saved.', 'success');
    });

    document.getElementById('deleteStopBtn').addEventListener('click', async () => {
      if (!state.selectedStopId) {
        setNote('stopFormNote', 'Select a stop first.', 'warning');
        return;
      }
      const result = await runAction('stopFormNote', async () => {
        await call(`/officers/outreach/stops/${state.selectedStopId}`, { method: 'DELETE' });
        clearStopForm();
        await loadBootstrap();
        return true;
      }, 'Deleting stop…');
      if (result) setNote('stopFormNote', 'Stop deleted.', 'success');
    });

    document.getElementById('clearStopBtn').addEventListener('click', clearStopForm);

    document.getElementById('previewBtn').addEventListener('click', async () => {
      const preview = await runAction('previewMeta', async () => {
        return await call('/officers/outreach/preview', {
          method: 'POST',
          body: JSON.stringify({
            template_id: requiredNumericValue('previewTemplateId', 'Template'),
            stop_id: requiredNumericValue('previewStopId', 'Stop'),
            contact_id: document.getElementById('previewContactId').value ? Number(document.getElementById('previewContactId').value) : null,
            recipient_email: document.getElementById('previewRecipientEmail').value || null,
          }),
        });
      }, 'Rendering preview…');
      if (!preview) return;
      document.getElementById('previewSubjectBox').textContent = preview.subject || '';
      document.getElementById('previewBodyBox').textContent = preview.text_body || '';
      document.getElementById('previewHtmlFrame').srcdoc = preview.html_body || '';
      setNote(
        'previewMeta',
        preview.missing_fields && preview.missing_fields.length
          ? `Preview rendered. Unknown placeholders: ${preview.missing_fields.join(', ')}`
          : 'Preview rendered successfully.',
        preview.missing_fields && preview.missing_fields.length ? 'warning' : 'success',
      );
    });

    document.getElementById('testSendBtn').addEventListener('click', async () => {
      const result = await runAction('previewMeta', async () => {
        return await call('/officers/outreach/test-send', {
          method: 'POST',
          body: JSON.stringify({
            template_id: requiredNumericValue('previewTemplateId', 'Template'),
            stop_id: requiredNumericValue('previewStopId', 'Stop'),
            contact_id: document.getElementById('previewContactId').value ? Number(document.getElementById('previewContactId').value) : null,
            recipient_email: document.getElementById('previewRecipientEmail').value,
          }),
        });
      }, 'Sending test…');
      if (!result) return;
      setNote('previewMeta', describeSendResult('Test send', result), result.status === 'failed' ? 'error' : 'success');
      await loadBootstrap();
      showSection('compose');
    });

    document.getElementById('quickPreviewBtn').addEventListener('click', async () => {
      const preview = await runAction('quickMessageMeta', async () => {
        return await call('/officers/outreach/quick-preview', {
          method: 'POST',
          body: JSON.stringify({
            stop_id: requiredNumericValue('quickMessageStopId', 'Stop'),
            recipient_email: document.getElementById('quickMessageRecipientEmail').value,
            subject_template: document.getElementById('quickMessageSubject').value,
            body_template: document.getElementById('quickMessageBody').value,
          }),
        });
      }, 'Rendering quick preview…');
      if (!preview) return;
      document.getElementById('quickMessageSubjectBox').textContent = preview.subject || '';
      document.getElementById('quickMessageBodyBox').textContent = preview.text_body || '';
      document.getElementById('quickMessageHtmlFrame').srcdoc = preview.html_body || '';
      setNote(
        'quickMessageMeta',
        preview.missing_fields && preview.missing_fields.length
          ? `Quick preview rendered. Unknown placeholders: ${preview.missing_fields.join(', ')}`
          : 'Quick test preview rendered successfully.',
        preview.missing_fields && preview.missing_fields.length ? 'warning' : 'success',
      );
    });

    document.getElementById('quickSendBtn').addEventListener('click', async () => {
      const result = await runAction('quickMessageMeta', async () => {
        return await call('/officers/outreach/quick-test-send', {
          method: 'POST',
          body: JSON.stringify({
            stop_id: requiredNumericValue('quickMessageStopId', 'Stop'),
            recipient_email: document.getElementById('quickMessageRecipientEmail').value,
            subject_template: document.getElementById('quickMessageSubject').value,
            body_template: document.getElementById('quickMessageBody').value,
          }),
        });
      }, 'Sending quick test…');
      if (!result) return;
      setNote('quickMessageMeta', describeSendResult('Quick test send', result), result.status === 'failed' ? 'error' : 'success');
      await loadBootstrap();
      showSection('compose');
    });

    document.getElementById('oneOffPreviewBtn').addEventListener('click', async () => {
      const preview = await runAction('oneOffMeta', async () => {
        return await call('/officers/outreach/preview', {
          method: 'POST',
          body: JSON.stringify({
            template_id: requiredNumericValue('oneOffTemplateId', 'Template'),
            stop_id: requiredNumericValue('oneOffStopId', 'Stop'),
            contact_id: document.getElementById('oneOffContactId').value ? Number(document.getElementById('oneOffContactId').value) : null,
            recipient_email: document.getElementById('oneOffRecipientEmail').value,
            manual_contact: oneOffManualContactPayload(),
          }),
        });
      }, 'Rendering one-off preview…');
      if (!preview) return;
      document.getElementById('oneOffSubjectBox').textContent = preview.subject || '';
      document.getElementById('oneOffBodyBox').textContent = preview.text_body || '';
      document.getElementById('oneOffHtmlFrame').srcdoc = preview.html_body || '';
      setNote(
        'oneOffMeta',
        preview.missing_fields && preview.missing_fields.length
          ? `One-off preview rendered. Unknown placeholders: ${preview.missing_fields.join(', ')}`
          : 'One-off preview rendered successfully.',
        preview.missing_fields && preview.missing_fields.length ? 'warning' : 'success',
      );
    });

    document.getElementById('oneOffSendBtn').addEventListener('click', async () => {
      const result = await runAction('oneOffMeta', async () => {
        return await call('/officers/outreach/one-off-send', {
          method: 'POST',
          body: JSON.stringify({
            template_id: requiredNumericValue('oneOffTemplateId', 'Template'),
            stop_id: requiredNumericValue('oneOffStopId', 'Stop'),
            contact_id: document.getElementById('oneOffContactId').value ? Number(document.getElementById('oneOffContactId').value) : null,
            recipient_email: document.getElementById('oneOffRecipientEmail').value,
            manual_contact: oneOffManualContactPayload(),
          }),
        });
      }, 'Sending one-off email…');
      if (!result) return;
      setNote('oneOffMeta', describeSendResult('One-off send', result), result.status === 'failed' ? 'error' : 'success');
      await loadBootstrap();
      showSection('compose');
    });

    document.getElementById('oneOffClearBtn').addEventListener('click', clearOneOffForm);
    document.getElementById('oneOffContactId').addEventListener('change', () => {
      const contactId = document.getElementById('oneOffContactId').value;
      if (!contactId) return;
      const row = (state.contacts || []).find((item) => item.id === Number(contactId));
      if (!row) return;
      document.getElementById('oneOffRecipientEmail').value = row.email || '';
      document.getElementById('oneOffFirstName').value = row.first_name || '';
      document.getElementById('oneOffLastName').value = row.last_name || '';
      document.getElementById('oneOffFullName').value = row.full_name || '';
      document.getElementById('oneOffWorkLocation').value = row.work_location || '';
      document.getElementById('oneOffWorkGroup').value = row.work_group || '';
      document.getElementById('oneOffGroupName').value = row.group_name || '';
      document.getElementById('oneOffSubgroupName').value = row.subgroup_name || '';
      document.getElementById('oneOffDepartment').value = row.department || '';
      document.getElementById('oneOffBargainingUnit').value = row.bargaining_unit || '';
      document.getElementById('oneOffLocalNumber').value = row.local_number || '';
      document.getElementById('oneOffStewardName').value = row.steward_name || '';
      document.getElementById('oneOffRepName').value = row.rep_name || '';
      document.getElementById('oneOffExtraFields').value = JSON.stringify(row.extra_fields || {}, null, 2);
    });

    document.getElementById('runDueBtn').addEventListener('click', async () => {
      const result = await runAction('runDueNote', async () => {
        return await call('/officers/outreach/run-due', { method: 'POST', body: '{}' });
      }, 'Running due sends…');
      if (!result) return;
      const failedRows = (result.rows || []).filter((row) => row.status === 'failed' && row.error_text);
      setNote(
        'runDueNote',
        `Processed ${result.processed_count || 0}, sent ${result.sent_count || 0}, failed ${result.failed_count || 0}, skipped suppressed ${result.skipped_suppressed_count || 0}, skipped existing ${result.skipped_existing_count || 0}${failedRows.length ? `. Latest failure: ${failedRows[0].error_text}` : ''}`,
        result.failed_count ? 'warning' : 'success',
      );
      await loadBootstrap();
    });

    document.getElementById('refreshBtn').addEventListener('click', async () => {
      const result = await runAction('runDueNote', async () => {
        await loadBootstrap();
        return true;
      }, 'Refreshing outreach data…');
      if (result) setNote('runDueNote', 'Outreach data refreshed.', 'success');
    });

    document.querySelectorAll('[data-target-section]').forEach((button) => {
      button.addEventListener('click', () => showSection(button.dataset.targetSection));
    });

    document.getElementById('analyticsApplyBtn').addEventListener('click', async () => {
      await runAction('analyticsNote', loadAnalytics, 'Loading analytics…');
    });

    document.getElementById('analyticsResetBtn').addEventListener('click', async () => {
      ['analyticsDateFrom','analyticsDateTo','analyticsLocation','analyticsWorkGroup','analyticsRecipientEmail'].forEach((id) => {
        document.getElementById(id).value = '';
      });
      document.getElementById('analyticsStopId').value = '';
      document.getElementById('analyticsTemplateId').value = '';
      await runAction('analyticsNote', loadAnalytics, 'Loading analytics…');
    });

    document.getElementById('analyticsExportSummaryBtn').addEventListener('click', () => {
      window.location.href = `/officers/outreach/analytics/export/summary.csv${queryStringFromFilters(analyticsFilters())}`;
    });
    document.getElementById('analyticsExportClicksBtn').addEventListener('click', () => {
      window.location.href = `/officers/outreach/analytics/export/clicks.csv${queryStringFromFilters(analyticsFilters())}`;
    });
    document.getElementById('analyticsExportSendsBtn').addEventListener('click', () => {
      window.location.href = `/officers/outreach/analytics/export/send-history.csv${queryStringFromFilters(analyticsFilters())}`;
    });
    document.getElementById('analyticsExportSuppressionsBtn').addEventListener('click', () => {
      window.location.href = '/officers/outreach/analytics/export/suppressions.csv';
    });

    window.addEventListener('error', (event) => {
      const message = event.error ? event.error.message : event.message;
      setNote('runDueNote', message || 'Unexpected browser error', 'error');
    });

    window.addEventListener('unhandledrejection', (event) => {
      const reason = event.reason;
      const message = reason && reason.message ? reason.message : String(reason || 'Unhandled promise rejection');
      setNote('runDueNote', message, 'error');
    });

    showSection(CURRENT_SECTION);
    resetImportWizard(false);
    resetQuickMessage();
    clearOneOffForm();
    loadBootstrap().catch((err) => {
      setNote('runDueNote', err.message || String(err), 'error');
    });
  </script>
</body>
</html>"""
    rendered = rendered.replace("__OUTREACH_PAGE_TITLE__", html.escape(f'{page["title"]} | Outreach Mail Console'))
    rendered = rendered.replace("__OUTREACH_PAGE_HEADING__", html.escape(page["title"]))
    rendered = rendered.replace("__OUTREACH_PAGE_DESCRIPTION__", html.escape(page["description"]))
    rendered = rendered.replace("__OUTREACH_NAV_LINKS__", _outreach_nav_links(page_name))
    rendered = rendered.replace("__OUTREACH_CURRENT_SECTION__", current_section)
    rendered = rendered.replace(
        f'class="panel route-section" data-section="{current_section}"',
        f'class="panel route-section active" data-section="{current_section}"',
    )
    rendered = rendered.replace(
        f'class="grid route-section" data-section="{current_section}"',
        f'class="grid route-section active" data-section="{current_section}"',
    )
    return rendered
