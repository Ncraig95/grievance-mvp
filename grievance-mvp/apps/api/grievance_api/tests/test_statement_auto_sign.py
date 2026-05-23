from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from grievance_api.core.config import DocumentPolicyConfig
from grievance_api.db.db import Db, utcnow
from grievance_api.db.migrate import migrate
from grievance_api.services.statement_auto_sign import (
    maybe_enqueue_statement_auto_sign_job,
    process_due_statement_auto_sign_jobs,
)


def _enabled_cfg(*, delay: int = 60, enabled: bool = True):  # noqa: ANN001
    return SimpleNamespace(
        document_policies={
            "statement_of_occurrence": DocumentPolicyConfig(
                folder_resolution="default",
                default_signer_field="personal_email",
                default_requires_signature=True,
                signature_dispatch_timing="immediate",
                attested_auto_sign_enabled=enabled,
                attested_auto_sign_delay_seconds=delay,
            )
        }
    )


class _FakeDocuSeal:
    def __init__(self, submitters: list[dict[str, object]], *, fail_complete: bool = False) -> None:
        self.submitters = submitters
        self.fail_complete = fail_complete
        self.list_calls: list[str] = []
        self.complete_calls: list[dict[str, object]] = []

    def list_submitters(self, *, submission_id: str) -> list[dict[str, object]]:
        self.list_calls.append(submission_id)
        return [dict(item) for item in self.submitters]

    def auto_complete_submitter(self, *, submitter_id: str | int, fields: list[dict[str, object]]) -> dict[str, object]:
        if self.fail_complete:
            raise RuntimeError("docuseal update failed")
        call = {"submitter_id": submitter_id, "fields": fields}
        self.complete_calls.append(call)
        return {"id": submitter_id, "completed": True}


class StatementAutoSignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        migrate(self.db_path)
        self.db = Db(self.db_path)
        await self._insert_case_and_document(status="sent_for_signature")

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    async def _insert_case_and_document(self, *, status: str) -> None:
        now = utcnow()
        await self.db.exec(
            """
            INSERT INTO cases(
              id, grievance_id, created_at_utc, status, approval_status, member_name,
              intake_request_id, intake_payload_json
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "C1",
                "2026001",
                now,
                "awaiting_signatures",
                "pending",
                "Taylor Jones",
                "REQ1",
                json.dumps({"_signature_attestation": {"accepted": True, "signer_typed_name": "Taylor Jones"}}),
            ),
        )
        await self.db.exec(
            """
            INSERT INTO documents(
              id, case_id, created_at_utc, doc_type, template_key, status, requires_signature,
              signer_order_json, docuseal_submission_id, docuseal_signing_link
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "D1",
                "C1",
                now,
                "statement_of_occurrence",
                "statement_of_occurrence",
                status,
                1,
                json.dumps(["taylor@example.org"]),
                "sub-1",
                "https://docuseal.local/s/sign",
            ),
        )

    async def _insert_due_job(self, *, signer_name: str = "Taylor Jones") -> None:
        now = utcnow()
        await self.db.exec(
            """
            INSERT INTO statement_auto_sign_jobs(
              id, case_id, document_id, docuseal_submission_id, signer_email, signer_name,
              run_after_utc, status, attempts, created_at_utc, updated_at_utc
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("J1", "C1", "D1", "sub-1", "taylor@example.org", signer_name, now, "pending", 0, now, now),
        )

    async def test_enqueue_requires_statement_one_signer_enabled_config_and_attestation(self) -> None:
        accepted = {"accepted": True, "signer_typed_name": "Taylor Jones"}

        self.assertFalse(
            await maybe_enqueue_statement_auto_sign_job(
                cfg=_enabled_cfg(enabled=False),
                db=self.db,
                case_id="C1",
                document_id="D1",
                doc_type="statement_of_occurrence",
                template_key="statement_of_occurrence",
                submission_id="sub-1",
                signer_order=["taylor@example.org"],
                attestation=accepted,
            )
        )
        self.assertFalse(
            await maybe_enqueue_statement_auto_sign_job(
                cfg=_enabled_cfg(),
                db=self.db,
                case_id="C1",
                document_id="D1",
                doc_type="settlement_form_3106",
                template_key="settlement_form_3106",
                submission_id="sub-1",
                signer_order=["taylor@example.org"],
                attestation=accepted,
            )
        )
        self.assertFalse(
            await maybe_enqueue_statement_auto_sign_job(
                cfg=_enabled_cfg(),
                db=self.db,
                case_id="C1",
                document_id="D1",
                doc_type="statement_of_occurrence",
                template_key="statement_of_occurrence",
                submission_id="sub-1",
                signer_order=["one@example.org", "two@example.org"],
                attestation=accepted,
            )
        )
        self.assertFalse(
            await maybe_enqueue_statement_auto_sign_job(
                cfg=_enabled_cfg(),
                db=self.db,
                case_id="C1",
                document_id="D1",
                doc_type="statement_of_occurrence",
                template_key="statement_of_occurrence",
                submission_id="sub-1",
                signer_order=["taylor@example.org"],
                attestation={"accepted": False},
            )
        )

        self.assertTrue(
            await maybe_enqueue_statement_auto_sign_job(
                cfg=_enabled_cfg(delay=30),
                db=self.db,
                case_id="C1",
                document_id="D1",
                doc_type="statement_of_occurrence",
                template_key="statement_of_occurrence",
                submission_id="sub-1",
                signer_order=["taylor@example.org"],
                attestation=accepted,
            )
        )
        row = await self.db.fetchone(
            "SELECT status, signer_email, signer_name FROM statement_auto_sign_jobs WHERE document_id=?",
            ("D1",),
        )
        self.assertEqual(row, ("pending", "taylor@example.org", "Taylor Jones"))

    async def test_due_job_skips_when_document_already_completed(self) -> None:
        await self.db.exec("UPDATE documents SET status='completed', completed_at_utc=? WHERE id='D1'", (utcnow(),))
        await self._insert_due_job()

        processed = await process_due_statement_auto_sign_jobs(
            cfg=_enabled_cfg(),
            db=self.db,
            docuseal=_FakeDocuSeal([]),
            logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
        )

        self.assertEqual(processed, 1)
        row = await self.db.fetchone("SELECT status FROM statement_auto_sign_jobs WHERE id='J1'")
        self.assertEqual(row[0], "skipped")
        event = await self.db.fetchone("SELECT event_type FROM events WHERE event_type='statement_auto_sign_skipped'")
        self.assertIsNotNone(event)

    async def test_due_job_completes_incomplete_submitter_with_typed_signature_fields(self) -> None:
        await self._insert_due_job()
        docuseal = _FakeDocuSeal([{"id": 11, "email": "taylor@example.org", "status": "opened"}])

        processed = await process_due_statement_auto_sign_jobs(
            cfg=_enabled_cfg(),
            db=self.db,
            docuseal=docuseal,
            logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
        )

        self.assertEqual(processed, 1)
        self.assertEqual(docuseal.list_calls, ["sub-1"])
        self.assertEqual(docuseal.complete_calls[0]["submitter_id"], 11)
        fields = {item["name"]: item for item in docuseal.complete_calls[0]["fields"]}
        self.assertEqual(fields["signer1_signature"]["default_value"], "Taylor Jones")
        self.assertTrue(fields["signer1_signature"]["readonly"])
        self.assertIn("signer1_date", fields)
        row = await self.db.fetchone("SELECT status FROM statement_auto_sign_jobs WHERE id='J1'")
        self.assertEqual(row[0], "completed")

    async def test_due_job_failure_preserves_docuseal_signing_link(self) -> None:
        await self._insert_due_job()
        docuseal = _FakeDocuSeal(
            [{"id": 11, "email": "taylor@example.org", "status": "opened"}],
            fail_complete=True,
        )

        processed = await process_due_statement_auto_sign_jobs(
            cfg=_enabled_cfg(),
            db=self.db,
            docuseal=docuseal,
            logger=SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None),
        )

        self.assertEqual(processed, 1)
        job = await self.db.fetchone("SELECT status, last_error FROM statement_auto_sign_jobs WHERE id='J1'")
        self.assertEqual(job[0], "failed")
        self.assertIn("docuseal update failed", job[1])
        doc = await self.db.fetchone("SELECT docuseal_signing_link FROM documents WHERE id='D1'")
        self.assertEqual(doc[0], "https://docuseal.local/s/sign")
        event = await self.db.fetchone("SELECT event_type FROM events WHERE event_type='statement_auto_sign_failed'")
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
