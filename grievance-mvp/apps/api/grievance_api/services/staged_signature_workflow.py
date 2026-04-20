from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..db.db import Db
from .signature_workflow import SignatureDispatchOutcome, send_document_for_signature


_DEFAULT_STAGE_KEYS = {
    1: "stage1_union",
    2: "stage2_manager",
    3: "stage3_union_final",
}
_THREE_G_THREE_A_FORM_KEYS = {
    "bst_grievance_form_3g3a",
    "bst_grievance_form_3g3a_extension",
}
_STAGED_FORM_STAGE_KEYS = {
    "bst_grievance_form_3g3a": _DEFAULT_STAGE_KEYS,
    "bst_grievance_form_3g3a_extension": _DEFAULT_STAGE_KEYS,
    "mobility_record_of_grievance": {
        1: "stage1_union",
        2: "stage2_company",
        3: "stage3_union_appeal",
    },
}


def resolve_staged_form_key(*, cfg, doc_type: str, template_key: str | None) -> str | None:  # noqa: ANN001
    normalized_doc_type = (doc_type or "").strip().lower()
    normalized_template_key = (template_key or "").strip().lower()

    candidates: list[str] = []
    for key in (normalized_doc_type, normalized_template_key):
        if key and key not in candidates:
            candidates.append(key)

    for key in candidates:
        if key not in _STAGED_FORM_STAGE_KEYS:
            continue
        policy = cfg.document_policies.get(key)
        if policy and policy.staged_flow_enabled:
            return key
    return None


@dataclass(frozen=True)
class StageSendOutcome:
    stage_no: int
    stage_key: str
    stage_id: int
    status: str
    signing_link: str | None
    submission_id: str | None


def is_staged_document(*, cfg, doc_type: str, template_key: str | None):  # noqa: ANN001
    return resolve_staged_form_key(cfg=cfg, doc_type=doc_type, template_key=template_key) is not None


def is_3g3a_staged(*, cfg, doc_type: str, template_key: str | None):  # noqa: ANN001
    return resolve_staged_form_key(cfg=cfg, doc_type=doc_type, template_key=template_key) in _THREE_G_THREE_A_FORM_KEYS


def stage_key_for(stage_no: int, *, form_key: str | None = None) -> str:
    normalized_form_key = str(form_key or "").strip().lower()
    if normalized_form_key in _STAGED_FORM_STAGE_KEYS:
        stage_keys = _STAGED_FORM_STAGE_KEYS[normalized_form_key]
    else:
        stage_keys = _DEFAULT_STAGE_KEYS
    return stage_keys.get(int(stage_no), f"stage{int(stage_no)}")


def stage_count_for(*, form_key: str | None) -> int:
    normalized_form_key = str(form_key or "").strip().lower()
    if normalized_form_key in _STAGED_FORM_STAGE_KEYS:
        return len(_STAGED_FORM_STAGE_KEYS[normalized_form_key])
    return len(_DEFAULT_STAGE_KEYS)


def normalize_staged_signers(signers: list[str] | None, *, form_key: str | None) -> list[str]:
    normalized = [str(email).strip() for email in (signers or []) if str(email).strip()]
    required_signer_count = stage_count_for(form_key=form_key)
    if len(normalized) < required_signer_count:
        return []
    return normalized[:required_signer_count]


def normalize_3g3a_signers(signers: list[str] | None) -> list[str]:
    return normalize_staged_signers(signers, form_key="bst_grievance_form_3g3a")


