from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from ..core.config import EmailConfig
from ..db.db import Db
from .email_templates import EmailTemplateStore
from .graph_mail import GraphMailer, MailAttachment


@dataclass(frozen=True)
class NotificationResult:
    recipient_email: str
    status: str
    graph_message_id: str | None
    internet_message_id: str | None
    resend_count: int
    deduped: bool


def _parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class NotificationService:
    def __init__(
        self,
        *,
        db: Db,
        logger: logging.Logger,
        mailer: GraphMailer | None,
        template_store: EmailTemplateStore,
        email_cfg: EmailConfig,
    ):
        self.db = db
        self.logger = logger
        self.mailer = mailer
        self.template_store = template_store
        self.email_cfg = email_cfg

    def _resolve_test_mode(self, *, form_key: str | None, override: bool | None) -> bool:
        if override is not None:
            return bool(override)

        if form_key:
            key = str(form_key).strip()
            if key in self.email_cfg.test_mode_by_form:
                return bool(self.email_cfg.test_mode_by_form[key])
            lowered = key.lower()
            if lowered in self.email_cfg.test_mode_by_form:
                return bool(self.email_cfg.test_mode_by_form[lowered])

        return bool(self.email_cfg.test_mode)

    async def send_one(
        self,
        *,
        case_id: str,
        template_key: str,
        recipient_email: str,
        context: dict[str, object],
        idempotency_key: str,
        document_id: str | None = None,
        attachments: list[MailAttachment] | None = None,
        allow_resend: bool = False,
        form_key: str | None = None,
        test_mode_override: bool | None = None,
        scope_kind: str = "case",
    ) -> NotificationResult:
        scope = str(scope_kind or "case").strip().lower()
        if scope not in {"case", "standalone"}:
            raise RuntimeError(f"unsupported notification scope_kind: {scope_kind}")

        workflow_id = case_id
        recipient = recipient_email.strip()
        if not recipient:
            raise RuntimeError("recipient_email is required")
        if not self.email_cfg.enabled:
            raise RuntimeError("email delivery disabled in config")
        if self.mailer is None:
            raise RuntimeError("Graph mailer is not configured")

        doc_scope = document_id or ""
        if scope == "standalone":
            existing = await self.db.standalone_outbound_email_by_idempotency(
                submission_id=workflow_id,
                document_scope_id=doc_scope,
                template_key=template_key,
                recipient_email=recipient,
                idempotency_key=idempotency_key,
            )
        else:
            existing = await self.db.outbound_email_by_idempotency(
                case_id=workflow_id,
                document_scope_id=doc_scope,
                template_key=template_key,
                recipient_email=recipient,
                idempotency_key=idempotency_key,
            )
        if existing and str(existing[1]) == "sent":
            return NotificationResult(
                recipient_email=recipient,
                status="sent",
                graph_message_id=existing[2],
                internet_message_id=existing[3],
                resend_count=int(existing[4]),
                deduped=True,
            )

        if existing:
            row_id = int(existing[0])
            resend_count = int(existing[4] or 0)
            if scope == "standalone":
                await self.db.mark_standalone_outbound_email_pending(row_id=row_id)
            else:
                await self.db.mark_outbound_email_pending(row_id=row_id)
        else:
            resend_count = 0
            if allow_resend:
                if scope == "standalone":
                    resend_count = await self.db.next_standalone_resend_count(
                        submission_id=workflow_id,
                        document_scope_id=doc_scope,
                        template_key=template_key,
                        recipient_email=recipient,
                    )
                    row = await self.db.fetchone(
                        """SELECT last_sent_at_utc
                           FROM standalone_outbound_emails
                           WHERE submission_id=? AND document_scope_id=? AND template_key=? AND recipient_email=? AND status='sent'
                           ORDER BY last_sent_at_utc DESC LIMIT 1""",
                        (workflow_id, doc_scope, template_key, recipient),
                    )
                else:
                    resend_count = await self.db.next_resend_count(
                        case_id=workflow_id,
                        document_scope_id=doc_scope,
                        template_key=template_key,
                        recipient_email=recipient,
                    )
                    row = await self.db.fetchone(
                        """SELECT last_sent_at_utc
                           FROM outbound_emails
                           WHERE case_id=? AND document_scope_id=? AND template_key=? AND recipient_email=? AND status='sent'
                           ORDER BY last_sent_at_utc DESC LIMIT 1""",
                        (workflow_id, doc_scope, template_key, recipient),
                    )
                last_sent = _parse_iso_utc(row[0] if row else None)
                now = datetime.now(timezone.utc)
                if (
                    last_sent
                    and self.email_cfg.resend_cooldown_seconds > 0
                    and (now - last_sent).total_seconds() < self.email_cfg.resend_cooldown_seconds
                ):
                    raise RuntimeError("resend cooldown active; retry later")

            metadata = {
                "recipient_email": recipient,
                "template_key": template_key,
                "document_id": document_id,
                "scope_kind": scope,
            }
            if scope == "standalone":
                row_id = await self.db.create_standalone_outbound_email(
                    submission_id=workflow_id,
                    document_scope_id=doc_scope,
                    template_key=template_key,
                    recipient_email=recipient,
                    idempotency_key=idempotency_key,
                    status="pending",
                    resend_count=resend_count,
                    metadata=metadata,
                )
            else:
                row_id = await self.db.create_outbound_email(
                    case_id=workflow_id,
                    document_scope_id=doc_scope,
                    template_key=template_key,
                    recipient_email=recipient,
                    idempotency_key=idempotency_key,
                    status="pending",
                    resend_count=resend_count,
                    metadata=metadata,
                )

        rendered = self.template_store.render(template_key, context)
        subject = rendered.subject
        text_body = rendered.text_body
        html_body = rendered.html_body
        if self._resolve_test_mode(form_key=form_key, override=test_mode_override):
            if not subject.upper().startswith("[TEST]"):
                subject = f"[TEST] {subject}"
            test_text_banner = "TEST MESSAGE: this is a test workflow email.\n\n"
            text_body = f"{test_text_banner}{text_body}"
            if html_body:
                html_body = (
                    "<p><strong>TEST MESSAGE:</strong> this is a test workflow email.</p>"
                    f"{html_body}"
                )
        try:
            custom_headers = {
                "X-Document-ID": document_id or "",
                "X-Template-Key": template_key,
                "X-Idempotency-Key": idempotency_key,
                "X-Workflow-Kind": scope,
            }
            if scope == "standalone":
                custom_headers["X-Submission-ID"] = workflow_id
            else:
                custom_headers["X-Case-ID"] = workflow_id
            sent = self.mailer.send_mail(
                to_recipients=[recipient],
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                attachments=attachments,
                custom_headers=custom_headers,
            )
            if scope == "standalone":
                await self.db.mark_standalone_outbound_email_sent(
                    row_id=row_id,
                    graph_message_id=sent.graph_message_id,
                    internet_message_id=sent.internet_message_id,
                )
                await self.db.add_standalone_event(
                    workflow_id,
                    document_id,
                    "email_sent",
                    {
                        "template_key": template_key,
                        "recipient_email": recipient,
                        "graph_message_id": sent.graph_message_id,
                        "resend_count": resend_count,
                    },
                )
            else:
                await self.db.mark_outbound_email_sent(
                    row_id=row_id,
                    graph_message_id=sent.graph_message_id,
                    internet_message_id=sent.internet_message_id,
                )
                await self.db.add_event(
                    workflow_id,
                    document_id,
                    "email_sent",
                    {
                        "template_key": template_key,
                        "recipient_email": recipient,
                        "graph_message_id": sent.graph_message_id,
                        "resend_count": resend_count,
                    },
                )
            return NotificationResult(
                recipient_email=recipient,
                status="sent",
                graph_message_id=sent.graph_message_id,
                internet_message_id=sent.internet_message_id,
                resend_count=resend_count,
                deduped=False,
            )
        except Exception as exc:
            if scope == "standalone":
                await self.db.mark_standalone_outbound_email_failed(row_id=row_id, error_message=str(exc))
                await self.db.add_standalone_event(
                    workflow_id,
                    document_id,
                    "email_send_failed",
                    {
                        "template_key": template_key,
                        "recipient_email": recipient,
                    },
                )
            else:
                await self.db.mark_outbound_email_failed(row_id=row_id, error_message=str(exc))
                await self.db.add_event(
                    workflow_id,
                    document_id,
                    "email_send_failed",
                    {
                        "template_key": template_key,
                        "recipient_email": recipient,
                    },
                )
            self.logger.exception("email_send_failed", extra={"correlation_id": workflow_id})
            raise
