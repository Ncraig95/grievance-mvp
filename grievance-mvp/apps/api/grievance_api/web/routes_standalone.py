from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..core.ids import new_document_id, new_submission_id
from ..core.intake_auth import verify_intake_request_auth
from ..db.db import Db, utcnow
from ..services.doc_render import render_docx
from ..services.notification_service import NotificationService
from ..services.pdf_convert import docx_to_pdf
from ..services.signature_workflow import resolve_docuseal_template_id
from ..services.standalone_forms import (
    build_standalone_context,
    standalone_document_basename,
    standalone_document_dir,
    standalone_sharepoint_folder_path,
)
from .models import (
    ResendNotificationRequest,
    ResendNotificationResult,
    StandaloneDocumentStatus,
    StandaloneSubmissionRequest,
    StandaloneSubmissionResponse,
)

router = APIRouter()


def _resolve_standalone_form(cfg, form_key: str):  # noqa: ANN001
    wanted = str(form_key or "").strip()
    if not wanted:
        raise HTTPException(status_code=404, detail="standalone form not found")
    if wanted in cfg.standalone_forms:
        return wanted, cfg.standalone_forms[wanted]
    lowered = wanted.lower()
    if lowered in cfg.standalone_forms:
        return lowered, cfg.standalone_forms[lowered]
    raise HTTPException(status_code=404, detail=f"standalone form '{wanted}' not found")


def _upload_local_file_to_standalone_folder(
    *,
    cfg,  # noqa: ANN001
    graph,  # noqa: ANN001
    form_cfg,  # noqa: ANN001
    submission_id: str,
    local_path: str,
    filename: str,
    subfolder: str,
):
    folder_path = standalone_sharepoint_folder_path(
        standalone_parent_folder=cfg.graph.standalone_parent_folder,
        form_cfg=form_cfg,
        submission_id=submission_id,
    )
    target_folder = "/".join(part for part in (folder_path, subfolder) if part)
    return graph.upload_local_file_to_folder_path(
        site_hostname=cfg.graph.site_hostname,
        site_path=cfg.graph.site_path,
        library=cfg.graph.document_library,
        folder_path=target_folder,
        filename=filename,
        local_path=local_path,
    )


def _build_signed_pdf_attachment(
    *,
    cfg,  # noqa: ANN001
    form_key: str,
    signed_pdf_path: str | None,
):
    path = Path(str(signed_pdf_path or "").strip())
    if not path.exists() or not path.is_file():
        return None
    size_bytes = path.stat().st_size
    if size_bytes <= 0 or size_bytes > cfg.email.max_attachment_bytes:
        return None
    from ..services.graph_mail import MailAttachment

    return [
        MailAttachment(
            filename=f"{form_key}_signed.pdf",
            content_type="application/pdf",
            content_bytes=path.read_bytes(),
        )
    ]


def _resolve_standalone_notification_template(template_key: str) -> str:
    normalized = str(template_key or "").strip().lower()
    mapping = {
        "signature_request": "standalone_signature_request",
        "completion_signer": "standalone_completion_signer",
        "completion_internal": "standalone_completion_internal",
    }
    if normalized not in mapping:
        raise HTTPException(status_code=400, detail=f"unsupported standalone notification template '{template_key}'")
    return mapping[normalized]


