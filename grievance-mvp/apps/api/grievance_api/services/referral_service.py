from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import csv
import hashlib
import io
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from ..core.config import EmailConfig, OfficerAuthConfig, ReferralConfig
from ..core.ids import new_referral_id
from ..db.db import Db, utcnow
from .email_templates import EmailTemplateStore
from .graph_mail import GraphMailer


REFERRAL_STATUSES = (
    "open",
    "contacted",
    "converted",
    "not_interested",
    "closed",
)
TERMINAL_REFERRAL_STATUSES = {"converted", "not_interested", "closed"}
REFERRAL_PROGRAM_SETTINGS_KEY = "referral_program"
DEFAULT_REFERRAL_SUNSET_DATE = "2027-02-05"


@dataclass(frozen=True)
class ReferralReminderResult:
    referral_id: str
    recipient_count: int
    status: str
    graph_message_id: str | None = None
    error_text: str | None = None


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_multiline(value: object) -> str:
    return str(value or "").strip()


def _normalize_email(value: object) -> str:
    return _normalize_text(value).lower()


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_local_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _normalize_sunset_date(value: object, default: object = DEFAULT_REFERRAL_SUNSET_DATE) -> str:
    parsed = _parse_local_date(value) or _parse_local_date(default) or date(2027, 2, 5)
    return parsed.isoformat()


def _hash_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public_base_url(email_cfg: EmailConfig, officer_auth_cfg: OfficerAuthConfig) -> str:
    for candidate in (email_cfg.approval_request_url_base, officer_auth_cfg.redirect_uri):
        parsed = urlsplit(str(candidate or "").strip())
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


