from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..db.db import Db, utcnow


_STATEMENT_FORM_KEY = "statement_of_occurrence"
_MIN_DELAY_SECONDS = 30
_DEFAULT_DELAY_SECONDS = 60
_WORKER_POLL_SECONDS = 5
_RUNNING_STALE_SECONDS = 5 * 60


@dataclass(frozen=True)
class StatementAutoSignJob:
    id: str
    case_id: str
    document_id: str
    docuseal_submission_id: str
    signer_email: str
    signer_name: str
    run_after_utc: str


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def _status_token(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _submitter_is_complete(submitter: dict[str, object]) -> bool:
    for key in ("completed_at", "completedAt", "completed_at_utc", "completedAtUtc"):
        if submitter.get(key):
            return True
    return _status_token(submitter.get("status")) in {"completed", "finished", "done"}


def _submitter_email(submitter: dict[str, object]) -> str:
    for key in ("email", "email_address", "emailAddress", "signer_email"):
        value = submitter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    email_address = submitter.get("emailAddress")
    if isinstance(email_address, dict):
        nested = email_address.get("address")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _select_submitter(*, submitters: list[dict[str, object]], signer_email: str) -> dict[str, object] | None:
    normalized = _normalize_email(signer_email)
    if normalized:
        matches = [item for item in submitters if _normalize_email(_submitter_email(item)) == normalized]
        if matches:
            return matches[0]
    if len(submitters) == 1:
        return submitters[0]
    return None


def _policy_for_statement(cfg):  # noqa: ANN001
    policies = getattr(cfg, "document_policies", {}) or {}
    if not isinstance(policies, dict):
        return None
    return policies.get(_STATEMENT_FORM_KEY)


def statement_auto_sign_enabled(cfg) -> bool:  # noqa: ANN001
    policy = _policy_for_statement(cfg)
    return bool(getattr(policy, "attested_auto_sign_enabled", False))


def statement_auto_sign_delay_seconds(cfg) -> int:  # noqa: ANN001
    policy = _policy_for_statement(cfg)
    raw_delay = getattr(policy, "attested_auto_sign_delay_seconds", _DEFAULT_DELAY_SECONDS)
    try:
        delay = int(raw_delay)
    except Exception:
        delay = _DEFAULT_DELAY_SECONDS
    return max(_MIN_DELAY_SECONDS, delay)


def _is_statement_document(*, doc_type: str, template_key: str | None) -> bool:
    return any(str(value or "").strip().lower() == _STATEMENT_FORM_KEY for value in (doc_type, template_key))


def _attestation_accepted(attestation: dict[str, object] | None) -> bool:
    if not isinstance(attestation, dict):
        return False
    accepted = attestation.get("accepted")
    if accepted is True:
        return True
    return str(accepted or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _job_from_row(row) -> StatementAutoSignJob:  # noqa: ANN001
    return StatementAutoSignJob(
        id=str(row[0]),
        case_id=str(row[1]),
        document_id=str(row[2]),
        docuseal_submission_id=str(row[3]),
        signer_email=str(row[4]),
        signer_name=str(row[5]),
        run_after_utc=str(row[6]),
    )


def _signature_fields(*, signer_name: str) -> list[dict[str, object]]:
    today = _now_dt().date().isoformat()
    return [
        {"name": "signer1_signature", "default_value": signer_name, "readonly": True},
        {"name": "signer1_date", "default_value": today, "readonly": True},
    ]


async def maybe_enqueue_statement_auto_sign_job(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    case_id: str,
    document_id: str,
    doc_type: str,
    template_key: str | None,
    submission_id: str | None,
    signer_order: list[str],
    attestation: dict[str, object] | None,
) -> bool:
    if not statement_auto_sign_enabled(cfg):
        return False
    if not _is_statement_document(doc_type=doc_type, template_key=template_key):
        return False
    if len(signer_order) != 1:
        return False
    if not submission_id:
        return False
    if not _attestation_accepted(attestation):
        return False

    signer_email = _clean_text(signer_order[0])
    if not signer_email:
        return False
    signer_name = _clean_text(attestation.get("signer_typed_name") if attestation else "")
    if not signer_name:
        signer_name = signer_email

    delay_seconds = statement_auto_sign_delay_seconds(cfg)
    now = _now_dt()
    run_after = (now + timedelta(seconds=delay_seconds)).isoformat()
    job_id = uuid4().hex
    await db.exec(
        """
        INSERT INTO statement_auto_sign_jobs(
          id, case_id, document_id, docuseal_submission_id, signer_email, signer_name,
          run_after_utc, status, attempts, created_at_utc, updated_at_utc
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(document_id) DO UPDATE SET
          docuseal_submission_id=excluded.docuseal_submission_id,
          signer_email=excluded.signer_email,
          signer_name=excluded.signer_name,
          run_after_utc=excluded.run_after_utc,
          status='pending',
          locked_at_utc=NULL,
          locked_by=NULL,
          last_error=NULL,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            job_id,
            case_id,
            document_id,
            submission_id,
            signer_email,
            signer_name,
            run_after,
            "pending",
            0,
            now.isoformat(),
            now.isoformat(),
        ),
    )
    await db.add_event(
        case_id,
        document_id,
        "statement_auto_sign_queued",
        {
            "docuseal_submission_id": submission_id,
            "signer_email": signer_email,
            "run_after_utc": run_after,
            "delay_seconds": delay_seconds,
        },
    )
    return True


async def _mark_job(
    *,
    db: Db,
    job: StatementAutoSignJob,
    status: str,
    event_type: str,
    details: dict[str, object],
    error: str = "",
) -> None:
    now = utcnow()
    completed_at = now if status in {"completed", "skipped"} else None
    failed_at = now if status == "failed" else None
    await db.exec(
        """
        UPDATE statement_auto_sign_jobs
        SET status=?, completed_at_utc=COALESCE(?, completed_at_utc),
            failed_at_utc=COALESCE(?, failed_at_utc), last_error=?,
            locked_at_utc=NULL, locked_by=NULL, updated_at_utc=?
        WHERE id=?
        """,
        (status, completed_at, failed_at, error, now, job.id),
    )
    await db.add_event(job.case_id, job.document_id, event_type, details)


async def _reset_stale_running_jobs(*, db: Db) -> None:
    cutoff = (_now_dt() - timedelta(seconds=_RUNNING_STALE_SECONDS)).isoformat()
    await db.exec(
        """
        UPDATE statement_auto_sign_jobs
        SET status='pending', locked_at_utc=NULL, locked_by=NULL, updated_at_utc=?
        WHERE status='running' AND COALESCE(locked_at_utc, '') <= ?
        """,
        (utcnow(), cutoff),
    )


async def _claim_job(*, db: Db, job: StatementAutoSignJob, worker_id: str) -> bool:
    now = utcnow()
    await db.exec(
        """
        UPDATE statement_auto_sign_jobs
        SET status='running', attempts=attempts + 1, locked_at_utc=?, locked_by=?, updated_at_utc=?
        WHERE id=? AND status='pending'
        """,
        (now, worker_id, now, job.id),
    )
    row = await db.fetchone("SELECT status, locked_by FROM statement_auto_sign_jobs WHERE id=?", (job.id,))
    return bool(row and str(row[0]) == "running" and str(row[1]) == worker_id)


async def _process_job(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    docuseal,  # noqa: ANN001
    logger: logging.Logger,
    job: StatementAutoSignJob,
) -> str:
    row = await db.fetchone(
        """
        SELECT d.status, d.completed_at_utc, d.docuseal_submission_id, c.intake_payload_json
        FROM documents d
        JOIN cases c ON c.id = d.case_id
        WHERE d.id=? AND d.case_id=?
        """,
        (job.document_id, job.case_id),
    )
    if not row:
        await _mark_job(
            db=db,
            job=job,
            status="failed",
            event_type="statement_auto_sign_failed",
            details={"reason": "document_not_found", "docuseal_submission_id": job.docuseal_submission_id},
            error="document_not_found",
        )
        return "failed"

    document_status = _status_token(row[0])
    completed_at = _clean_text(row[1])
    stored_submission_id = _clean_text(row[2])
    if completed_at or document_status == "completed":
        await _mark_job(
            db=db,
            job=job,
            status="skipped",
            event_type="statement_auto_sign_skipped",
            details={"reason": "document_already_completed", "document_status": document_status},
        )
        return "skipped"
    if stored_submission_id != job.docuseal_submission_id:
        await _mark_job(
            db=db,
            job=job,
            status="failed",
            event_type="statement_auto_sign_failed",
            details={
                "reason": "submission_mismatch",
                "expected_submission_id": job.docuseal_submission_id,
                "stored_submission_id": stored_submission_id,
            },
            error="submission_mismatch",
        )
        return "failed"

    submitters = await asyncio.to_thread(docuseal.list_submitters, submission_id=job.docuseal_submission_id)
    submitter = _select_submitter(submitters=submitters, signer_email=job.signer_email)
    if submitter and _submitter_is_complete(submitter):
        await _mark_job(
            db=db,
            job=job,
            status="skipped",
            event_type="statement_auto_sign_skipped",
            details={
                "reason": "submitter_already_completed",
                "docuseal_submission_id": job.docuseal_submission_id,
                "signer_email": job.signer_email,
            },
        )
        return "skipped"
    if not submitter:
        await _mark_job(
            db=db,
            job=job,
            status="failed",
            event_type="statement_auto_sign_failed",
            details={
                "reason": "submitter_not_found",
                "docuseal_submission_id": job.docuseal_submission_id,
                "signer_email": job.signer_email,
            },
            error="submitter_not_found",
        )
        return "failed"

    submitter_id = submitter.get("id") or submitter.get("submitter_id") or submitter.get("submitterId")
    if not submitter_id:
        await _mark_job(
            db=db,
            job=job,
            status="failed",
            event_type="statement_auto_sign_failed",
            details={"reason": "submitter_id_missing", "docuseal_submission_id": job.docuseal_submission_id},
            error="submitter_id_missing",
        )
        return "failed"

    signer_name = job.signer_name.strip() or job.signer_email
    response = await asyncio.to_thread(
        docuseal.auto_complete_submitter,
        submitter_id=submitter_id,
        fields=_signature_fields(signer_name=signer_name),
    )
    logger.info(
        "statement_auto_sign_completed",
        extra={"correlation_id": job.case_id, "document_id": job.document_id},
    )
    await _mark_job(
        db=db,
        job=job,
        status="completed",
        event_type="statement_auto_sign_completed",
        details={
            "docuseal_submission_id": job.docuseal_submission_id,
            "submitter_id": str(submitter_id),
            "signer_email": job.signer_email,
            "signer_name": signer_name,
            "docuseal_response": response,
        },
    )
    return "completed"


async def process_due_statement_auto_sign_jobs(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    docuseal,  # noqa: ANN001
    logger: logging.Logger,
    limit: int = 10,
) -> int:
    if not statement_auto_sign_enabled(cfg):
        return 0
    await _reset_stale_running_jobs(db=db)
    now = utcnow()
    rows = await db.fetchall(
        """
        SELECT id, case_id, document_id, docuseal_submission_id, signer_email, signer_name, run_after_utc
        FROM statement_auto_sign_jobs
        WHERE status='pending' AND run_after_utc <= ?
        ORDER BY run_after_utc, created_at_utc
        LIMIT ?
        """,
        (now, max(1, int(limit))),
    )
    worker_id = uuid4().hex
    processed = 0
    for row in rows:
        job = _job_from_row(row)
        if not await _claim_job(db=db, job=job, worker_id=worker_id):
            continue
        try:
            await _process_job(cfg=cfg, db=db, docuseal=docuseal, logger=logger, job=job)
            processed += 1
        except Exception as exc:
            logger.exception(
                "statement_auto_sign_failed",
                extra={"correlation_id": job.case_id, "document_id": job.document_id},
            )
            await _mark_job(
                db=db,
                job=job,
                status="failed",
                event_type="statement_auto_sign_failed",
                details={
                    "docuseal_submission_id": job.docuseal_submission_id,
                    "signer_email": job.signer_email,
                    "error": str(exc),
                },
                error=str(exc),
            )
            processed += 1
    return processed


async def run_statement_auto_sign_worker(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    docuseal,  # noqa: ANN001
    logger: logging.Logger,
    poll_seconds: int = _WORKER_POLL_SECONDS,
) -> None:
    while True:
        try:
            await process_due_statement_auto_sign_jobs(
                cfg=cfg,
                db=db,
                docuseal=docuseal,
                logger=logger,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("statement_auto_sign_worker_error")
        await asyncio.sleep(max(1, int(poll_seconds)))
