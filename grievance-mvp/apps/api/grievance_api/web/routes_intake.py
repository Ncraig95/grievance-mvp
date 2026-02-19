from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import textwrap
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests

from fastapi import APIRouter, HTTPException, Request

from ..core.hmac_auth import verify_hmac
from ..core.ids import new_case_id, new_document_id, normalize_grievance_id
from ..core.intake_auth import verify_intake_request_auth
from ..db.db import Db, utcnow
from ..services.case_folder_naming import build_case_folder_member_name
from ..services.doc_render import render_docx
from ..services.grievance_id_allocator import GrievanceIdAllocationError, GrievanceIdAllocator
from ..services.notification_service import NotificationService
from ..services.pdf_convert import docx_to_pdf
from ..services.signature_workflow import normalize_signers, send_document_for_signature
from .models import (
    CaseStatusResponse,
    ClientSuppliedFile,
    DocumentRequest,
    DocumentStatus,
    IntakeRequest,
    IntakeResponse,
)

router = APIRouter()


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_FIELD_KEY_SAFE = re.compile(r"[^A-Za-z0-9]+")
_CLIENT_SUPPLIED_TOTAL_MAX_BYTES = 1_073_741_824
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_DEFAULT_STATEMENT_WRAP_WIDTH = 95


def _safe_name(value: str) -> str:
    safe = _FILENAME_SAFE.sub("_", value.strip())
    return safe.strip("_") or "document"


def _resolve_template_path(cfg, doc_req: DocumentRequest) -> str:  # noqa: ANN001
    if doc_req.template_key and doc_req.template_key in cfg.doc_templates:
        return cfg.doc_templates[doc_req.template_key]
    if doc_req.doc_type in cfg.doc_templates:
        return cfg.doc_templates[doc_req.doc_type]
    return cfg.docx_template_path


def _normalize_field_key(key: str) -> str:
    normalized = _FIELD_KEY_SAFE.sub("_", key.strip()).strip("_").lower()
    return normalized


def _flatten_fields(value: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, object] = {}
    for raw_key, raw_val in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        composed = f"{prefix}_{key}" if prefix else key
        if isinstance(raw_val, dict):
            out.update(_flatten_fields(raw_val, composed))
        else:
            out[composed] = raw_val
    return out


def _coerce_context_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None)
    return json.dumps(value, ensure_ascii=False)


def _coerce_positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return fallback
    return parsed if parsed > 0 else fallback


