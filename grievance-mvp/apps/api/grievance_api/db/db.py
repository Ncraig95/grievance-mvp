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

    async def add_event(self, grievance_id: str, event_type: str, details: dict) -> None:
        await self.exec(
            "INSERT INTO events(grievance_id, ts_utc, event_type, details_json) VALUES(?,?,?,?)",
            (grievance_id, utcnow(), event_type, json.dumps(details, ensure_ascii=False)),
        )

    async def receipt_seen(self, provider: str, receipt_key: str) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM webhook_receipts WHERE provider=? AND receipt_key=?",
            (provider, receipt_key),
        )
        return row is not None

    async def store_receipt(self, provider: str, receipt_key: str, raw_body: str) -> None:
        await self.exec(
            "INSERT INTO webhook_receipts(provider, receipt_key, ts_utc, raw_body, handled) VALUES(?,?,?,?,0)",
            (provider, receipt_key, utcnow(), raw_body),
        )