async def create_or_send_stage(
    *,
    cfg,  # noqa: ANN001
    db: Db,
    logger,  # noqa: ANN001
    docuseal,  # noqa: ANN001
    notifications,  # noqa: ANN001
    case_id: str,
    grievance_id: str,
    document_id: str,
    doc_type: str,
    template_key: str | None,
    pdf_bytes: bytes,
    alignment_pdf_bytes: bytes | None,
    signer_email: str,
    full_signer_chain: list[str] | None = None,
    stage_no: int,
    correlation_id: str,
    idempotency_prefix: str,
) -> StageSendOutcome:
    stage_no = int(stage_no)
    form_key = resolve_staged_form_key(cfg=cfg, doc_type=doc_type, template_key=template_key)
    key = stage_key_for(stage_no, form_key=form_key)
    existing = await db.get_document_stage(document_id=document_id, stage_no=stage_no)
    if existing:
        stage_id = int(existing[0])
        status = str(existing[5] or "")
        submission_id = str(existing[7] or "") or None
        signing_link = str(existing[8] or "") or None
        return StageSendOutcome(
            stage_no=stage_no,
            stage_key=key,
            stage_id=stage_id,
            status=status,
            signing_link=signing_link,
            submission_id=submission_id,
        )

    stage_id = await db.create_document_stage(
        case_id=case_id,
        document_id=document_id,
        stage_no=stage_no,
        stage_key=key,
        status="preparing",
        signer_email=signer_email,
        source_payload={},
    )
    await db.add_event(
        case_id,
        document_id,
        "document_stage_created",
        {"stage_no": stage_no, "stage_key": key, "stage_id": stage_id},
    )
    try:
        outcome: SignatureDispatchOutcome = await send_document_for_signature(
            cfg=cfg,
            db=db,
            logger=logger,
            docuseal=docuseal,
            notifications=notifications,
            case_id=case_id,
            grievance_id=grievance_id,
            document_id=document_id,
            doc_type=doc_type,
            template_key=template_key,
            pdf_bytes=pdf_bytes,
            alignment_pdf_bytes=alignment_pdf_bytes,
            signer_order=[signer_email],
            correlation_id=correlation_id,
            idempotency_prefix=idempotency_prefix,
        )
    except Exception:
        await db.fail_document_stage(stage_id=stage_id, status="failed")
        raise

    if outcome.status == "sent_for_signature":
        row = await db.fetchone("SELECT docuseal_submission_id FROM documents WHERE id=?", (document_id,))
        submission_id = str((row[0] if row and row[0] is not None else "")).strip() or None
        if full_signer_chain:
            await db.exec(
                "UPDATE documents SET signer_order_json=? WHERE id=?",
                (json.dumps(full_signer_chain, ensure_ascii=False), document_id),
            )
        await db.update_document_stage_submission(
            stage_id=stage_id,
            status=f"sent_for_signature_stage{stage_no}",
            submission_id=submission_id or "",
            signing_link=outcome.signing_link,
        )
        await db.exec(
            "UPDATE documents SET status=?, docuseal_signing_link=? WHERE id=?",
            (f"sent_for_signature_stage{stage_no}", outcome.signing_link, document_id),
        )
        await db.add_event(
            case_id,
            document_id,
            "document_stage_sent_for_signature",
            {
                "stage_no": stage_no,
                "stage_key": key,
                "stage_id": stage_id,
                "submission_id": submission_id,
            },
        )
        return StageSendOutcome(
            stage_no=stage_no,
            stage_key=key,
            stage_id=stage_id,
            status=f"sent_for_signature_stage{stage_no}",
            signing_link=outcome.signing_link,
            submission_id=submission_id,
        )

    await db.fail_document_stage(stage_id=stage_id, status=outcome.status)
    return StageSendOutcome(
        stage_no=stage_no,
        stage_key=key,
        stage_id=stage_id,
        status=outcome.status,
        signing_link=outcome.signing_link,
        submission_id=None,
    )


async def record_stage_artifact(
    *,
    db: Db,
    stage_id: int,
    artifact_type: str,
    storage_backend: str,
    storage_path: str,
    content_bytes: bytes,
) -> None:
    await db.create_document_stage_artifact(
        document_stage_id=stage_id,
        artifact_type=artifact_type,
        storage_backend=storage_backend,
        storage_path=storage_path,
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        size_bytes=len(content_bytes),
    )


def stage_file_path(*, case_dir: Path, document_id: str, stage_no: int, filename: str) -> Path:
    return case_dir / document_id / "Stages" / f"Stage{int(stage_no)}" / filename


def stage_alignment_pdf_path(*, case_dir: Path, document_id: str, stage_no: int) -> Path:
    return case_dir / document_id / "stage_alignments" / f"stage{int(stage_no)}_alignment.pdf"