def _normalize_existing_statement_lines(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    rows: list[dict[str, object]] = []
    for idx, item in enumerate(value, start=1):
        if isinstance(item, dict):
            text = str(item.get("text", ""))
        else:
            text = str(item)
        rows.append({"text": text, "line_no": idx})
    return rows


def _build_statement_line_rows(full_text: str, wrap_width: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for paragraph in full_text.splitlines():
        normalized = paragraph.strip()
        if not normalized:
            rows.append({"text": ""})
            continue
        wrapped = textwrap.wrap(
            normalized,
            width=wrap_width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if not wrapped:
            rows.append({"text": normalized})
            continue
        for line in wrapped:
            rows.append({"text": line})

    if not rows:
        rows = [{"text": ""}]

    return [{"text": str(row.get("text", "")), "line_no": idx + 1} for idx, row in enumerate(rows)]


def _apply_dynamic_statement_context(context: dict[str, object]) -> None:
    statement_text = str(context.get("statement_text", "") or "")
    continuation = str(context.get("statement_continuation", "") or "")
    joined = statement_text
    if continuation.strip():
        joined = f"{statement_text}\n{continuation}" if statement_text else continuation

    existing = _normalize_existing_statement_lines(context.get("statement_lines"))
    if existing is not None:
        rows = existing if existing else [{"text": "", "line_no": 1}]
    else:
        wrap_width = _coerce_positive_int(context.get("statement_line_wrap_width"), _DEFAULT_STATEMENT_WRAP_WIDTH)
        rows = _build_statement_line_rows(joined, wrap_width)

    context["statement_full_text"] = joined
    context["statement_lines"] = rows
    context["statement_rows"] = rows
    context["statement_line_count"] = len(rows)
    context["statement_has_continuation"] = bool(continuation.strip())


def _preferred_signer_email(payload: IntakeRequest) -> str | None:
    template_data = payload.template_data or {}
    if isinstance(template_data, dict):
        for key in ("personal_email", "personalEmail", "signer_email", "signerEmail"):
            val = template_data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    if payload.grievant_email and payload.grievant_email.strip():
        return payload.grievant_email.strip()
    return None


def _apply_statement_defaults(
    *,
    context: dict[str, object],
    payload: IntakeRequest,
    grievance_id: str,
    grievance_number: str | None,
) -> None:
    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    defaults: dict[str, object] = {
        "grievant_name": member_name,
        "work_address": payload.work_location or "",
        "home_address": "",
        "seniority_date": "",
        "ncs_date": "",
        "personal_cell": payload.grievant_phone or "",
        "personal_email": payload.grievant_email or "",
        "department": "",
        "title": "",
        "supervisor_name": payload.supervisor or "",
        "supervisor_phone": "",
        "supervisor_email": "",
        # Legacy placeholder in template includes a space.
        "grievants uid": grievance_id,
        "grievants_uid": grievance_id,
        "incident_date": payload.incident_date or "",
        "article": "",
        "statement_text": payload.narrative or "",
        "statement_continuation": "",
        "witness_1_name": "",
        "witness_1_title": "",
        "witness_1_phone": "",
        "witness_2_name": "",
        "witness_2_title": "",
        "witness_2_phone": "",
        "witness_3_name": "",
        "witness_3_title": "",
        "witness_3_phone": "",
        "grievance_number": grievance_number or "",
    }
    for key, value in defaults.items():
        context.setdefault(key, value)


def _validate_grievance_input_mode(grievance_mode: str, incoming_grievance_id: str) -> None:
    if grievance_mode == "auto" and incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id must be omitted when grievance_id.mode=auto",
        )
    if grievance_mode == "manual" and not incoming_grievance_id:
        raise HTTPException(
            status_code=400,
            detail="grievance_id is required when grievance_id.mode=manual",
        )


def _sanitize_intake_payload_for_storage(payload: IntakeRequest) -> str:
    body = payload.model_dump(exclude_none=True)
    sanitized_client_files: list[dict[str, object]] = []
    for item in payload.client_supplied_files:
        sanitized_client_files.append(
            {
                "file_name": item.file_name,
                "has_download_url": bool((item.download_url or "").strip()),
                "has_content_base64": bool((item.content_base64 or "").strip()),
            }
        )
    body["client_supplied_files"] = sanitized_client_files
    return json.dumps(body, ensure_ascii=False)


def _decode_base64_payload(value: str) -> bytes:
    raw = (value or "").strip()
    if not raw:
        return b""
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw, validate=True)


def _download_to_temp_file(*, download_url: str) -> tuple[Path, int]:
    parsed = urlparse(download_url)
    if parsed.scheme.lower() != "https":
        raise RuntimeError("client file download_url must use https")

    with tempfile.NamedTemporaryFile(prefix="client-file-", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    size_bytes = 0
    try:
        with requests.get(download_url, stream=True, timeout=(10, 300)) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    out.write(chunk)
                    size_bytes += len(chunk)
                    if size_bytes > _CLIENT_SUPPLIED_TOTAL_MAX_BYTES:
                        raise RuntimeError("client supplied file exceeds 1GB limit")
        return tmp_path, size_bytes
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _materialize_client_supplied_file(item: ClientSuppliedFile) -> tuple[Path, int]:
    if item.download_url and item.download_url.strip():
        return _download_to_temp_file(download_url=item.download_url.strip())

    if item.content_base64 and item.content_base64.strip():
        content = _decode_base64_payload(item.content_base64)
        if len(content) > _CLIENT_SUPPLIED_TOTAL_MAX_BYTES:
            raise RuntimeError("client supplied file exceeds 1GB limit")
        with tempfile.NamedTemporaryFile(prefix="client-file-", delete=False) as tmp:
            tmp.write(content)
            return Path(tmp.name), len(content)

    raise RuntimeError("client supplied file requires download_url or content_base64")


async def _upload_client_supplied_files(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    logger,  # noqa: ANN001
    graph,  # noqa: ANN001
    payload: IntakeRequest,
    case_id: str,
    grievance_id: str,
    member_name: str,
    correlation_id: str,
    sharepoint_case_folder: str | None,
    sharepoint_case_web_url: str | None,
) -> tuple[str | None, str | None]:
    files = payload.client_supplied_files or []
    if not files:
        return sharepoint_case_folder, sharepoint_case_web_url

    if not cfg.graph.site_hostname or not cfg.graph.site_path or not cfg.graph.document_library:
        raise HTTPException(status_code=503, detail="SharePoint is not configured for client supplied files")

    case_folder_name = sharepoint_case_folder
    case_folder_web_url = sharepoint_case_web_url
    if not case_folder_name:
        folder_ref = graph.ensure_case_folder(
            site_hostname=cfg.graph.site_hostname,
            site_path=cfg.graph.site_path,
            library=cfg.graph.document_library,
            case_parent_folder=cfg.graph.case_parent_folder,
            grievance_id=grievance_id,
            member_name=member_name,
        )
        case_folder_name = folder_ref.folder_name
        case_folder_web_url = folder_ref.web_url

    total_uploaded_bytes = 0
    uploaded_count = 0
    for item in files:
        safe_filename = _safe_name(item.file_name)
        local_path: Path | None = None
        try:
            local_path, size_bytes = _materialize_client_supplied_file(item)
            total_uploaded_bytes += size_bytes
            if total_uploaded_bytes > _CLIENT_SUPPLIED_TOTAL_MAX_BYTES:
                raise RuntimeError("combined client supplied files exceed 1GB limit")

            uploaded = graph.upload_local_file_to_case_subfolder(
                site_hostname=cfg.graph.site_hostname,
                site_path=cfg.graph.site_path,
                library=cfg.graph.document_library,
                case_folder_name=case_folder_name,
                case_parent_folder=cfg.graph.case_parent_folder,
                subfolder=cfg.graph.client_supplied_subfolder,
                filename=safe_filename,
                local_path=str(local_path),
            )
            uploaded_count += 1
            await db.add_event(
                case_id,
                None,
                "client_supplied_file_uploaded",
                {
                    "filename": safe_filename,
                    "size_bytes": size_bytes,
                    "sharepoint_path": uploaded.path,
                },
            )
        except Exception as exc:
            logger.exception(
                "client_supplied_file_upload_failed",
                extra={"correlation_id": correlation_id, "case_id": case_id, "client_filename": safe_filename},
            )
            await db.add_event(
                case_id,
                None,
                "client_supplied_file_upload_failed",
                {"filename": safe_filename, "error": str(exc)},
            )
            raise HTTPException(status_code=503, detail="unable to upload client supplied files") from exc
        finally:
            if local_path is not None:
                local_path.unlink(missing_ok=True)

    await db.add_event(
        case_id,
        None,
        "client_supplied_files_upload_completed",
        {"file_count": uploaded_count, "total_bytes": total_uploaded_bytes},
    )
    return case_folder_name, case_folder_web_url


def _resolve_document_command(cfg, document_command: str) -> DocumentRequest:  # noqa: ANN001
    raw = (document_command or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="document_command cannot be empty")

    candidates: list[str] = []
    for value in (raw, _normalize_field_key(raw)):
        normalized = value.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    for key in candidates:
        if key in cfg.doc_templates:
            # Command mode is a convenience for single-doc workflows (Power Automate).
            return DocumentRequest(doc_type=key, template_key=key, requires_signature=True)

    raise HTTPException(status_code=400, detail=f"Unknown document_command '{raw}'")


def _build_template_context(
    *,
    payload: IntakeRequest,
    case_id: str,
    grievance_id: str,
    document_id: str,
    doc_type: str,
    grievance_number: str | None,
) -> dict[str, object]:
    payload_data = payload.model_dump(
        exclude={"documents", "template_data", "document_command", "client_supplied_files"}
    )
    merged_fields = _flatten_fields(payload_data)
    merged_fields.update(_flatten_fields(payload.template_data))

    context: dict[str, object] = {
        "case_id": case_id,
        "grievance_id": grievance_id,
        "grievance_number": grievance_number or "",
        "document_id": document_id,
        "document_type": doc_type,
        "created_at_utc": utcnow(),
    }
    protected_keys = set(context.keys())

    for key, raw_val in merged_fields.items():
        value = _coerce_context_value(raw_val)
        if key not in protected_keys:
            context[key] = value

        normalized = _normalize_field_key(key)
        if normalized and normalized not in context:
            context[normalized] = value

    _apply_statement_defaults(
        context=context,
        payload=payload,
        grievance_id=grievance_id,
        grievance_number=grievance_number,
    )

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    if member_name:
        context.setdefault("grievant_name", member_name)
        context.setdefault("grievant_names", member_name)

    today = date.today().isoformat()
    context.setdefault("today_date", today)
    context.setdefault("request_date", today)
    _apply_dynamic_statement_context(context)

    return context


async def _load_case_status(db: Db, case_id: str) -> CaseStatusResponse:
    case_row = await db.fetchone(
        "SELECT grievance_id, status, approval_status, grievance_number FROM cases WHERE id=?",
        (case_id,),
    )
    if not case_row:
        raise HTTPException(status_code=404, detail="case_id not found")

    docs = await db.fetchall(
        "SELECT id, doc_type, status, docuseal_signing_link FROM documents WHERE case_id=? ORDER BY created_at_utc",
        (case_id,),
    )
    statuses = [
        DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3])
        for d in docs
    ]

    return CaseStatusResponse(
        case_id=case_id,
        grievance_id=case_row[0],
        status=case_row[1],
        approval_status=case_row[2],
        grievance_number=case_row[3],
        documents=statuses,
    )


