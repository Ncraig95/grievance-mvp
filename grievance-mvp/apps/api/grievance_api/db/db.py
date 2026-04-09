from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Db:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._table_columns_cache: dict[str, set[str]] = {}

    async def exec(self, sql: str, params: tuple = ()) -> None:
        async with aiosqlite.connect(self.db_path) as con:
            await con.execute(sql, params)
            await con.commit()

    async def insert(self, sql: str, params: tuple = ()) -> int:
        async with aiosqlite.connect(self.db_path) as con:
            cur = await con.execute(sql, params)
            await con.commit()
            return int(cur.lastrowid)

    async def fetchone(self, sql: str, params: tuple = ()):  # noqa: ANN001
        async with aiosqlite.connect(self.db_path) as con:
            cur = await con.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):  # noqa: ANN001
        async with aiosqlite.connect(self.db_path) as con:
            cur = await con.execute(sql, params)
            return await cur.fetchall()

    async def reserve_next_grievance_seq(self, *, year: int, floor_seq: int) -> int:
        now = utcnow()
        floor = max(0, int(floor_seq))
        async with aiosqlite.connect(self.db_path) as con:
            await con.execute("BEGIN IMMEDIATE")
            cur = await con.execute(
                "SELECT last_seq FROM grievance_id_sequences WHERE year=?",
                (year,),
            )
            row = await cur.fetchone()
            db_last = int(row[0]) if row else 0
            next_seq = max(floor, db_last) + 1
            await con.execute(
                """
                INSERT INTO grievance_id_sequences(year, last_seq, updated_at_utc)
                VALUES(?,?,?)
                ON CONFLICT(year) DO UPDATE SET
                  last_seq=excluded.last_seq,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (year, next_seq, now),
            )
            await con.commit()
            return next_seq

    async def ensure_standalone_submission_filing_assignment(
        self,
        *,
        submission_id: str,
        form_key: str,
        filing_year: int,
        root_folder: str,
        label_prefix: str,
        year_subfolders: bool,
    ) -> tuple[int, int, str, str]:
        normalized_root = str(root_folder or "").strip().strip("/")
        normalized_prefix = str(label_prefix or "").strip() or "Document"

        def _folder_path(*, year: int, label: str) -> str:
            parts = [normalized_root]
            if year_subfolders:
                parts.append(str(int(year)))
            parts.append(label.strip("/"))
            return "/".join(part for part in parts if part)

        async with aiosqlite.connect(self.db_path) as con:
            await con.execute("BEGIN IMMEDIATE")
            cur = await con.execute(
                """SELECT filing_year, filing_sequence, filing_label, COALESCE(sharepoint_folder_path, '')
                   FROM standalone_submissions
                   WHERE id=?""",
                (submission_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise RuntimeError(f"standalone submission not found: {submission_id}")

            existing_year = int(row[0]) if row[0] is not None else None
            existing_sequence = int(row[1]) if row[1] is not None else None
            existing_label = str(row[2] or "").strip()
            existing_folder = str(row[3] or "").strip()
            if existing_year and existing_sequence and existing_label:
                folder_path = existing_folder or _folder_path(year=existing_year, label=existing_label)
                if folder_path != existing_folder:
                    await con.execute(
                        "UPDATE standalone_submissions SET sharepoint_folder_path=? WHERE id=?",
                        (folder_path, submission_id),
                    )
                    await con.commit()
                else:
                    await con.commit()
                return existing_year, existing_sequence, existing_label, folder_path

            cur = await con.execute(
                """SELECT last_seq
                   FROM standalone_form_sequences
                   WHERE form_key=? AND year=?""",
                (form_key, filing_year),
            )
            seq_row = await cur.fetchone()
            next_sequence = (int(seq_row[0]) if seq_row else 0) + 1
            filing_label = f"{normalized_prefix} {next_sequence}"
            folder_path = _folder_path(year=filing_year, label=filing_label)
            await con.execute(
                """
                INSERT INTO standalone_form_sequences(form_key, year, last_seq, updated_at_utc)
                VALUES(?,?,?,?)
                ON CONFLICT(form_key, year) DO UPDATE SET
                  last_seq=excluded.last_seq,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (form_key, filing_year, next_sequence, utcnow()),
            )
            await con.execute(
                """UPDATE standalone_submissions
                   SET filing_year=?, filing_sequence=?, filing_label=?, sharepoint_folder_path=?
                   WHERE id=?""",
                (filing_year, next_sequence, filing_label, folder_path, submission_id),
            )
            await con.commit()
            return filing_year, next_sequence, filing_label, folder_path

    async def table_columns(self, table: str) -> set[str]:
        if table in self._table_columns_cache:
            return self._table_columns_cache[table]
        rows = await self.fetchall(f"PRAGMA table_info({table})")
        cols = {str(r[1]) for r in rows}
        self._table_columns_cache[table] = cols
        return cols

    async def hosted_form_settings_by_key(self) -> dict[str, tuple[str, int, str | None, str | None]]:
        rows = await self.fetchall(
            """SELECT form_key, visibility, enabled, updated_by, updated_at_utc
               FROM hosted_form_settings"""
        )
        return {
            str(row[0]): (
                str(row[1] or ""),
                int(row[2] or 0),
                str(row[3]) if row[3] is not None else None,
                str(row[4]) if row[4] is not None else None,
            )
            for row in rows
        }

    async def upsert_hosted_form_setting(
        self,
        *,
        form_key: str,
        visibility: str,
        enabled: bool,
        updated_by: str | None,
    ) -> None:
        await self.exec(
            """
            INSERT INTO hosted_form_settings(form_key, visibility, enabled, updated_by, updated_at_utc)
            VALUES(?,?,?,?,?)
            ON CONFLICT(form_key) DO UPDATE SET
              visibility=excluded.visibility,
              enabled=excluded.enabled,
              updated_by=excluded.updated_by,
              updated_at_utc=excluded.updated_at_utc
            """,
            (
                form_key,
                visibility,
                1 if enabled else 0,
                updated_by,
                utcnow(),
            ),
        )

    async def add_event(self, case_id: str, document_id: str | None, event_type: str, details: dict) -> None:
        cols = await self.table_columns("events")
        ts = utcnow()
        details_json = json.dumps(details, ensure_ascii=False)
        if "case_id" in cols and "grievance_id" in cols:
            if "document_id" in cols:
                await self.exec(
                    """INSERT INTO events(case_id, grievance_id, document_id, ts_utc, event_type, details_json)
                       VALUES(?,?,?,?,?,?)""",
                    (case_id, case_id, document_id, ts, event_type, details_json),
                )
            else:
                await self.exec(
                    """INSERT INTO events(case_id, grievance_id, ts_utc, event_type, details_json)
                       VALUES(?,?,?,?,?)""",
                    (case_id, case_id, ts, event_type, details_json),
                )
            return
        if "case_id" in cols:
            if "document_id" in cols:
                await self.exec(
                    "INSERT INTO events(case_id, document_id, ts_utc, event_type, details_json) VALUES(?,?,?,?,?)",
                    (case_id, document_id, ts, event_type, details_json),
                )
            else:
                await self.exec(
                    "INSERT INTO events(case_id, ts_utc, event_type, details_json) VALUES(?,?,?,?)",
                    (case_id, ts, event_type, details_json),
                )
            return
        if "grievance_id" in cols:
            await self.exec(
                "INSERT INTO events(grievance_id, ts_utc, event_type, details_json) VALUES(?,?,?,?)",
                (case_id, ts, event_type, details_json),
            )
            return
        raise RuntimeError("events table missing required case/grievance id column")

    async def add_standalone_event(
        self,
        submission_id: str,
        document_id: str | None,
        event_type: str,
        details: dict,
    ) -> None:
        await self.exec(
            """INSERT INTO standalone_events(submission_id, document_id, ts_utc, event_type, details_json)
               VALUES(?,?,?,?,?)""",
            (
                submission_id,
                document_id,
                utcnow(),
                event_type,
                json.dumps(details, ensure_ascii=False),
            ),
        )

    async def receipt_seen(self, provider: str, receipt_key: str) -> bool:
        row = await self.fetchone(
            "SELECT handled FROM webhook_receipts WHERE provider=? AND receipt_key=?",
            (provider, receipt_key),
        )
        return bool(row and int(row[0]) == 1)

    async def try_claim_receipt(self, provider: str, receipt_key: str, raw_body: str) -> bool:
        now = utcnow()
        async with aiosqlite.connect(self.db_path) as con:
            await con.execute("BEGIN IMMEDIATE")
            await con.execute(
                "INSERT OR IGNORE INTO webhook_receipts(provider, receipt_key, ts_utc, raw_body, handled) VALUES(?,?,?,?,0)",
                (provider, receipt_key, now, raw_body),
            )
            cur = await con.execute(
                """UPDATE webhook_receipts
                   SET handled=2, ts_utc=?, raw_body=?
                   WHERE provider=? AND receipt_key=? AND handled=0""",
                (now, raw_body, provider, receipt_key),
            )
            claimed = cur.rowcount > 0
            await con.commit()
            return claimed

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

    async def release_receipt_claim(self, provider: str, receipt_key: str) -> None:
        await self.exec(
            "UPDATE webhook_receipts SET handled=0 WHERE provider=? AND receipt_key=? AND handled=2",
            (provider, receipt_key),
        )

    async def outbound_email_by_idempotency(
        self,
        *,
        case_id: str,
        document_scope_id: str,
        template_key: str,
        recipient_email: str,
        idempotency_key: str,
    ):
        return await self.fetchone(
            """SELECT id, status, graph_message_id, internet_message_id, resend_count, last_sent_at_utc
               FROM outbound_emails
               WHERE case_id=? AND document_scope_id=? AND template_key=? AND recipient_email=? AND idempotency_key=?""",
            (case_id, document_scope_id, template_key, recipient_email, idempotency_key),
        )

    async def standalone_outbound_email_by_idempotency(
        self,
        *,
        submission_id: str,
        document_scope_id: str,
        template_key: str,
        recipient_email: str,
        idempotency_key: str,
    ):
        return await self.fetchone(
            """SELECT id, status, graph_message_id, internet_message_id, resend_count, last_sent_at_utc
               FROM standalone_outbound_emails
               WHERE submission_id=? AND document_scope_id=? AND template_key=? AND recipient_email=? AND idempotency_key=?""",
            (submission_id, document_scope_id, template_key, recipient_email, idempotency_key),
        )

    async def next_resend_count(
        self,
        *,
        case_id: str,
        document_scope_id: str,
        template_key: str,
        recipient_email: str,
    ) -> int:
        row = await self.fetchone(
            """SELECT COALESCE(MAX(resend_count), -1)
               FROM outbound_emails
               WHERE case_id=? AND document_scope_id=? AND template_key=? AND recipient_email=? AND status='sent'""",
            (case_id, document_scope_id, template_key, recipient_email),
        )
        if not row:
            return 0
        return int(row[0]) + 1

    async def next_standalone_resend_count(
        self,
        *,
        submission_id: str,
        document_scope_id: str,
        template_key: str,
        recipient_email: str,
    ) -> int:
        row = await self.fetchone(
            """SELECT COALESCE(MAX(resend_count), -1)
               FROM standalone_outbound_emails
               WHERE submission_id=? AND document_scope_id=? AND template_key=? AND recipient_email=? AND status='sent'""",
            (submission_id, document_scope_id, template_key, recipient_email),
        )
        if not row:
            return 0
        return int(row[0]) + 1

    async def create_outbound_email(
        self,
        *,
        case_id: str,
        document_scope_id: str,
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
                 case_id, document_scope_id, template_key, recipient_email, idempotency_key, status,
                 resend_count, created_at_utc, updated_at_utc, metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                case_id,
                document_scope_id,
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

    async def create_standalone_outbound_email(
        self,
        *,
        submission_id: str,
        document_scope_id: str,
        template_key: str,
        recipient_email: str,
        idempotency_key: str,
        status: str,
        resend_count: int,
        metadata: dict,
    ) -> int:
        ts = utcnow()
        return await self.insert(
            """INSERT INTO standalone_outbound_emails(
                 submission_id, document_scope_id, template_key, recipient_email, idempotency_key, status,
                 resend_count, created_at_utc, updated_at_utc, metadata_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                submission_id,
                document_scope_id,
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

    async def mark_standalone_outbound_email_sent(
        self,
        *,
        row_id: int,
        graph_message_id: str,
        internet_message_id: str | None,
    ) -> None:
        now = utcnow()
        await self.exec(
            """UPDATE standalone_outbound_emails
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
            "UPDATE outbound_emails SET status='pending', updated_at_utc=? WHERE id=?",
            (now, row_id),
        )

    async def mark_standalone_outbound_email_pending(self, *, row_id: int) -> None:
        now = utcnow()
        await self.exec(
            "UPDATE standalone_outbound_emails SET status='pending', updated_at_utc=? WHERE id=?",
            (now, row_id),
        )

    async def mark_outbound_email_failed(self, *, row_id: int, error_message: str) -> None:
        now = utcnow()
        await self.exec(
            "UPDATE outbound_emails SET status='failed', updated_at_utc=?, metadata_json=? WHERE id=?",
            (now, json.dumps({"error": error_message}, ensure_ascii=False), row_id),
        )

    async def mark_standalone_outbound_email_failed(self, *, row_id: int, error_message: str) -> None:
        now = utcnow()
        await self.exec(
            "UPDATE standalone_outbound_emails SET status='failed', updated_at_utc=?, metadata_json=? WHERE id=?",
            (now, json.dumps({"error": error_message}, ensure_ascii=False), row_id),
        )

    async def create_document_stage(
        self,
        *,
        case_id: str,
        document_id: str,
        stage_no: int,
        stage_key: str,
        status: str,
        signer_email: str,
        source_payload: dict | None = None,
    ) -> int:
        return await self.insert(
            """INSERT INTO document_stages(
                 case_id, document_id, stage_no, stage_key, status, signer_email, source_payload_json, started_at_utc
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                case_id,
                document_id,
                int(stage_no),
                stage_key,
                status,
                signer_email,
                json.dumps(source_payload or {}, ensure_ascii=False),
                utcnow(),
            ),
        )

    async def get_document_stage(self, *, document_id: str, stage_no: int):
        return await self.fetchone(
            """SELECT id, case_id, document_id, stage_no, stage_key, status, signer_email,
                      docuseal_submission_id, docuseal_signing_link, source_payload_json,
                      started_at_utc, completed_at_utc, failed_at_utc
               FROM document_stages
               WHERE document_id=? AND stage_no=?""",
            (document_id, int(stage_no)),
        )

    async def get_document_stage_by_submission(self, *, submission_id: str):
        return await self.fetchone(
            """SELECT id, case_id, document_id, stage_no, stage_key, status, signer_email,
                      docuseal_submission_id, docuseal_signing_link, source_payload_json,
                      started_at_utc, completed_at_utc, failed_at_utc
               FROM document_stages
               WHERE docuseal_submission_id=?""",
            (submission_id,),
        )

    async def update_document_stage_submission(
        self,
        *,
        stage_id: int,
        status: str,
        submission_id: str,
        signing_link: str | None,
    ) -> None:
        await self.exec(
            """UPDATE document_stages
               SET status=?, docuseal_submission_id=?, docuseal_signing_link=?
               WHERE id=?""",
            (status, submission_id, signing_link, int(stage_id)),
        )

    async def complete_document_stage(self, *, stage_id: int) -> None:
        await self.exec(
            "UPDATE document_stages SET status='completed', completed_at_utc=? WHERE id=?",
            (utcnow(), int(stage_id)),
        )

    async def fail_document_stage(self, *, stage_id: int, status: str = "failed") -> None:
        await self.exec(
            "UPDATE document_stages SET status=?, failed_at_utc=? WHERE id=?",
            (status, utcnow(), int(stage_id)),
        )

    async def create_document_stage_artifact(
        self,
        *,
        document_stage_id: int,
        artifact_type: str,
        storage_backend: str,
        storage_path: str,
        sha256: str,
        size_bytes: int,
    ) -> int:
        return await self.insert(
            """INSERT INTO document_stage_artifacts(
                 document_stage_id, artifact_type, storage_backend, storage_path, sha256, size_bytes, created_at_utc
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                int(document_stage_id),
                artifact_type,
                storage_backend,
                storage_path,
                sha256,
                int(size_bytes),
                utcnow(),
            ),
        )

    async def replace_document_stage_fields(self, *, document_stage_id: int, fields: dict[str, object]) -> None:
        await self.exec("DELETE FROM document_stage_field_values WHERE document_stage_id=?", (int(document_stage_id),))
        for key, value in fields.items():
            await self.exec(
                """INSERT INTO document_stage_field_values(document_stage_id, field_key, field_value, created_at_utc)
                   VALUES(?,?,?,?)""",
                (int(document_stage_id), str(key), json.dumps(value, ensure_ascii=False), utcnow()),
            )
