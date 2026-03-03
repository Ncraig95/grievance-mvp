from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..db.db import Db
from .notification_service import NotificationService


@dataclass(frozen=True)
class SignatureDispatchOutcome:
    status: str
    signing_link: str | None = None


def resolve_form_key(*, template_key: str | None, doc_type: str) -> str:
    for key in (template_key, doc_type):
        text = str(key or "").strip()
        if text:
            return text
    return "document"


def resolve_docuseal_template_id(cfg, *, template_key: str | None, doc_type: str) -> int | None:  # noqa: ANN001
    lookup_order: list[str] = []
    for key in (template_key, doc_type):
        text = str(key or "").strip()
        if text and text not in lookup_order:
            lookup_order.append(text)

    for key in lookup_order:
        if key in cfg.docuseal.template_ids:
            return cfg.docuseal.template_ids[key]

    if getattr(cfg.docuseal, "strict_template_ids", False):
        form_key = resolve_form_key(template_key=template_key, doc_type=doc_type)
        raise RuntimeError(
            f"No DocuSeal template id configured for form '{form_key}'. "
            "Add docuseal.template_ids.<form_key> in config."
        )
    return cfg.docuseal.default_template_id


def normalize_signers(signers: list[str] | None, fallback_email: str | None) -> list[str]:
    ordered = [s.strip() for s in (signers or []) if s and s.strip()]
    if ordered:
        return ordered
    if fallback_email and fallback_email.strip():
        return [fallback_email.strip()]
    return []


def signer_order_from_json(raw_json: str | None, fallback_email: str | None) -> list[str]:
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                normalized = normalize_signers([str(v) for v in parsed], fallback_email)
                if normalized:
                    return normalized
        except Exception:
            pass
    return normalize_signers(None, fallback_email)


async def send_document_for_signature(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    logger: logging.Logger,
    docuseal,  # noqa: ANN001
    notifications: NotificationService,
    case_id: str,
    grievance_id: str,
    document_id: str,
    doc_type: str,
    template_key: str | None,
    pdf_bytes: bytes,
    alignment_pdf_bytes: bytes | None,
    signer_order: list[str],
    correlation_id: str,
    idempotency_prefix: str,
) -> SignatureDispatchOutcome:
    normalized_signers = normalize_signers(signer_order, fallback_email=None)
    if not normalized_signers:
        await db.exec(
            "UPDATE documents SET status='failed', signer_order_json=? WHERE id=?",
            (json.dumps([], ensure_ascii=False), document_id),
        )
        await db.add_event(
            case_id,
            document_id,
            "docuseal_create_failed",
            {"error": "no_signers", "doc_type": doc_type},
        )
        return SignatureDispatchOutcome(status="failed")

    try:
        form_key = resolve_form_key(template_key=template_key, doc_type=doc_type)
        submission = docuseal.create_submission(
            pdf_bytes=pdf_bytes,
            alignment_pdf_bytes=alignment_pdf_bytes,
            signers=normalized_signers,
            title=f"Grievance {grievance_id} - {doc_type}",
            metadata={
                "case_id": case_id,
                "document_id": document_id,
                "grievance_id": grievance_id,
                "doc_type": doc_type,
            },
            template_id=resolve_docuseal_template_id(cfg, template_key=template_key, doc_type=doc_type),
            form_key=form_key,
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
        await db.add_event(
            case_id,
            document_id,
            "signature_links_resolved",
            {
                "submission_id": submission.submission_id,
                "signer_count": len(normalized_signers),
                "resolved_link_count": len(signer_links_by_email),
                "has_fallback_link": bool(signing_link),
            },
        )
        status = "sent_for_signature"
        await db.exec(
            """UPDATE documents
               SET status=?, signer_order_json=?, docuseal_submission_id=?, docuseal_signing_link=?
               WHERE id=?""",
            (
                status,
                json.dumps(normalized_signers, ensure_ascii=False),
                submission.submission_id,
                signing_link,
                document_id,
            ),
        )
        await db.add_event(
            case_id,
            document_id,
            "sent_for_signature",
            {"submission_id": submission.submission_id, "doc_type": doc_type},
        )

        if cfg.email.enabled and (signing_link or signer_links_by_email):
            for signer in normalized_signers:
                signer_key = signer.strip().lower()
                signer_link = signer_links_by_email.get(signer_key) or signing_link
                if not signer_link:
                    continue
                try:
                    await notifications.send_one(
                        case_id=case_id,
                        document_id=document_id,
                        recipient_email=signer,
                        template_key="signature_request",
                        context={
                            "case_id": case_id,
                            "grievance_id": grievance_id,
                            "document_id": document_id,
                            "document_type": doc_type,
                            "docuseal_signing_url": signer_link,
                            "signer_email": signer,
                            "status": status,
                        },
                        idempotency_key=f"{idempotency_prefix}:signature_request:{signer.lower()}",
                        form_key=form_key,
                    )
                except Exception:
                    await db.add_event(
                        case_id,
                        document_id,
                        "signature_request_email_failed",
                        {"recipient": signer},
                    )
                    logger.exception(
                        "signature_request_email_failed",
                        extra={"correlation_id": correlation_id, "document_id": document_id},
                    )

        return SignatureDispatchOutcome(status=status, signing_link=signing_link)
    except Exception as exc:
        await db.exec(
            "UPDATE documents SET status='failed', signer_order_json=? WHERE id=?",
            (json.dumps(normalized_signers, ensure_ascii=False), document_id),
        )
        await db.add_event(
            case_id,
            document_id,
            "docuseal_create_failed",
            {"error": str(exc), "doc_type": doc_type},
        )
        logger.exception("docuseal_create_failed", extra={"correlation_id": correlation_id, "document_id": document_id})
        return SignatureDispatchOutcome(status="failed")