@router.get("/cases/{case_id}", response_model=CaseStatusResponse)
async def get_case(case_id: str, request: Request):
    db: Db = request.app.state.db
    return await _load_case_status(db, case_id)


@router.post("/intake", response_model=IntakeResponse)
async def intake(request: Request):
    cfg = request.app.state.cfg
    db: Db = request.app.state.db
    logger = request.app.state.logger
    docuseal = request.app.state.docuseal
    graph = request.app.state.graph
    notifications: NotificationService = request.app.state.notifications

    await verify_intake_request_auth(request, cfg.intake_auth)
    body = await verify_hmac(request, cfg.hmac_shared_secret)
    payload = IntakeRequest.model_validate_json(body)

    correlation_id = payload.request_id

    existing = await db.fetchone(
        "SELECT id, grievance_id, status FROM cases WHERE intake_request_id=?",
        (payload.request_id,),
    )
    if existing:
        case_id, grievance_id, status = existing
        logger.info("intake_deduped", extra={"correlation_id": correlation_id, "case_id": case_id})
        docs = await db.fetchall(
            "SELECT id, doc_type, status, docuseal_signing_link FROM documents WHERE case_id=? ORDER BY created_at_utc",
            (case_id,),
        )
        return IntakeResponse(
            case_id=case_id,
            grievance_id=grievance_id,
            status=status,
            documents=[
                DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3]) for d in docs
            ],
        )

    grievance_number = (payload.grievance_number or "").strip() or None
    case_id = new_case_id()
    cdir = Path(cfg.data_root) / case_id
    cdir.mkdir(parents=True, exist_ok=True)

    member_name = f"{payload.grievant_firstname} {payload.grievant_lastname}".strip()
    folder_member_name = build_case_folder_member_name(member_name, payload.contract)
    incoming_grievance_id = (payload.grievance_id or "").strip()
    grievance_mode = cfg.grievance_id.mode

    _validate_grievance_input_mode(grievance_mode, incoming_grievance_id)

    sharepoint_case_folder: str | None = None
    sharepoint_case_web_url: str | None = None
    if grievance_mode == "manual":
        grievance_id = normalize_grievance_id(incoming_grievance_id) or incoming_grievance_id
    else:
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=db,
            graph=request.app.state.graph,
            logger=logger,
        )
        try:
            allocation = await allocator.allocate_and_reserve_folder(
                member_name=folder_member_name,
                correlation_id=correlation_id,
            )
        except GrievanceIdAllocationError as exc:
            logger.exception("grievance_id_allocation_failed", extra={"correlation_id": correlation_id})
            raise HTTPException(status_code=503, detail="unable to allocate grievance_id") from exc
        grievance_id = allocation.grievance_id
        sharepoint_case_folder = allocation.case_folder_name
        sharepoint_case_web_url = allocation.case_folder_web_url

    try:
        await db.exec(
            """INSERT INTO cases(
                 id, grievance_id, created_at_utc, status, approval_status,
                 grievance_number, member_name, member_email, intake_request_id, intake_payload_json,
                 sharepoint_case_folder, sharepoint_case_web_url
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                grievance_id,
                utcnow(),
                "processing",
                "pending",
                grievance_number,
                member_name,
                payload.grievant_email,
                payload.request_id,
                _sanitize_intake_payload_for_storage(payload),
                sharepoint_case_folder,
                sharepoint_case_web_url,
            ),
        )
    except Exception as exc:
        if "UNIQUE constraint failed: cases.intake_request_id" in str(exc):
            dupe = await db.fetchone(
                "SELECT id, grievance_id, status FROM cases WHERE intake_request_id=?",
                (payload.request_id,),
            )
            if not dupe:
                raise
            dupe_case_id, dupe_grievance_id, dupe_status = dupe
            docs = await db.fetchall(
                "SELECT id, doc_type, status, docuseal_signing_link FROM documents WHERE case_id=? ORDER BY created_at_utc",
                (dupe_case_id,),
            )
            return IntakeResponse(
                case_id=dupe_case_id,
                grievance_id=dupe_grievance_id,
                status=dupe_status,
                documents=[
                    DocumentStatus(document_id=d[0], doc_type=d[1], status=d[2], signing_link=d[3]) for d in docs
                ],
            )
        raise
    await db.add_event(
        case_id,
        None,
        "case_created",
        {
            "request_id": payload.request_id,
            "grievance_id": grievance_id,
            "grievance_number": grievance_number,
            "grievance_id_mode": grievance_mode,
            "sharepoint_case_folder": sharepoint_case_folder,
        },
    )

    try:
        sharepoint_case_folder, sharepoint_case_web_url = await _upload_client_supplied_files(
            cfg=cfg,
            db=db,
            logger=logger,
            graph=graph,
            payload=payload,
            case_id=case_id,
            grievance_id=grievance_id,
            member_name=folder_member_name,
            correlation_id=correlation_id,
            sharepoint_case_folder=sharepoint_case_folder,
            sharepoint_case_web_url=sharepoint_case_web_url,
        )
        if sharepoint_case_folder or sharepoint_case_web_url:
            await db.exec(
                "UPDATE cases SET sharepoint_case_folder=?, sharepoint_case_web_url=? WHERE id=?",
                (sharepoint_case_folder, sharepoint_case_web_url, case_id),
            )
    except HTTPException:
        await db.exec("UPDATE cases SET status=? WHERE id=?", ("failed", case_id))
        raise

    if payload.documents:
        doc_requests = payload.documents
    elif (payload.document_command or "").strip():
        doc_requests = [_resolve_document_command(cfg, payload.document_command or "")]
    else:
        doc_requests = [DocumentRequest(doc_type="grievance_form", requires_signature=True)]
    doc_statuses: list[DocumentStatus] = []
    any_signature_requested = False
    any_signature_queued = False
    any_failed = False

    for doc_req in doc_requests:
        document_id = new_document_id()
        doc_type = doc_req.doc_type.strip() or "document"
        template_path = _resolve_template_path(cfg, doc_req)
        doc_name = _safe_name(doc_type)
        ddir = cdir / document_id
        ddir.mkdir(parents=True, exist_ok=True)

        docx_path = str(ddir / f"{doc_name}.docx")
        pdf_path = str(ddir / f"{doc_name}.pdf")

        context = _build_template_context(
            payload=payload,
            case_id=case_id,
            grievance_id=grievance_id,
            document_id=document_id,
            doc_type=doc_type,
            grievance_number=grievance_number,
        )

        alignment_pdf_bytes: bytes | None = None
        try:
            if doc_req.requires_signature:
                anchor_docx_path = str(ddir / f"{doc_name}.anchor.docx")
                anchor_pdf_path: str | None = None
                try:
                    render_docx(
                        template_path,
                        context,
                        anchor_docx_path,
                        strip_signature_placeholders=False,
                    )
                    anchor_pdf_path = docx_to_pdf(
                        anchor_docx_path,
                        str(ddir),
                        cfg.libreoffice_timeout_seconds,
                    )
                    alignment_pdf_bytes = Path(anchor_pdf_path).read_bytes()
                finally:
                    Path(anchor_docx_path).unlink(missing_ok=True)
                    if anchor_pdf_path:
                        Path(anchor_pdf_path).unlink(missing_ok=True)

                render_docx(
                    template_path,
                    context,
                    docx_path,
                    strip_signature_placeholders=True,
                )
            else:
                render_docx(template_path, context, docx_path)
            pdf_path = docx_to_pdf(docx_path, str(ddir), cfg.libreoffice_timeout_seconds)
            pdf_bytes = Path(pdf_path).read_bytes()
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        except Exception as exc:
            any_failed = True
            await db.exec(
                """INSERT INTO documents(
                     id, case_id, created_at_utc, doc_type, template_key, status,
                     requires_signature, docx_path, pdf_path, pdf_sha256
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    case_id,
                    utcnow(),
                    doc_type,
                    doc_req.template_key,
                    "failed",
                    1 if doc_req.requires_signature else 0,
                    docx_path,
                    pdf_path,
                    "",
                ),
            )
            await db.add_event(
                case_id,
                document_id,
                "render_failed",
                {"error": str(exc), "doc_type": doc_type, "template_path": template_path},
            )
            logger.exception("render_failed", extra={"correlation_id": correlation_id, "doc_type": doc_type})
            doc_statuses.append(DocumentStatus(document_id=document_id, doc_type=doc_type, status="failed"))
            continue

        initial_status = "created"
        await db.exec(
            """INSERT INTO documents(
                 id, case_id, created_at_utc, doc_type, template_key, status,
                 requires_signature, docx_path, pdf_path, pdf_sha256
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                case_id,
                utcnow(),
                doc_type,
                doc_req.template_key,
                initial_status,
                1 if doc_req.requires_signature else 0,
                docx_path,
                pdf_path,
                pdf_sha,
            ),
        )
        await db.add_event(
            case_id,
            document_id,
            "document_created",
            {"doc_type": doc_type, "template_path": template_path},
        )

        status = initial_status
        signing_link: str | None = None

        if doc_req.requires_signature:
            signer_order = normalize_signers(doc_req.signers, _preferred_signer_email(payload))
            if not grievance_number:
                status = "pending_grievance_number"
                any_signature_queued = True
                await db.exec(
                    "UPDATE documents SET status=?, signer_order_json=? WHERE id=?",
                    (status, json.dumps(signer_order, ensure_ascii=False), document_id),
                )
                await db.add_event(
                    case_id,
                    document_id,
                    "signature_queued_pending_grievance_number",
                    {"doc_type": doc_type},
                )
            else:
                outcome = await send_document_for_signature(
                    cfg=cfg,
                    db=db,
                    logger=logger,
                    docuseal=docuseal,
                    notifications=notifications,
                    case_id=case_id,
                    grievance_id=grievance_id,
                    document_id=document_id,
                    doc_type=doc_type,
                    template_key=doc_req.template_key,
                    pdf_bytes=pdf_bytes,
                    alignment_pdf_bytes=alignment_pdf_bytes,
                    signer_order=signer_order,
                    correlation_id=correlation_id,
                    idempotency_prefix=f"intake:{case_id}:{document_id}",
                )
                status = outcome.status
                signing_link = outcome.signing_link
                any_signature_requested = any_signature_requested or status == "sent_for_signature"
                any_failed = any_failed or status == "failed"
        else:
            status = "pending_approval"
            await db.exec("UPDATE documents SET status=? WHERE id=?", (status, document_id))
            await db.add_event(case_id, document_id, "no_signature_required", {})

        doc_statuses.append(
            DocumentStatus(document_id=document_id, doc_type=doc_type, status=status, signing_link=signing_link)
        )

    if any_signature_requested:
        case_status = "awaiting_signatures"
    elif any_signature_queued:
        case_status = "pending_grievance_number"
    elif any_failed:
        case_status = "failed"
    else:
        case_status = "pending_approval"

    await db.exec("UPDATE cases SET status=? WHERE id=?", (case_status, case_id))
    await db.add_event(case_id, None, "intake_completed", {"status": case_status, "document_count": len(doc_statuses)})

    return IntakeResponse(case_id=case_id, grievance_id=grievance_id, status=case_status, documents=doc_statuses)