async def _load_submission_status(db: Db, submission_id: str) -> StandaloneSubmissionResponse:
    row = await db.fetchone(
        """SELECT form_key, form_title, status
           FROM standalone_submissions
           WHERE id=?""",
        (submission_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="submission_id not found")

    docs = await db.fetchall(
        """SELECT id, form_key, status, docuseal_signing_link,
                  COALESCE(sharepoint_signed_url, sharepoint_generated_url, '')
           FROM standalone_documents
           WHERE submission_id=?
           ORDER BY created_at_utc""",
        (submission_id,),
    )
    return StandaloneSubmissionResponse(
        submission_id=submission_id,
        form_key=row[0],
        form_title=row[1],
        status=row[2],
        documents=[
            StandaloneDocumentStatus(
                document_id=doc[0],
                form_key=doc[1],
                status=doc[2],
                signing_link=doc[3],
                document_link=doc[4] or None,
            )
            for doc in docs
        ],
    )


def _build_standalone_notification_context(
    *,
    submission_id: str,
    form_key: str,
    form_title: str,
    document_id: str,
    signing_url: str,
    document_link: str,
    status: str,
) -> dict[str, object]:
    return {
        "submission_id": submission_id,
        "form_key": form_key,
        "form_title": form_title,
        "document_id": document_id,
        "document_type": form_title,
        "docuseal_signing_url": signing_url,
        "document_link": document_link,
        "copy_link": document_link,
        "status": status,
        "completed_at_utc": utcnow(),
    }


def _resolve_standalone_signer_email(*, body: StandaloneSubmissionRequest, form_cfg) -> str:  # noqa: ANN001
    requested = str(body.local_president_signer_email or "").strip()
    if requested:
        return requested
    configured = str(getattr(form_cfg, "default_signer_email", "") or "").strip()
    if configured:
        return configured
    raise HTTPException(
        status_code=400,
        detail="local_president_signer_email is required unless standalone_forms.<form_key>.default_signer_email is set",
    )


@router.post(
    "/standalone/forms/{form_key}/submissions",
    response_model=StandaloneSubmissionResponse,
)
async def create_standalone_submission(
    form_key: str,
    body: StandaloneSubmissionRequest,
    request: Request,
):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger
    graph = request.app.state.graph
    docuseal = request.app.state.docuseal
    notifications: NotificationService = request.app.state.notifications

    await verify_intake_request_auth(request, cfg.intake_auth)

    resolved_form_key, form_cfg = _resolve_standalone_form(cfg, form_key)
    if body.form_key.strip().lower() != resolved_form_key.lower():
        raise HTTPException(status_code=400, detail="body.form_key must match path form_key")
    signer_email = _resolve_standalone_signer_email(body=body, form_cfg=form_cfg)

    existing = await db.fetchone(
        "SELECT id, status FROM standalone_submissions WHERE request_id=?",
        (body.request_id,),
    )
    if existing:
        logger.info("standalone_submission_deduped", extra={"correlation_id": existing[0], "status": existing[1]})
        return await _load_submission_status(db, existing[0])

    submission_id = new_submission_id()
    document_id = new_document_id()
    document_dir = standalone_document_dir(
        data_root=cfg.data_root,
        submission_id=submission_id,
        document_id=document_id,
    )
    document_dir.mkdir(parents=True, exist_ok=True)

    basename = standalone_document_basename(submission_id=submission_id, form_cfg=form_cfg)
    docx_path = str(document_dir / f"{basename}.docx")
    pdf_path = str(document_dir / f"{basename}.pdf")
    anchor_docx_path = str(document_dir / f"{basename}.anchor.docx")

    await db.exec(
        """INSERT INTO standalone_submissions(
             id, request_id, form_key, form_title, signer_email, status, created_at_utc, template_data_json
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            submission_id,
            body.request_id,
            resolved_form_key,
            form_cfg.form_label,
            signer_email,
            "processing",
            utcnow(),
            json.dumps(body.template_data, ensure_ascii=False),
        ),
    )
    await db.exec(
        """INSERT INTO standalone_documents(
             id, submission_id, created_at_utc, form_key, template_key, status, requires_signature, signer_order_json
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            document_id,
            submission_id,
            utcnow(),
            resolved_form_key,
            resolved_form_key,
            "processing",
            1,
            json.dumps([signer_email], ensure_ascii=False),
        ),
    )
    await db.add_standalone_event(
        submission_id,
        None,
        "submission_created",
        {"request_id": body.request_id, "form_key": resolved_form_key},
    )

    try:
        context = build_standalone_context(
            form_key=resolved_form_key,
            form_cfg=form_cfg,
            payload=body,
            submission_id=submission_id,
            document_id=document_id,
        )

        render_docx(
            form_cfg.template_path,
            context,
            anchor_docx_path,
            strip_signature_placeholders=False,
            normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
        )
        anchor_pdf_path = docx_to_pdf(
            anchor_docx_path,
            str(document_dir),
            cfg.libreoffice_timeout_seconds,
            engine=cfg.docx_pdf_engine,
            graph_uploader=graph,
            graph_site_hostname=cfg.graph.site_hostname,
            graph_site_path=cfg.graph.site_path,
            graph_library=cfg.graph.document_library,
            graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
        )
        alignment_pdf_bytes = Path(anchor_pdf_path).read_bytes()

        render_docx(
            form_cfg.template_path,
            context,
            docx_path,
            strip_signature_placeholders=True,
            normalize_split_placeholders=cfg.rendering.normalize_split_placeholders,
        )
        rendered_pdf_path = docx_to_pdf(
            docx_path,
            str(document_dir),
            cfg.libreoffice_timeout_seconds,
            engine=cfg.docx_pdf_engine,
            graph_uploader=graph,
            graph_site_hostname=cfg.graph.site_hostname,
            graph_site_path=cfg.graph.site_path,
            graph_library=cfg.graph.document_library,
            graph_temp_folder_path=cfg.docx_pdf_graph_temp_folder,
        )
        pdf_bytes = Path(rendered_pdf_path).read_bytes()
        Path(pdf_path).write_bytes(pdf_bytes)
        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

        sharepoint_generated_url: str | None = None
        sharepoint_folder_path: str | None = None
        if (
            cfg.graph.site_hostname
            and cfg.graph.site_path
            and cfg.graph.document_library
            and form_cfg.sharepoint_storage.upload_generated
        ):
            try:
                uploaded_generated = _upload_local_file_to_standalone_folder(
                    cfg=cfg,
                    graph=graph,
                    form_cfg=form_cfg,
                    submission_id=submission_id,
                    local_path=pdf_path,
                    filename=f"{resolved_form_key}_{document_id}.pdf",
                    subfolder=cfg.graph.generated_subfolder,
                )
                sharepoint_generated_url = uploaded_generated.web_url
                sharepoint_folder_path = standalone_sharepoint_folder_path(
                    standalone_parent_folder=cfg.graph.standalone_parent_folder,
                    form_cfg=form_cfg,
                    submission_id=submission_id,
                )
                await db.add_standalone_event(
                    submission_id,
                    document_id,
                    "sharepoint_generated_uploaded",
                    {"path": uploaded_generated.path, "web_url": uploaded_generated.web_url},
                )
            except Exception as exc:
                await db.add_standalone_event(
                    submission_id,
                    document_id,
                    "sharepoint_generated_upload_failed",
                    {"error": str(exc)},
                )

        submission = docuseal.create_submission(
            pdf_bytes=pdf_bytes,
            alignment_pdf_bytes=alignment_pdf_bytes,
            signers=[signer_email],
            title=f"{form_cfg.form_label} - {submission_id}",
            metadata={
                "submission_id": submission_id,
                "standalone_document_id": document_id,
                "form_key": resolved_form_key,
            },
            template_id=resolve_docuseal_template_id(
                cfg,
                template_key=resolved_form_key,
                doc_type=resolved_form_key,
            ),
            form_key=resolved_form_key,
        )
        signing_link = submission.signing_link
        signer_links_by_email: dict[str, str] = {}
        try:
            signer_links_by_email = docuseal.extract_signing_links_by_email(submission.raw)
        except Exception:
            signer_links_by_email = {}
        if not signer_links_by_email:
            try:
                signer_links_by_email = docuseal.fetch_signing_links_by_email(submission_id=submission.submission_id)
            except Exception:
                signer_links_by_email = {}
        signer_link = signer_links_by_email.get(signer_email.lower()) or signing_link

        await db.exec(
            """UPDATE standalone_documents
               SET status=?, docx_path=?, pdf_path=?, pdf_sha256=?,
                   docuseal_submission_id=?, docuseal_signing_link=?, sharepoint_generated_url=?
               WHERE id=?""",
            (
                "awaiting_signature",
                docx_path,
                pdf_path,
                pdf_sha256,
                submission.submission_id,
                signer_link,
                sharepoint_generated_url,
                document_id,
            ),
        )
        await db.exec(
            """UPDATE standalone_submissions
               SET status=?, sharepoint_folder_path=?
               WHERE id=?""",
            ("awaiting_signature", sharepoint_folder_path, submission_id),
        )
        await db.add_standalone_event(
            submission_id,
            document_id,
            "sent_for_signature",
            {"submission_id": submission.submission_id, "signing_link": signer_link},
        )

        if cfg.email.enabled and signer_link:
            context = _build_standalone_notification_context(
                submission_id=submission_id,
                form_key=resolved_form_key,
                form_title=form_cfg.form_label,
                document_id=document_id,
                signing_url=signer_link,
                document_link=sharepoint_generated_url or signer_link,
                status="awaiting_signature",
            )
            await notifications.send_one(
                case_id=submission_id,
                document_id=document_id,
                recipient_email=signer_email,
                template_key="standalone_signature_request",
                context={**context, "signer_email": signer_email},
                idempotency_key=f"standalone:{submission_id}:{document_id}:signature_request:{signer_email.lower()}",
                form_key=resolved_form_key,
                scope_kind="standalone",
            )

        return await _load_submission_status(db, submission_id)
    except Exception as exc:
        await db.exec("UPDATE standalone_documents SET status='failed' WHERE id=?", (document_id,))
        await db.exec("UPDATE standalone_submissions SET status='failed' WHERE id=?", (submission_id,))
        await db.add_standalone_event(
            submission_id,
            document_id,
            "submission_failed",
            {"error": str(exc)},
        )
        logger.exception(
            "standalone_submission_failed",
            extra={"correlation_id": submission_id, "document_id": document_id},
        )
        raise HTTPException(status_code=500, detail="standalone submission failed") from exc


@router.get(
    "/standalone/submissions/{submission_id}",
    response_model=StandaloneSubmissionResponse,
)
async def get_standalone_submission(submission_id: str, request: Request):
    db: Db = request.app.state.db
    return await _load_submission_status(db, submission_id)


@router.post(
    "/standalone/submissions/{submission_id}/notifications/resend",
    response_model=list[ResendNotificationResult],
)
async def resend_standalone_notification(
    submission_id: str,
    body: ResendNotificationRequest,
    request: Request,
):
    db: Db = request.app.state.db
    cfg = request.app.state.cfg
    notifications: NotificationService = request.app.state.notifications

    submission_row = await db.fetchone(
        """SELECT form_key, form_title, signer_email, status
           FROM standalone_submissions
           WHERE id=?""",
        (submission_id,),
    )
    if not submission_row:
        raise HTTPException(status_code=404, detail="submission_id not found")

    form_key, form_title, signer_email, submission_status = submission_row
    document_id = body.document_id
    signing_url = ""
    document_link = ""
    signer_order_json = ""
    signed_pdf_path = ""
    if document_id:
        doc_row = await db.fetchone(
            """SELECT COALESCE(docuseal_signing_link, ''),
                      COALESCE(sharepoint_signed_url, sharepoint_generated_url, ''),
                      COALESCE(signer_order_json, ''),
                      COALESCE(signed_pdf_path, pdf_path, '')
               FROM standalone_documents
               WHERE id=? AND submission_id=?""",
            (document_id, submission_id),
        )
        if not doc_row:
            raise HTTPException(status_code=404, detail="document_id not found for submission")
        signing_url, document_link, signer_order_json, signed_pdf_path = doc_row

    requested_template_key = body.template_key.strip()
    actual_template_key = _resolve_standalone_notification_template(requested_template_key)

    recipients = [r.strip() for r in (body.recipients or []) if r.strip()]
    if not recipients:
        parsed_signers: list[str] = []
        if signer_order_json:
            try:
                values = json.loads(signer_order_json)
                if isinstance(values, list):
                    parsed_signers = [str(v).strip() for v in values if str(v).strip()]
            except Exception:
                parsed_signers = []

        if requested_template_key in {"signature_request", "completion_signer"}:
            recipients = parsed_signers or ([signer_email] if signer_email else [])
        elif requested_template_key == "completion_internal":
            recipients = list(cfg.email.internal_recipients)

    if not recipients:
        raise HTTPException(status_code=400, detail="No recipients available for this template")

    base_context = _build_standalone_notification_context(
        submission_id=submission_id,
        form_key=form_key,
        form_title=form_title,
        document_id=document_id or "",
        signing_url=signing_url,
        document_link=document_link,
        status=submission_status,
    )
    base_context.update(body.context_overrides)

    attachments = None
    if requested_template_key == "completion_signer":
        attachments = _build_signed_pdf_attachment(cfg=cfg, form_key=form_key, signed_pdf_path=signed_pdf_path)
    elif requested_template_key == "completion_internal" and cfg.email.artifact_delivery_mode == "attach_pdf":
        attachments = _build_signed_pdf_attachment(cfg=cfg, form_key=form_key, signed_pdf_path=signed_pdf_path)

    out: list[ResendNotificationResult] = []
    for recipient in recipients:
        idem = f"{body.idempotency_key}:{requested_template_key}:{(document_id or '')}:{recipient.lower()}"
        result = await notifications.send_one(
            case_id=submission_id,
            document_id=document_id,
            recipient_email=recipient,
            template_key=actual_template_key,
            context={**base_context, "signer_email": recipient},
            idempotency_key=idem,
            allow_resend=True,
            attachments=attachments,
            form_key=form_key,
            scope_kind="standalone",
        )
        out.append(
            ResendNotificationResult(
                recipient_email=result.recipient_email,
                status=result.status,
                deduped=result.deduped,
                graph_message_id=result.graph_message_id,
                resend_count=result.resend_count,
            )
        )

    return out
