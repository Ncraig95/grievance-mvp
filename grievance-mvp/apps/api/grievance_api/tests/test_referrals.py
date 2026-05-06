from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest

from grievance_api.core.config import EmailConfig, OfficerAuthConfig, ReferralConfig
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services.email_templates import EmailTemplateStore
from grievance_api.services.graph_mail import SentGraphMail
from grievance_api.services.referral_service import ReferralService


class _FakeMailer:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def send_mail(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return SentGraphMail(graph_message_id=f"graph-{len(self.calls)}", internet_message_id=None)


class _FailingMailer:
    def send_mail(self, **kwargs):  # noqa: ANN003
        _ = kwargs
        raise RuntimeError("graph denied")


class ReferralTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "referrals.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)
        self.templates_dir = Path(self.tmpdir.name) / "templates"
        self.templates_dir.mkdir()
        (self.templates_dir / "referral_reminder.subject.txt").write_text(
            "Referral due: $referred_name",
            encoding="utf-8",
        )
        (self.templates_dir / "referral_reminder.txt").write_text(
            "$referral_id $referrer_name $referred_name $portal_url",
            encoding="utf-8",
        )

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    def _service(
        self,
        *,
        mailer=None,
        enabled: bool = True,
        sunset_date: str | None = None,
    ) -> ReferralService:  # noqa: ANN001
        active_sunset_date = sunset_date or (date.today() + timedelta(days=365)).isoformat()
        return ReferralService(
            db=self.db,
            logger=logging.getLogger("test.referrals"),
            referral_cfg=ReferralConfig(
                enabled=enabled,
                reminder_days=60,
                notification_recipients=("officer@cwa3106.com",),
                sunset_date=active_sunset_date,
            ),
            email_cfg=EmailConfig(
                enabled=True,
                sender_user_id="do-not-reply@example.org",
                templates_dir=str(self.templates_dir),
                internal_recipients=(),
                derek_email=None,
                approval_request_url_base="https://grievance.example.org/cases",
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=300,
                dry_run=False,
            ),
            officer_auth_cfg=OfficerAuthConfig(enabled=False, redirect_uri="https://grievance.example.org/auth/callback"),
            mailer=mailer,
            template_store=EmailTemplateStore(str(self.templates_dir)),
        )

    @staticmethod
    def _payload(request_id: str = "req-1") -> dict[str, object]:
        return {
            "request_id": request_id,
            "referrer_name": "Taylor Referrer",
            "referrer_address": "1 Union Way",
            "referrer_phone": "904-555-0100",
            "referrer_email": "taylor@example.org",
            "referrer_group": "Utilities",
            "referred_name": "Jordan Referred",
            "referred_group": "Utilities",
            "referred_att_uid": "JR1234",
            "referral_notes": "Follow up after work.",
        }

    async def test_migration_creates_referral_tables_and_indexes(self):
        con = sqlite3.connect(self.db_path)
        try:
            cols = {str(row[1]) for row in con.execute("PRAGMA table_info(referrals)").fetchall()}
            indexes = {str(row[1]) for row in con.execute("PRAGMA index_list(referrals)").fetchall()}
        finally:
            con.close()

        self.assertIn("referrer_address", cols)
        self.assertIn("referred_att_uid", cols)
        self.assertIn("reminder_sent_at_utc", cols)
        self.assertIn("idx_referrals_request_id", indexes)

    async def test_program_settings_default_and_update_sunset_date(self):
        service = self._service(mailer=_FakeMailer(), sunset_date="2027-02-05")

        default_settings = await service.program_settings()
        updated_settings = await service.update_program_settings(
            sunset_date="2027-08-05",
            updated_by="Officer One",
        )

        self.assertEqual(default_settings["sunset_date"], "2027-02-05")
        self.assertEqual(updated_settings["sunset_date"], "2027-08-05")
        self.assertEqual(updated_settings["updated_by"], "Officer One")

    async def test_submission_blocks_new_referral_after_sunset_but_keeps_dedupe(self):
        service = self._service(mailer=_FakeMailer())
        existing = await service.create_referral(payload=self._payload("same"), client_ip=None, user_agent=None)
        await service.update_program_settings(sunset_date="2000-01-01", updated_by="Officer One")

        replay = await service.create_referral(payload=self._payload("same"), client_ip=None, user_agent=None)
        with self.assertRaisesRegex(RuntimeError, "sunset date has passed"):
            await service.create_referral(payload=self._payload("new"), client_ip=None, user_agent=None)

        self.assertEqual(replay["id"], existing["id"])

    async def test_submission_dedupes_and_sets_60_day_due_date(self):
        service = self._service(mailer=_FakeMailer())
        first = await service.create_referral(payload=self._payload(), client_ip="127.0.0.1", user_agent="test")
        second = await service.create_referral(payload=self._payload(), client_ip="127.0.0.1", user_agent="test")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["referred_att_uid"], "JR1234")
        created = datetime.fromisoformat(first["created_at_utc"])
        due = datetime.fromisoformat(first["reminder_due_at_utc"])
        self.assertEqual((due - created).days, 60)

    async def test_submission_validates_required_fields(self):
        service = self._service(mailer=_FakeMailer())
        payload = self._payload()
        payload["referrer_phone"] = ""

        with self.assertRaisesRegex(RuntimeError, "referrer_phone is required"):
            await service.create_referral(payload=payload, client_ip=None, user_agent=None)

    async def test_run_due_sends_once_and_skips_terminal_status(self):
        fake = _FakeMailer()
        service = self._service(mailer=fake)
        due_referral = await service.create_referral(payload=self._payload("due"), client_ip=None, user_agent=None)
        terminal_referral = await service.create_referral(payload=self._payload("terminal"), client_ip=None, user_agent=None)
        past_due = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await self.db.exec("UPDATE referrals SET reminder_due_at_utc=? WHERE id=?", (past_due, due_referral["id"]))
        await self.db.exec(
            "UPDATE referrals SET status='closed', reminder_due_at_utc=? WHERE id=?",
            (past_due, terminal_referral["id"]),
        )

        first = await service.run_due()
        second = await service.run_due()

        self.assertEqual(first["sent_count"], 1)
        self.assertEqual(first["failed_count"], 0)
        self.assertEqual(second["sent_count"], 0)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["to_recipients"], ["officer@cwa3106.com"])

    async def test_run_due_records_failures_without_marking_sent(self):
        service = self._service(mailer=_FailingMailer())
        referral = await service.create_referral(payload=self._payload("fail"), client_ip=None, user_agent=None)
        past_due = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        await self.db.exec("UPDATE referrals SET reminder_due_at_utc=? WHERE id=?", (past_due, referral["id"]))

        result = await service.run_due()
        row = await service.get_referral(referral["id"])

        self.assertEqual(result["failed_count"], 1)
        self.assertIsNone(row["reminder_sent_at_utc"])
        self.assertIn("graph denied", row["reminder_error"])