class ReferralService:
    def __init__(
        self,
        *,
        db: Db,
        logger: logging.Logger,
        referral_cfg: ReferralConfig,
        email_cfg: EmailConfig,
        officer_auth_cfg: OfficerAuthConfig,
        mailer: GraphMailer | None,
        template_store: EmailTemplateStore,
    ):
        self.db = db
        self.logger = logger
        self.cfg = referral_cfg
        self.email_cfg = email_cfg
        self.officer_auth_cfg = officer_auth_cfg
        self.mailer = mailer
        self.template_store = template_store

    async def program_settings(self) -> dict[str, Any]:
        row = await self.db.app_setting(REFERRAL_PROGRAM_SETTINGS_KEY)
        raw_sunset_date: object = self.cfg.sunset_date
        updated_by: str | None = None
        updated_at_utc: str | None = None
        if row:
            try:
                parsed = json.loads(str(row[0] or "{}"))
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                raw_sunset_date = parsed.get("sunset_date") or raw_sunset_date
            updated_by = str(row[1]) if row[1] is not None else None
            updated_at_utc = str(row[2]) if row[2] is not None else None

        sunset_date = _normalize_sunset_date(raw_sunset_date, self.cfg.sunset_date)
        parsed_sunset = _parse_local_date(sunset_date) or date(2027, 2, 5)
        return {
            "enabled": bool(self.cfg.enabled),
            "sunset_date": sunset_date,
            "is_active": bool(self.cfg.enabled) and date.today() <= parsed_sunset,
            "updated_by": updated_by,
            "updated_at_utc": updated_at_utc,
        }

    async def update_program_settings(self, *, sunset_date: object, updated_by: str | None) -> dict[str, Any]:
        parsed_sunset = _parse_local_date(sunset_date)
        if not parsed_sunset:
            raise RuntimeError("sunset_date must be a YYYY-MM-DD date")
        await self.db.upsert_app_setting(
            setting_key=REFERRAL_PROGRAM_SETTINGS_KEY,
            setting={"sunset_date": parsed_sunset.isoformat()},
            updated_by=updated_by,
        )
        return await self.program_settings()

    async def create_referral(
        self,
        *,
        payload: dict[str, Any],
        client_ip: str | None,
        user_agent: str | None,
    ) -> dict[str, Any]:
        if not self.cfg.enabled:
            raise RuntimeError("referral tracking is disabled")

        request_id = _normalize_text(payload.get("request_id"))
        if not request_id:
            raise RuntimeError("request_id is required")

        existing = await self.db.fetchone("SELECT id FROM referrals WHERE request_id=?", (request_id,))
        if existing:
            return await self.get_referral(str(existing[0]))

        settings = await self.program_settings()
        if not settings["is_active"]:
            raise RuntimeError(
                f"referral program sunset date has passed ({settings['sunset_date']}); "
                "extend it before accepting new submissions"
            )

        referrer_name = self._required(payload, "referrer_name", "referrer_name is required")
        referrer_address = self._required(payload, "referrer_address", "referrer_address is required")
        referrer_phone = self._required(payload, "referrer_phone", "referrer_phone is required")
        referrer_group = self._required(payload, "referrer_group", "referrer_group is required")
        referred_name = self._required(payload, "referred_name", "referred_name is required")
        referrer_email = _normalize_email(payload.get("referrer_email"))
        if referrer_email and "@" not in referrer_email:
            raise RuntimeError("referrer_email must be a valid email address")

        created_at = utcnow()
        created_dt = _parse_iso(created_at) or datetime.now(timezone.utc)
        reminder_due = (created_dt + timedelta(days=int(self.cfg.reminder_days))).isoformat()
        referral_id = new_referral_id()
        await self.db.exec(
            """
            INSERT INTO referrals(
              id, request_id, created_at_utc, updated_at_utc, status, assignee, officer_notes,
              reminder_due_at_utc, reminder_attempted_at_utc, reminder_sent_at_utc, reminder_error,
              referrer_name, referrer_address, referrer_phone, referrer_email, referrer_group,
              referred_name, referred_group, referred_att_uid, referral_notes,
              submitter_ip_hash, submitter_user_agent_hash, source_payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                referral_id,
                request_id,
                created_at,
                created_at,
                "open",
                None,
                None,
                reminder_due,
                None,
                None,
                None,
                referrer_name,
                referrer_address,
                referrer_phone,
                referrer_email or None,
                referrer_group,
                referred_name,
                _normalize_text(payload.get("referred_group")) or None,
                _normalize_text(payload.get("referred_att_uid")) or None,
                _normalize_multiline(payload.get("referral_notes")) or None,
                _hash_text(client_ip),
                _hash_text(user_agent),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        return await self.get_referral(referral_id)

    @staticmethod
    def _required(payload: dict[str, Any], key: str, message: str) -> str:
        value = _normalize_text(payload.get(key))
        if not value:
            raise RuntimeError(message)
        return value

    async def get_referral(self, referral_id: str) -> dict[str, Any]:
        row = await self.db.fetchone(
            """
            SELECT id, request_id, created_at_utc, updated_at_utc, status, assignee, officer_notes,
                   reminder_due_at_utc, reminder_attempted_at_utc, reminder_sent_at_utc, reminder_error,
                   referrer_name, referrer_address, referrer_phone, referrer_email, referrer_group,
                   referred_name, referred_group, referred_att_uid, referral_notes,
                   submitter_ip_hash, submitter_user_agent_hash, source_payload_json
            FROM referrals
            WHERE id=?
            """,
            (referral_id,),
        )
        if not row:
            raise RuntimeError("referral not found")
        return self._row(row)

    async def list_referrals(
        self,
        *,
        search: str | None = None,
        group: str | None = None,
        status: str | None = None,
        assignee: str | None = None,
        reminder: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, request_id, created_at_utc, updated_at_utc, status, assignee, officer_notes,
                   reminder_due_at_utc, reminder_attempted_at_utc, reminder_sent_at_utc, reminder_error,
                   referrer_name, referrer_address, referrer_phone, referrer_email, referrer_group,
                   referred_name, referred_group, referred_att_uid, referral_notes,
                   submitter_ip_hash, submitter_user_agent_hash, source_payload_json
            FROM referrals
            ORDER BY created_at_utc DESC, id DESC
            """
        )
        items = [self._row(row) for row in rows]
        query = _normalize_text(search).lower()
        group_filter = _normalize_text(group).lower()
        status_filter = _normalize_text(status).lower()
        assignee_filter = _normalize_text(assignee).lower()
        reminder_filter = _normalize_text(reminder).lower()
        now = datetime.now(timezone.utc)

        def _matches(item: dict[str, Any]) -> bool:
            if query:
                haystack = " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "id",
                        "referrer_name",
                        "referrer_email",
                        "referrer_phone",
                        "referrer_group",
                        "referred_name",
                        "referred_group",
                        "referred_att_uid",
                        "referral_notes",
                    )
                ).lower()
                if query not in haystack:
                    return False
            if group_filter and group_filter not in " ".join(
                [
                    str(item.get("referrer_group") or "").lower(),
                    str(item.get("referred_group") or "").lower(),
                ]
            ):
                return False
            if status_filter and status_filter != str(item.get("status") or "").lower():
                return False
            if assignee_filter and assignee_filter != str(item.get("assignee") or "").lower():
                return False
            if reminder_filter:
                due_at = _parse_iso(item.get("reminder_due_at_utc"))
                sent = bool(item.get("reminder_sent_at_utc"))
                terminal = str(item.get("status") or "") in TERMINAL_REFERRAL_STATUSES
                if reminder_filter in {"due", "overdue"} and (sent or terminal or not due_at or due_at > now):
                    return False
                if reminder_filter == "sent" and not sent:
                    return False
                if reminder_filter == "upcoming" and (sent or terminal or not due_at or due_at <= now):
                    return False
            return True

        return [item for item in items if _matches(item)]

    async def update_referral(self, referral_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = await self.get_referral(referral_id)
        updates: dict[str, Any] = {}

        if "status" in payload:
            status = _normalize_text(payload.get("status")).lower()
            if status not in REFERRAL_STATUSES:
                raise RuntimeError("invalid referral status")
            updates["status"] = status
        for key, column in (
            ("assignee", "assignee"),
            ("officer_notes", "officer_notes"),
            ("referred_group", "referred_group"),
            ("referred_att_uid", "referred_att_uid"),
            ("reminder_due_at_utc", "reminder_due_at_utc"),
        ):
            if key not in payload:
                continue
            value = _normalize_multiline(payload.get(key)) if key == "officer_notes" else _normalize_text(payload.get(key))
            updates[column] = value or None

        if "reminder_due_at_utc" in updates and updates["reminder_due_at_utc"]:
            parsed = _parse_iso(updates["reminder_due_at_utc"])
            if not parsed:
                raise RuntimeError("reminder_due_at_utc must be an ISO timestamp")
            updates["reminder_due_at_utc"] = parsed.isoformat()

        if not updates:
            return existing
        updates["updated_at_utc"] = utcnow()
        assignments = ", ".join(f"{column}=?" for column in updates)
        await self.db.exec(
            f"UPDATE referrals SET {assignments} WHERE id=?",
            tuple(updates.values()) + (referral_id,),
        )
        return await self.get_referral(referral_id)

    async def delete_referral(self, referral_id: str) -> dict[str, Any]:
        existing = await self.get_referral(referral_id)
        await self.db.exec("DELETE FROM referrals WHERE id=?", (referral_id,))
        return existing

    async def export_csv(self, rows: list[dict[str, Any]]) -> str:
        output = io.StringIO()
        fieldnames = [
            "id",
            "status",
            "created_at_utc",
            "reminder_due_at_utc",
            "reminder_sent_at_utc",
            "assignee",
            "referrer_name",
            "referrer_phone",
            "referrer_email",
            "referrer_address",
            "referrer_group",
            "referred_name",
            "referred_group",
            "referred_att_uid",
            "referral_notes",
            "officer_notes",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()

    async def run_due(self) -> dict[str, Any]:
        if not self.cfg.enabled:
            return {"processed_count": 0, "sent_count": 0, "failed_count": 0, "skipped_count": 0, "rows": []}
        if not self.email_cfg.enabled or self.mailer is None:
            raise RuntimeError("referral reminder email delivery is not configured")
        recipients = [email for email in self.cfg.notification_recipients if _normalize_email(email)]
        if not recipients:
            raise RuntimeError("referrals.notification_recipients must include at least one address")

        now = datetime.now(timezone.utc).isoformat()
        rows = await self.db.fetchall(
            """
            SELECT id, request_id, created_at_utc, updated_at_utc, status, assignee, officer_notes,
                   reminder_due_at_utc, reminder_attempted_at_utc, reminder_sent_at_utc, reminder_error,
                   referrer_name, referrer_address, referrer_phone, referrer_email, referrer_group,
                   referred_name, referred_group, referred_att_uid, referral_notes,
                   submitter_ip_hash, submitter_user_agent_hash, source_payload_json
            FROM referrals
            WHERE reminder_sent_at_utc IS NULL
              AND reminder_due_at_utc<=?
              AND status NOT IN ('converted', 'not_interested', 'closed')
            ORDER BY reminder_due_at_utc ASC, created_at_utc ASC
            """,
            (now,),
        )
        processed = 0
        sent = 0
        failed = 0
        results: list[ReferralReminderResult] = []
        for row in rows:
            processed += 1
            item = self._row(row)
            result = await self._send_reminder(item, recipients)
            results.append(result)
            if result.status == "sent":
                sent += 1
            else:
                failed += 1
        return {
            "processed_count": processed,
            "sent_count": sent,
            "failed_count": failed,
            "skipped_count": 0,
            "rows": [
                {
                    "referral_id": item.referral_id,
                    "recipient_count": item.recipient_count,
                    "status": item.status,
                    "graph_message_id": item.graph_message_id,
                    "error_text": item.error_text,
                }
                for item in results
            ],
        }

    async def _send_reminder(self, item: dict[str, Any], recipients: list[str]) -> ReferralReminderResult:
        referral_id = str(item["id"])
        now = utcnow()
        context = self._notification_context(item)
        rendered = self.template_store.render("referral_reminder", context)
        await self.db.exec(
            """
            UPDATE referrals
            SET reminder_attempted_at_utc=?, reminder_error=NULL, updated_at_utc=?
            WHERE id=?
            """,
            (now, now, referral_id),
        )
        try:
            sent = self.mailer.send_mail(
                to_recipients=recipients,
                subject=rendered.subject,
                text_body=rendered.text_body,
                html_body=rendered.html_body,
                custom_headers={
                    "X-Referral-ID": referral_id,
                    "X-Template-Key": "referral_reminder",
                },
            )
        except Exception as exc:
            error_text = str(exc)
            await self.db.exec(
                """
                UPDATE referrals
                SET reminder_error=?, updated_at_utc=?
                WHERE id=?
                """,
                (error_text, utcnow(), referral_id),
            )
            self.logger.exception("referral_reminder_failed", extra={"correlation_id": referral_id})
            return ReferralReminderResult(
                referral_id=referral_id,
                recipient_count=len(recipients),
                status="failed",
                error_text=error_text,
            )

        await self.db.exec(
            """
            UPDATE referrals
            SET reminder_sent_at_utc=?, reminder_error=NULL, updated_at_utc=?
            WHERE id=?
            """,
            (utcnow(), utcnow(), referral_id),
        )
        return ReferralReminderResult(
            referral_id=referral_id,
            recipient_count=len(recipients),
            status="sent",
            graph_message_id=sent.graph_message_id,
        )

    def _notification_context(self, item: dict[str, Any]) -> dict[str, object]:
        base_url = _public_base_url(self.email_cfg, self.officer_auth_cfg)
        portal_url = f"{base_url}/officers/referrals" if base_url else "/officers/referrals"
        return {
            "referral_id": item["id"],
            "status": item["status"],
            "created_at_utc": item["created_at_utc"],
            "reminder_due_at_utc": item["reminder_due_at_utc"],
            "referrer_name": item["referrer_name"],
            "referrer_phone": item["referrer_phone"],
            "referrer_email": item.get("referrer_email") or "",
            "referrer_address": item["referrer_address"],
            "referrer_group": item["referrer_group"],
            "referred_name": item["referred_name"],
            "referred_group": item.get("referred_group") or "",
            "referred_att_uid": item.get("referred_att_uid") or "",
            "referral_notes": item.get("referral_notes") or "",
            "portal_url": portal_url,
        }

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "id": str(row[0] or ""),
            "request_id": str(row[1] or ""),
            "created_at_utc": str(row[2] or ""),
            "updated_at_utc": str(row[3] or ""),
            "status": str(row[4] or "open"),
            "assignee": str(row[5]) if row[5] is not None else None,
            "officer_notes": str(row[6]) if row[6] is not None else None,
            "reminder_due_at_utc": str(row[7] or ""),
            "reminder_attempted_at_utc": str(row[8]) if row[8] is not None else None,
            "reminder_sent_at_utc": str(row[9]) if row[9] is not None else None,
            "reminder_error": str(row[10]) if row[10] is not None else None,
            "referrer_name": str(row[11] or ""),
            "referrer_address": str(row[12] or ""),
            "referrer_phone": str(row[13] or ""),
            "referrer_email": str(row[14]) if row[14] is not None else None,
            "referrer_group": str(row[15] or ""),
            "referred_name": str(row[16] or ""),
            "referred_group": str(row[17]) if row[17] is not None else None,
            "referred_att_uid": str(row[18]) if row[18] is not None else None,
            "referral_notes": str(row[19]) if row[19] is not None else None,
            "submitter_ip_hash": str(row[20]) if row[20] is not None else None,
            "submitter_user_agent_hash": str(row[21]) if row[21] is not None else None,
            "source_payload_json": str(row[22] or "{}"),
        }
