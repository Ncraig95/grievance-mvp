
from __future__ import annotations

import json
from datetime import datetime, timezone
import aiosqlite

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

class Db:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def exec(self, sql: str, params: tuple = ()) -> None:
        async with aiosqlite.connect(self.db_path) as con:
            await con.execute(sql, params)
            await con.commit()

    async def fetchone(self, sql: str, params: tuple = ()):
        async with aiosqlite.connect(self.db_path) as con:
            cur = await con.execute(sql, params)
            row = await cur.fetchone()
            return row

    async def fetchall(self, sql: str, params: tuple = ()):
        async with aiosqlite.connect(self.db_path) as con:
            cur = await con.execute(sql, params)
            rows = await cur.fetchall()
            return rows

<<<<<<< HEAD
    async def insert(self, sql: str, params: tuple = ()) -> int:
        async with aiosqlite.connect(self.db_path) as con:
            cur = await con.execute(sql, params)
            await con.commit()
            return int(cur.lastrowid)

    async def add_event(self, grievance_id: str, event_type: str, details: dict) -> None:
=======
    async def add_event(self, case_id: str, document_id: str | None, event_type: str, details: dict) -> None:
>>>>>>> Firebase-Studio-Test-run
        await self.exec(
            "INSERT INTO events(case_id, document_id, ts_utc, event_type, details_json) VALUES(?,?,?,?,?)",
            (case_id, document_id, utcnow(), event_type, json.dumps(details, ensure_ascii=False)),
        )

    async def receipt_seen(self, provider: str, receipt_key: str) -> bool:
        row = await self.fetchone(
            "SELECT handled FROM webhook_receipts WHERE provider=? AND receipt_key=?",
            (provider, receipt_key),
        )
        return bool(row and int(row[0]) == 1)

    async def store_receipt(self, provider: str, receipt_key: str, raw_body: str) -> None:
        await self.exec(
            "INSERT OR IGNORE INTO webhook_receipts(provider, receipt_key, ts_utc, raw_body, handled) VALUES(?,?,?,?,0)",
            (provider, receipt_key, utcnow(), raw_body),
        )

    async def mark_receipt_handled(self, provider: str, receipt_key: str) -> None:
        await self.exec(
            "UPDATE webhook_receipts SET handled=1 WHERE provider=? AND receipt_key=?",
            (provider, receipt_key),
        )

    async def outbound_email_by_idempotency(
        self,
        *,
        grievance_id: str,
        template_key: str,
        recipient_email: str,
        idempotency_key: str,
    ):
        return await self.fetchone(
            """SELECT id, status, graph_message_id, internet_message_id, resend_count, last_sent_at_utc
               FROM outbound_emails
               WHERE grievance_id=? AND template_key=? AND recipient_email=? AND idempotency_key=?""",
            (grievance_id, template_key, recipient_email, idempotency_key),
        )

    async def next_resend_count(self, *, grievance_id: str, template_key: str, recipient_email: str) -> int:
        row = await self.fetchone(
            """SELECT COALESCE(MAX(resend_count), -1)
               FROM outbound_emails
               WHERE grievance_id=? AND template_key=? AND recipient_email=? AND status='sent'""",
            (grievance_id, template_key, recipient_email),
        )
        if not row:
            return 0
        return int(row[0]) + 1

    async def create_outbound_email(
        self,
        *,
        grievance_id: str,
        template_key: str,
        recipient_email: str,
        idempotency_key: str,
        status: str,
        resend_count: int,
        metadata: dict,
    ) -> int:
        ts = utcnow()
        return await self.insert(
            """INSERT INTO outbound_emails(
                 grievance_id, template_key, recipient_email, idempotency_key, status,
                 resend_count, created_at_utc, updated_at_utc, metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                grievance_id,
                template_key,
                recipient_email,
                idempotency_key,
                status,
                resend_count,
                ts,
                ts,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )

    async def mark_outbound_email_sent(
        self,
        *,
        row_id: int,
        graph_message_id: str,
        internet_message_id: str | None,
    ) -> None:
        now = utcnow()
        await self.exec(
            """UPDATE outbound_emails
               SET status='sent',
                   graph_message_id=?,
                   internet_message_id=?,
                   last_sent_at_utc=?,
                   updated_at_utc=?
               WHERE id=?""",
            (graph_message_id, internet_message_id, now, now, row_id),
        )

    async def mark_outbound_email_pending(self, *, row_id: int) -> None:
        now = utcnow()
        await self.exec(
            """UPDATE outbound_emails
               SET status='pending',
                   updated_at_utc=?
               WHERE id=?""",
            (now, row_id),
        )

    async def mark_outbound_email_failed(self, *, row_id: int, error_message: str) -> None:
        now = utcnow()
        await self.exec(
            """UPDATE outbound_emails
               SET status='failed',
                   updated_at_utc=?,
                   metadata_json=?
               WHERE id=?""",
            (now, json.dumps({"error": error_message}, ensure_ascii=False), row_id),
        )
