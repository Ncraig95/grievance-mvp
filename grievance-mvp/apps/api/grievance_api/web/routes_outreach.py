from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from ..services.outreach_service import OutreachRenderedMessage, OutreachService
from .officer_auth import require_admin_user, require_ops_page_access
from .outreach_models import (
    OutreachContactListResponse,
    OutreachContactRow,
    OutreachContactUpsertRequest,
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
)

router = APIRouter()


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


@router.get("/officers/outreach", response_class=HTMLResponse)
async def outreach_page(request: Request):
    gate = await require_ops_page_access(request, next_path="/officers/outreach")
    if isinstance(gate, RedirectResponse):
        return gate
    return HTMLResponse(
        _render_outreach_page(),
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
        row = await service.save_contact(contact_id=None, payload=body.model_dump())
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachContactRow(**row)


@router.patch("/officers/outreach/contacts/{contact_id}", response_model=OutreachContactRow)
async def outreach_update_contact(contact_id: int, body: OutreachContactUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_contact(contact_id=contact_id, payload=body.model_dump())
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachContactRow(**row)


@router.delete("/officers/outreach/contacts/{contact_id}")
async def outreach_delete_contact(contact_id: int, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    await _service(request).delete_contact(contact_id)
    return {"deleted": True, "contact_id": int(contact_id)}


@router.post("/officers/outreach/contacts/import", response_model=OutreachImportResponse)
async def outreach_import_contacts(body: OutreachImportRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        result = await service.import_contacts(filename=body.filename, content_base64=body.content_base64)
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
        row = await service.save_template(template_id=None, payload=body.model_dump())
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachTemplateRow(**row)


@router.patch("/officers/outreach/templates/{template_id}", response_model=OutreachTemplateRow)
async def outreach_update_template(template_id: int, body: OutreachTemplateUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_template(template_id=template_id, payload=body.model_dump())
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
        row = await service.save_stop(stop_id=None, payload=body.model_dump())
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachStopRow(**row)


@router.patch("/officers/outreach/stops/{stop_id}", response_model=OutreachStopRow)
async def outreach_update_stop(stop_id: int, body: OutreachStopUpsertRequest, request: Request):
    await require_admin_user(request, allow_local_fallback=True)
    service = _service(request)
    try:
        row = await service.save_stop(stop_id=stop_id, payload=body.model_dump())
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
                manual_contact=body.manual_contact.model_dump(),
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
            manual_contact=body.manual_contact.model_dump() if body.manual_contact is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachSendResult(
        send_log_id=result.send_log_id,
        recipient_email=result.recipient_email,
        status=result.status,
        graph_message_id=result.graph_message_id,
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
            manual_contact=body.manual_contact.model_dump() if body.manual_contact is not None else None,
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
            manual_contact=body.manual_contact.model_dump() if body.manual_contact is not None else None,
        )
    except RuntimeError as exc:
        raise _handle_runtime_error(exc) from exc
    return OutreachSendResult(
        send_log_id=result.send_log_id,
        recipient_email=result.recipient_email,
        status=result.status,
        graph_message_id=result.graph_message_id,
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


def _render_outreach_page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Outreach Mail Console</title>
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
    .hero p { margin: 0; max-width: 72ch; line-height: 1.5; }
    .nav-tabs {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 18px 0;
    }
    .nav-tabs button {
      width: auto;
      min-width: 120px;
      background: white;
      color: var(--text);
      border-color: var(--border);
    }
    .nav-tabs button.active {
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }
    .route-section { display: none; }
    .route-section.active { display: block; }
    .route-section.grid.active { display: grid; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
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
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
    }
    .stat {
      background: white;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
    }
    .stat strong { display: block; font-size: 28px; margin-bottom: 4px; }
    .muted { color: var(--muted); }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
    }
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
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
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
    }
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
    }
    .error { color: var(--danger); }
    .quick-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
    }
    @media (max-width: 720px) {
      body { padding: 14px; }
      .hero h1 { font-size: 28px; }
      .actions button { width: 100%; }
      .nav-tabs button { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <h1>Outreach Mail Console</h1>
      <p>Use the navigation below instead of one long admin page. Compose handles quick sends, Contacts is for your recipient list, Stops manages visit schedules, and Analytics/History show results.</p>
    </section>

    <section class="nav-tabs panel">
      <button type="button" class="active" data-target-section="compose">Compose</button>
      <button type="button" data-target-section="home">Overview</button>
      <button type="button" data-target-section="contacts">Contacts</button>
      <button type="button" data-target-section="templates">Templates</button>
      <button type="button" data-target-section="stops">Stops</button>
      <button type="button" data-target-section="analytics">Analytics</button>
      <button type="button" data-target-section="history">History</button>
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
      <div class="note" id="analyticsNote">
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
        <h2>Contacts</h2>
        <div class="form-grid">
          <div><label>Email<input id="contactEmail" /></label></div>
          <div><label>First Name<input id="contactFirstName" /></label></div>
          <div><label>Last Name<input id="contactLastName" /></label></div>
          <div><label>Full Name<input id="contactFullName" /></label></div>
          <div><label>Work Location<input id="contactLocation" /></label></div>
          <div><label>Work Group<input id="contactWorkGroup" /></label></div>
          <div><label>Department<input id="contactDepartment" /></label></div>
          <div><label>Bargaining Unit<input id="contactBargainingUnit" /></label></div>
          <div><label>Local Number<input id="contactLocalNumber" /></label></div>
          <div><label>Steward Name<input id="contactStewardName" /></label></div>
          <div><label>Rep Name<input id="contactRepName" /></label></div>
          <div><label>Source<input id="contactSource" placeholder="manual, csv, xlsx" /></label></div>
          <div><label>Active<select id="contactActive"><option value="true">Active</option><option value="false">Inactive</option></select></label></div>
        </div>
        <label style="margin-top:10px;">Notes<textarea id="contactNotes"></textarea></label>
        <label>Extra Fields JSON<textarea id="contactExtraFields" placeholder='{"assigned_officer":"Nick Craig"}'></textarea></label>
        <div class="actions">
          <button id="saveContactBtn" type="button">Save Contact</button>
          <button id="clearContactBtn" class="secondary" type="button">Clear Form</button>
          <button id="deleteContactBtn" class="danger" type="button">Delete Selected</button>
        </div>
        <div class="note">
          <input id="contactImportFile" type="file" accept=".csv,.xlsx" />
          <button id="importContactsBtn" type="button" style="margin-top:10px;">Import CSV or XLSX</button>
          <div id="importResult" class="muted" style="margin-top:8px;"></div>
        </div>
      </section>
    </section>

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
      <div class="note" id="placeholderList"></div>
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
    </section>

    <section class="panel route-section active" data-section="compose">
      <h2>Quick Test Message</h2>
      <div class="note">Fastest path for testing: type a subject and message here, leave the recipient as <strong>ncraig@cwa3106.com</strong>, preview it, then send a test. Merge fields still work if you use them.</div>
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

    <section class="panel route-section active" data-section="compose">
      <h2>Preview and Test Send</h2>
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

    <section class="panel route-section active" data-section="compose">
      <h2>One-Off Live Send</h2>
      <div class="note">Use this when you need to send one real outreach email right now without importing a CSV/XLSX first. This creates a tracked outreach message with unsubscribe support for one recipient.</div>
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

    <section class="panel route-section" data-section="contacts">
      <h2>Contacts Table</h2>
      <div class="table-wrap"><table id="contactsTable"></table></div>
    </section>

    <section class="panel route-section" data-section="templates">
      <h2>Templates Table</h2>
      <div class="table-wrap"><table id="templatesTable"></table></div>
    </section>

    <section class="panel route-section" data-section="stops">
      <h2>Stops Table</h2>
      <div class="table-wrap"><table id="stopsTable"></table></div>
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
    let state = {
      contacts: [],
      templates: [],
      stops: [],
      suppressions: [],
      send_log: [],
      summary: {},
      analytics: { totals: {}, campaigns: [], top_links: [], recent_activity: [], notes: [] },
      placeholder_catalog: [],
      selectedContactId: null,
      selectedTemplateId: null,
      selectedStopId: null,
      currentSection: 'compose',
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

    function jsonOrEmpty(text) {
      try { return text.trim() ? JSON.parse(text) : {}; } catch (err) { throw new Error('Extra fields JSON must be valid JSON'); }
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
      const grid = document.getElementById('summaryGrid');
      const summary = state.summary || {};
      const items = [
        ['Sent', summary.sent_count || 0],
        ['Failed', summary.failed_count || 0],
        ['Suppressed', summary.suppressed_count || 0],
        ['Stops', summary.stop_count || 0],
        ['Active Contacts', summary.active_contact_count || 0],
      ];
      grid.innerHTML = items.map(([label, value]) => `<div class="stat"><strong>${value}</strong><span class="muted">${label}</span></div>`).join('');
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
      document.getElementById('analyticsGrid').innerHTML = metrics.map(([label, value]) => `<div class="stat"><strong>${value}</strong><span class="muted">${label}</span></div>`).join('');
      if (Array.isArray(analytics.notes) && analytics.notes.length) {
        document.getElementById('analyticsNote').textContent = analytics.notes.join(' ');
      }

      const campaignsHeader = '<tr><th>Location</th><th>Visit</th><th>Sent</th><th>Failed</th><th>Suppressed</th><th>Unsubscribes</th><th>Unique Opens</th><th>Unique Clicks</th></tr>';
      const campaignsRows = (analytics.campaigns || []).map((row) => `
        <tr>
          <td>${row.location_name || ''}</td>
          <td>${row.visit_date_local || ''}</td>
          <td>${row.sent_count || 0}</td>
          <td>${row.failed_count || 0}</td>
          <td>${row.suppressed_count || 0}</td>
          <td>${row.unsubscribe_count || 0}</td>
          <td>${row.unique_estimated_open_count || 0}</td>
          <td>${row.unique_click_count || 0}</td>
        </tr>`).join('');
      document.getElementById('analyticsCampaignsTable').innerHTML = campaignsHeader + campaignsRows;

      const linksHeader = '<tr><th>Destination</th><th>Clicks</th><th>Unique Clicks</th></tr>';
      const linksRows = (analytics.top_links || []).map((row) => `
        <tr>
          <td><a href="${row.destination_url}" target="_blank" rel="noreferrer">${row.destination_url}</a></td>
          <td>${row.click_count || 0}</td>
          <td>${row.unique_click_count || 0}</td>
        </tr>`).join('');
      document.getElementById('analyticsTopLinksTable').innerHTML = linksHeader + linksRows;

      const activityHeader = '<tr><th>Time</th><th>Type</th><th>Recipient</th><th>Location</th><th>Detail</th><th>Flag</th></tr>';
      const activityRows = (analytics.recent_activity || []).map((row) => `
        <tr>
          <td>${row.occurred_at_utc || ''}</td>
          <td>${row.event_type || ''}</td>
          <td>${row.recipient_email || ''}</td>
          <td>${row.location_name || ''}</td>
          <td>${row.destination_url || row.automation_reason || ''}</td>
          <td>${row.suspected_automation ? '<span class="pill">Flagged</span>' : ''}</td>
        </tr>`).join('');
      document.getElementById('analyticsActivityTable').innerHTML = activityHeader + activityRows;
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
        notice_subject: document.getElementById('stopNoticeSubject').value,
        reminder_subject: document.getElementById('stopReminderSubject').value,
        notice_send_at_local: document.getElementById('stopNoticeSendLocal').value || null,
        reminder_send_at_local: document.getElementById('stopReminderSendLocal').value || null,
        status: document.getElementById('stopStatus').value,
      };
    }

    function renderContacts() {
      const table = document.getElementById('contactsTable');
      const header = '<tr><th>Email</th><th>Name</th><th>Location</th><th>Work Group</th><th>Department</th><th>Source</th><th>Status</th></tr>';
      const rows = state.contacts.map((row) => `
        <tr data-contact-id="${row.id}">
          <td>${row.email}</td>
          <td>${row.full_name || ''}</td>
          <td>${row.work_location || ''}</td>
          <td>${row.work_group || ''}</td>
          <td>${row.department || ''}</td>
          <td>${row.source || ''}</td>
          <td><span class="pill">${row.active ? 'Active' : 'Inactive'}</span></td>
        </tr>`).join('');
      table.innerHTML = header + rows;
      table.querySelectorAll('tr[data-contact-id]').forEach((tr) => {
        tr.addEventListener('click', () => selectContact(Number(tr.dataset.contactId)));
      });
    }

    function renderTemplates() {
      const table = document.getElementById('templatesTable');
      const header = '<tr><th>Name</th><th>Key</th><th>Type</th><th>Status</th><th>Seeded</th></tr>';
      const rows = state.templates.map((row) => `
        <tr data-template-id="${row.id}">
          <td>${row.name}</td>
          <td>${row.template_key}</td>
          <td>${row.template_type}</td>
          <td><span class="pill">${row.active ? 'Active' : 'Inactive'}</span></td>
          <td>${row.seeded ? 'Yes' : 'No'}</td>
        </tr>`).join('');
      table.innerHTML = header + rows;
      table.querySelectorAll('tr[data-template-id]').forEach((tr) => {
        tr.addEventListener('click', () => selectTemplate(Number(tr.dataset.templateId)));
      });
    }

    function renderStops() {
      const table = document.getElementById('stopsTable');
      const header = '<tr><th>Location</th><th>Visit</th><th>Audience</th><th>Notice Send</th><th>Reminder Send</th><th>Status</th></tr>';
      const rows = state.stops.map((row) => `
        <tr data-stop-id="${row.id}">
          <td>${row.location_name}</td>
          <td>${row.visit_date_local} ${row.start_time_local}-${row.end_time_local}</td>
          <td>${row.audience_location || ''}${row.audience_work_group ? ' / ' + row.audience_work_group : ''}</td>
          <td>${row.notice_send_at_local}</td>
          <td>${row.reminder_send_at_local}</td>
          <td><span class="pill">${row.status}</span></td>
        </tr>`).join('');
      table.innerHTML = header + rows;
      table.querySelectorAll('tr[data-stop-id]').forEach((tr) => {
        tr.addEventListener('click', () => selectStop(Number(tr.dataset.stopId)));
      });
    }

    function renderSuppressions() {
      const table = document.getElementById('suppressionsTable');
      const header = '<tr><th>Email</th><th>Reason</th><th>Created</th><th></th></tr>';
      const rows = state.suppressions.map((row) => `
        <tr>
          <td>${row.email}</td>
          <td>${row.reason}</td>
          <td>${row.created_at_utc}</td>
          <td><button class="danger" data-unsuppress-id="${row.id}" type="button">Remove</button></td>
        </tr>`).join('');
      table.innerHTML = header + rows;
      table.querySelectorAll('button[data-unsuppress-id]').forEach((button) => {
        button.addEventListener('click', async () => {
          await call(`/officers/outreach/suppressions/${button.dataset.unsuppressId}`, { method: 'DELETE' });
          await loadBootstrap();
        });
      });
    }

    function renderSendLog() {
      const table = document.getElementById('sendLogTable');
      const header = '<tr><th>Recipient</th><th>Type</th><th>Subject</th><th>Status</th><th>Stop</th><th>Sent</th><th>Error</th></tr>';
      const rows = state.send_log.map((row) => `
        <tr>
          <td>${row.recipient_email}</td>
          <td>${row.email_type}</td>
          <td>${row.subject}</td>
          <td><span class="pill">${row.status}</span></td>
          <td>${row.location_name || ''} ${row.visit_date_local || ''}</td>
          <td>${row.sent_at_utc || ''}</td>
          <td class="error">${row.error_text || ''}</td>
        </tr>`).join('');
      table.innerHTML = header + rows;
    }

    function refreshSelects() {
      const contactOptions = ['<option value="">No saved contact</option>'].concat(
        state.contacts.map((row) => `<option value="${row.id}">${row.full_name || row.email}</option>`)
      ).join('');
      const templateOptions = state.templates.map((row) => `<option value="${row.id}">${row.name}</option>`).join('');
      const stopOptions = state.stops.map((row) => `<option value="${row.id}">${row.location_name} ${row.visit_date_local}</option>`).join('');
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
      setSelectValueIfPresent('analyticsTemplateId', '', '');
      setSelectValueIfPresent('analyticsStopId', '', '');
    }

    function selectContact(contactId) {
      state.selectedContactId = contactId;
      const row = state.contacts.find((item) => item.id === contactId);
      if (!row) return;
      document.getElementById('contactEmail').value = row.email || '';
      document.getElementById('contactFirstName').value = row.first_name || '';
      document.getElementById('contactLastName').value = row.last_name || '';
      document.getElementById('contactFullName').value = row.full_name || '';
      document.getElementById('contactLocation').value = row.work_location || '';
      document.getElementById('contactWorkGroup').value = row.work_group || '';
      document.getElementById('contactDepartment').value = row.department || '';
      document.getElementById('contactBargainingUnit').value = row.bargaining_unit || '';
      document.getElementById('contactLocalNumber').value = row.local_number || '';
      document.getElementById('contactStewardName').value = row.steward_name || '';
      document.getElementById('contactRepName').value = row.rep_name || '';
      document.getElementById('contactActive').value = row.active ? 'true' : 'false';
      document.getElementById('contactNotes').value = row.notes || '';
      document.getElementById('contactSource').value = row.source || '';
      document.getElementById('contactExtraFields').value = JSON.stringify(row.extra_fields || {}, null, 2);
      document.getElementById('previewContactId').value = String(row.id);
      document.getElementById('oneOffContactId').value = String(row.id);
    }

    function selectTemplate(templateId) {
      state.selectedTemplateId = templateId;
      const row = state.templates.find((item) => item.id === templateId);
      if (!row) return;
      document.getElementById('templateKey').value = row.template_key || '';
      document.getElementById('templateName').value = row.name || '';
      document.getElementById('templateType').value = row.template_type || 'notice';
      document.getElementById('templateSubject').value = row.subject_template || '';
      document.getElementById('templateBody').value = row.body_template || '';
      document.getElementById('templateActive').value = row.active ? 'true' : 'false';
      document.getElementById('previewTemplateId').value = String(row.id);
      document.getElementById('oneOffTemplateId').value = String(row.id);
    }

    function selectStop(stopId) {
      state.selectedStopId = stopId;
      const row = state.stops.find((item) => item.id === stopId);
      if (!row) return;
      document.getElementById('stopLocationName').value = row.location_name || '';
      document.getElementById('stopVisitDate').value = row.visit_date_local || '';
      document.getElementById('stopStartTime').value = row.start_time_local || '';
      document.getElementById('stopEndTime').value = row.end_time_local || '';
      document.getElementById('stopTimezone').value = row.timezone || 'America/New_York';
      document.getElementById('stopAudienceLocation').value = row.audience_location || '';
      document.getElementById('stopAudienceWorkGroup').value = row.audience_work_group || '';
      document.getElementById('stopNoticeSubject').value = row.notice_subject || '';
      document.getElementById('stopReminderSubject').value = row.reminder_subject || '';
      document.getElementById('stopNoticeSendLocal').value = row.notice_send_at_local || '';
      document.getElementById('stopReminderSendLocal').value = row.reminder_send_at_local || '';
      document.getElementById('stopStatus').value = row.status || 'draft';
      document.getElementById('previewStopId').value = String(row.id);
      document.getElementById('quickMessageStopId').value = String(row.id);
      document.getElementById('oneOffStopId').value = String(row.id);
    }

    function clearContactForm() {
      state.selectedContactId = null;
      ['contactEmail','contactFirstName','contactLastName','contactFullName','contactLocation','contactWorkGroup','contactDepartment','contactBargainingUnit','contactLocalNumber','contactStewardName','contactRepName','contactNotes','contactSource','contactExtraFields'].forEach((id) => document.getElementById(id).value = '');
      document.getElementById('contactActive').value = 'true';
    }

    function clearTemplateForm() {
      state.selectedTemplateId = null;
      ['templateKey','templateName','templateSubject','templateBody'].forEach((id) => document.getElementById(id).value = '');
      document.getElementById('templateType').value = 'notice';
      document.getElementById('templateActive').value = 'true';
    }

    function clearStopForm() {
      state.selectedStopId = null;
      ['stopLocationName','stopVisitDate','stopStartTime','stopEndTime','stopAudienceLocation','stopAudienceWorkGroup','stopNoticeSubject','stopReminderSubject','stopNoticeSendLocal','stopReminderSendLocal'].forEach((id) => document.getElementById(id).value = '');
      document.getElementById('stopTimezone').value = 'America/New_York';
      document.getElementById('stopStatus').value = 'draft';
    }

    function oneOffManualContactPayload() {
      return {
        first_name: document.getElementById('oneOffFirstName').value || null,
        last_name: document.getElementById('oneOffLastName').value || null,
        full_name: document.getElementById('oneOffFullName').value || null,
        work_location: document.getElementById('oneOffWorkLocation').value || null,
        work_group: document.getElementById('oneOffWorkGroup').value || null,
        department: document.getElementById('oneOffDepartment').value || null,
        bargaining_unit: document.getElementById('oneOffBargainingUnit').value || null,
        local_number: document.getElementById('oneOffLocalNumber').value || null,
        steward_name: document.getElementById('oneOffStewardName').value || null,
        rep_name: document.getElementById('oneOffRepName').value || null,
        extra_fields: jsonOrEmpty(document.getElementById('oneOffExtraFields').value),
      };
    }

    function clearOneOffForm() {
      ['oneOffFirstName','oneOffLastName','oneOffFullName','oneOffWorkLocation','oneOffWorkGroup','oneOffDepartment','oneOffBargainingUnit','oneOffLocalNumber','oneOffStewardName','oneOffRepName','oneOffExtraFields'].forEach((id) => document.getElementById(id).value = '');
      document.getElementById('oneOffRecipientEmail').value = 'ncraig@cwa3106.com';
      document.getElementById('oneOffContactId').value = '';
      document.getElementById('oneOffMeta').textContent = 'This sends one live message immediately. Use the normal Preview/Test Send panel for non-live testing.';
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
      document.getElementById('quickMessageMeta').textContent = 'Quick test preview and send status will appear below.';
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

    async function loadBootstrap() {
      const data = await call('/officers/outreach/bootstrap');
      state = { ...state, ...data };
      fillSummary();
      renderContacts();
      renderTemplates();
      renderStops();
      renderSuppressions();
      renderSendLog();
      refreshSelects();
      await loadAnalytics();
      showSection(state.currentSection || 'compose');
    }

    document.getElementById('saveContactBtn').addEventListener('click', async () => {
      const payload = contactPayload();
      if (state.selectedContactId) {
        await call(`/officers/outreach/contacts/${state.selectedContactId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        await call('/officers/outreach/contacts', { method: 'POST', body: JSON.stringify(payload) });
      }
      clearContactForm();
      await loadBootstrap();
    });

    document.getElementById('deleteContactBtn').addEventListener('click', async () => {
      if (!state.selectedContactId) return;
      await call(`/officers/outreach/contacts/${state.selectedContactId}`, { method: 'DELETE' });
      clearContactForm();
      await loadBootstrap();
    });

    document.getElementById('clearContactBtn').addEventListener('click', clearContactForm);

    document.getElementById('importContactsBtn').addEventListener('click', async () => {
      const file = document.getElementById('contactImportFile').files[0];
      if (!file) throw new Error('Choose a CSV or XLSX file first');
      const content_base64 = await fileToBase64(file);
      const result = await call('/officers/outreach/contacts/import', {
        method: 'POST',
        body: JSON.stringify({ filename: file.name, content_base64 }),
      });
      document.getElementById('importResult').textContent = `Imported ${result.imported_count}, updated ${result.updated_count}, skipped ${result.skipped_count}${result.errors.length ? ' with errors: ' + result.errors.join(' | ') : ''}`;
      await loadBootstrap();
    });

    document.getElementById('saveTemplateBtn').addEventListener('click', async () => {
      const payload = templatePayload();
      if (state.selectedTemplateId) {
        await call(`/officers/outreach/templates/${state.selectedTemplateId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        await call('/officers/outreach/templates', { method: 'POST', body: JSON.stringify(payload) });
      }
      clearTemplateForm();
      await loadBootstrap();
    });

    document.getElementById('deleteTemplateBtn').addEventListener('click', async () => {
      if (!state.selectedTemplateId) return;
      await call(`/officers/outreach/templates/${state.selectedTemplateId}`, { method: 'DELETE' });
      clearTemplateForm();
      await loadBootstrap();
    });

    document.getElementById('clearTemplateBtn').addEventListener('click', clearTemplateForm);

    document.getElementById('saveStopBtn').addEventListener('click', async () => {
      const payload = stopPayload();
      if (state.selectedStopId) {
        await call(`/officers/outreach/stops/${state.selectedStopId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        await call('/officers/outreach/stops', { method: 'POST', body: JSON.stringify(payload) });
      }
      clearStopForm();
      await loadBootstrap();
    });

    document.getElementById('deleteStopBtn').addEventListener('click', async () => {
      if (!state.selectedStopId) return;
      await call(`/officers/outreach/stops/${state.selectedStopId}`, { method: 'DELETE' });
      clearStopForm();
      await loadBootstrap();
    });

    document.getElementById('clearStopBtn').addEventListener('click', clearStopForm);

    document.getElementById('previewBtn').addEventListener('click', async () => {
      const preview = await call('/officers/outreach/preview', {
        method: 'POST',
        body: JSON.stringify({
          template_id: requiredNumericValue('previewTemplateId', 'Template'),
          stop_id: requiredNumericValue('previewStopId', 'Stop'),
          contact_id: document.getElementById('previewContactId').value ? Number(document.getElementById('previewContactId').value) : null,
          recipient_email: document.getElementById('previewRecipientEmail').value || null,
        }),
      });
      document.getElementById('previewSubjectBox').textContent = preview.subject;
      document.getElementById('previewBodyBox').textContent = preview.text_body;
      document.getElementById('previewHtmlFrame').srcdoc = preview.html_body || '';
      document.getElementById('previewMeta').textContent = preview.missing_fields.length
        ? 'Unknown placeholders: ' + preview.missing_fields.join(', ')
        : 'Preview rendered successfully.';
    });

    document.getElementById('testSendBtn').addEventListener('click', async () => {
      const result = await call('/officers/outreach/test-send', {
        method: 'POST',
        body: JSON.stringify({
          template_id: requiredNumericValue('previewTemplateId', 'Template'),
          stop_id: requiredNumericValue('previewStopId', 'Stop'),
          contact_id: document.getElementById('previewContactId').value ? Number(document.getElementById('previewContactId').value) : null,
          recipient_email: document.getElementById('previewRecipientEmail').value,
        }),
      });
      document.getElementById('previewMeta').textContent = `Test send status: ${result.status} to ${result.recipient_email}`;
      await loadBootstrap();
    });

    document.getElementById('quickPreviewBtn').addEventListener('click', async () => {
      const preview = await call('/officers/outreach/quick-preview', {
        method: 'POST',
        body: JSON.stringify({
          stop_id: requiredNumericValue('quickMessageStopId', 'Stop'),
          recipient_email: document.getElementById('quickMessageRecipientEmail').value,
          subject_template: document.getElementById('quickMessageSubject').value,
          body_template: document.getElementById('quickMessageBody').value,
        }),
      });
      document.getElementById('quickMessageSubjectBox').textContent = preview.subject;
      document.getElementById('quickMessageBodyBox').textContent = preview.text_body;
      document.getElementById('quickMessageHtmlFrame').srcdoc = preview.html_body || '';
      document.getElementById('quickMessageMeta').textContent = preview.missing_fields.length
        ? 'Unknown placeholders: ' + preview.missing_fields.join(', ')
        : 'Quick test preview rendered successfully.';
    });

    document.getElementById('quickSendBtn').addEventListener('click', async () => {
      const result = await call('/officers/outreach/quick-test-send', {
        method: 'POST',
        body: JSON.stringify({
          stop_id: requiredNumericValue('quickMessageStopId', 'Stop'),
          recipient_email: document.getElementById('quickMessageRecipientEmail').value,
          subject_template: document.getElementById('quickMessageSubject').value,
          body_template: document.getElementById('quickMessageBody').value,
        }),
      });
      document.getElementById('quickMessageMeta').textContent = `Quick test send status: ${result.status} to ${result.recipient_email}`;
      await loadBootstrap();
      showSection('compose');
    });

    document.getElementById('oneOffPreviewBtn').addEventListener('click', async () => {
      const preview = await call('/officers/outreach/preview', {
        method: 'POST',
        body: JSON.stringify({
          template_id: requiredNumericValue('oneOffTemplateId', 'Template'),
          stop_id: requiredNumericValue('oneOffStopId', 'Stop'),
          contact_id: document.getElementById('oneOffContactId').value ? Number(document.getElementById('oneOffContactId').value) : null,
          recipient_email: document.getElementById('oneOffRecipientEmail').value,
          manual_contact: oneOffManualContactPayload(),
        }),
      });
      document.getElementById('oneOffSubjectBox').textContent = preview.subject;
      document.getElementById('oneOffBodyBox').textContent = preview.text_body;
      document.getElementById('oneOffHtmlFrame').srcdoc = preview.html_body || '';
      document.getElementById('oneOffMeta').textContent = preview.missing_fields.length
        ? 'Unknown placeholders: ' + preview.missing_fields.join(', ')
        : 'One-off preview rendered successfully.';
    });

    document.getElementById('oneOffSendBtn').addEventListener('click', async () => {
      const result = await call('/officers/outreach/one-off-send', {
        method: 'POST',
        body: JSON.stringify({
          template_id: requiredNumericValue('oneOffTemplateId', 'Template'),
          stop_id: requiredNumericValue('oneOffStopId', 'Stop'),
          contact_id: document.getElementById('oneOffContactId').value ? Number(document.getElementById('oneOffContactId').value) : null,
          recipient_email: document.getElementById('oneOffRecipientEmail').value,
          manual_contact: oneOffManualContactPayload(),
        }),
      });
      document.getElementById('oneOffMeta').textContent = `One-off send status: ${result.status} to ${result.recipient_email}`;
      await loadBootstrap();
      showSection('compose');
    });

    document.getElementById('oneOffClearBtn').addEventListener('click', clearOneOffForm);
    document.getElementById('oneOffContactId').addEventListener('change', () => {
      const contactId = document.getElementById('oneOffContactId').value;
      if (!contactId) return;
      const row = state.contacts.find((item) => item.id === Number(contactId));
      if (!row) return;
      document.getElementById('oneOffRecipientEmail').value = row.email || '';
      document.getElementById('oneOffFirstName').value = row.first_name || '';
      document.getElementById('oneOffLastName').value = row.last_name || '';
      document.getElementById('oneOffFullName').value = row.full_name || '';
      document.getElementById('oneOffWorkLocation').value = row.work_location || '';
      document.getElementById('oneOffWorkGroup').value = row.work_group || '';
      document.getElementById('oneOffDepartment').value = row.department || '';
      document.getElementById('oneOffBargainingUnit').value = row.bargaining_unit || '';
      document.getElementById('oneOffLocalNumber').value = row.local_number || '';
      document.getElementById('oneOffStewardName').value = row.steward_name || '';
      document.getElementById('oneOffRepName').value = row.rep_name || '';
      document.getElementById('oneOffExtraFields').value = JSON.stringify(row.extra_fields || {}, null, 2);
    });

    document.getElementById('runDueBtn').addEventListener('click', async () => {
      const result = await call('/officers/outreach/run-due', { method: 'POST', body: '{}' });
      document.getElementById('runDueNote').textContent = `Processed ${result.processed_count}, sent ${result.sent_count}, failed ${result.failed_count}, skipped suppressed ${result.skipped_suppressed_count}, skipped existing ${result.skipped_existing_count}.`;
      await loadBootstrap();
    });

    document.getElementById('refreshBtn').addEventListener('click', loadBootstrap);
    document.querySelectorAll('[data-target-section]').forEach((button) => {
      button.addEventListener('click', () => showSection(button.dataset.targetSection));
    });
    document.getElementById('analyticsApplyBtn').addEventListener('click', loadAnalytics);
    document.getElementById('analyticsResetBtn').addEventListener('click', async () => {
      ['analyticsDateFrom','analyticsDateTo','analyticsLocation','analyticsWorkGroup','analyticsRecipientEmail'].forEach((id) => document.getElementById(id).value = '');
      document.getElementById('analyticsStopId').value = '';
      document.getElementById('analyticsTemplateId').value = '';
      await loadAnalytics();
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
      document.getElementById('runDueNote').textContent = message;
      document.getElementById('runDueNote').classList.add('error');
    });

    loadBootstrap().catch((err) => {
      document.getElementById('runDueNote').textContent = err.message;
      document.getElementById('runDueNote').classList.add('error');
    });
    resetQuickMessage();
  </script>
</body>
</html>"""
