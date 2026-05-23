from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import sqlite3

from ..db.db import utcnow


_SETTLEMENT_DOCUMENT_KEYS = {"settlement_form", "settlement_form_3106"}
_COMPLETED_DOCUMENT_STATUSES = {"signed", "approved", "uploaded"}
_SETTLEMENT_TRACKER_AUTOMATION_ACTOR = "DocuSeal automation"


@dataclass(frozen=True)
class SettlementTrackerRepairChange:
    case_id: str
    grievance_id: str
    member_name: str
    document_id: str
    doc_type: str
    template_key: str | None
    document_status: str
    completed_at_utc: str | None
    docuseal_submission_id: str
    previous_officer_status: str | None
    new_officer_status: str
    previous_officer_closed_at_utc: str | None
    new_officer_closed_at_utc: str
    previous_officer_closed_by: str | None
    new_officer_closed_by: str


@dataclass(frozen=True)
class SettlementTrackerRepairResult:
    db_path: str
    dry_run: bool
    affected_case_count: int
    changes: list[SettlementTrackerRepairChange]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def _event_columns(con: sqlite3.Connection) -> set[str]:
    return {str(row["name"]) for row in con.execute("PRAGMA table_info(events)").fetchall()}


def _insert_event(
    con: sqlite3.Connection,
    *,
    cols: set[str],
    case_id: str,
    document_id: str,
    event_type: str,
    details: dict[str, object],
) -> None:
    ts = utcnow()
    details_json = json.dumps(details, ensure_ascii=False)
    if "case_id" in cols and "grievance_id" in cols:
        if "document_id" in cols:
            con.execute(
                """INSERT INTO events(case_id, grievance_id, document_id, ts_utc, event_type, details_json)
                   VALUES(?,?,?,?,?,?)""",
                (case_id, case_id, document_id, ts, event_type, details_json),
            )
        else:
            con.execute(
                """INSERT INTO events(case_id, grievance_id, ts_utc, event_type, details_json)
                   VALUES(?,?,?,?,?)""",
                (case_id, case_id, ts, event_type, details_json),
            )
        return
    if "case_id" in cols:
        if "document_id" in cols:
            con.execute(
                "INSERT INTO events(case_id, document_id, ts_utc, event_type, details_json) VALUES(?,?,?,?,?)",
                (case_id, document_id, ts, event_type, details_json),
            )
        else:
            con.execute(
                "INSERT INTO events(case_id, ts_utc, event_type, details_json) VALUES(?,?,?,?)",
                (case_id, ts, event_type, details_json),
            )
        return
    if "grievance_id" in cols:
        con.execute(
            "INSERT INTO events(grievance_id, ts_utc, event_type, details_json) VALUES(?,?,?,?)",
            (case_id, ts, event_type, details_json),
        )
        return
    raise RuntimeError("events table missing required case/grievance id column")


def _candidate_sort_key(row: sqlite3.Row) -> tuple[str, str]:
    completed_at = str(row["completed_at_utc"] or "").strip()
    created_at = str(row["document_created_at_utc"] or "").strip()
    return (completed_at or created_at, str(row["document_id"] or ""))


def repair_settlement_tracker_closures(
    *,
    db_path: str,
    dry_run: bool = True,
) -> SettlementTrackerRepairResult:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        if not dry_run:
            con.execute("BEGIN IMMEDIATE")

        rows = con.execute(
            """
            SELECT c.id AS case_id,
                   c.grievance_id,
                   c.member_name,
                   c.officer_status,
                   c.officer_closed_at_utc,
                   c.officer_closed_by,
                   d.id AS document_id,
                   d.created_at_utc AS document_created_at_utc,
                   d.doc_type,
                   d.template_key,
                   d.status AS document_status,
                   d.completed_at_utc,
                   d.docuseal_submission_id
            FROM cases c
            JOIN documents d ON d.case_id = c.id
            WHERE d.requires_signature = 1
              AND COALESCE(TRIM(d.docuseal_submission_id), '') <> ''
              AND (
                LOWER(TRIM(COALESCE(d.doc_type, ''))) IN ('settlement_form', 'settlement_form_3106')
                OR LOWER(TRIM(COALESCE(d.template_key, ''))) IN ('settlement_form', 'settlement_form_3106')
              )
              AND (
                COALESCE(TRIM(d.completed_at_utc), '') <> ''
                OR LOWER(TRIM(COALESCE(d.status, ''))) IN ('signed', 'approved', 'uploaded')
              )
              AND COALESCE(LOWER(TRIM(c.officer_status)), '') <> 'closed'
            """
        ).fetchall()

        latest_by_case: dict[str, sqlite3.Row] = {}
        for row in rows:
            if _normalized(row["doc_type"]) not in _SETTLEMENT_DOCUMENT_KEYS and _normalized(
                row["template_key"]
            ) not in _SETTLEMENT_DOCUMENT_KEYS:
                continue
            if _normalized(row["document_status"]) not in _COMPLETED_DOCUMENT_STATUSES and not _clean_optional_text(
                row["completed_at_utc"]
            ):
                continue
            case_id = str(row["case_id"] or "")
            current = latest_by_case.get(case_id)
            if current is None or _candidate_sort_key(row) > _candidate_sort_key(current):
                latest_by_case[case_id] = row

        changes: list[SettlementTrackerRepairChange] = []
        for row in sorted(latest_by_case.values(), key=lambda item: str(item["case_id"] or "")):
            previous_closed_at = _clean_optional_text(row["officer_closed_at_utc"])
            previous_closed_by = _clean_optional_text(row["officer_closed_by"])
            new_closed_at = previous_closed_at or utcnow()
            new_closed_by = previous_closed_by or _SETTLEMENT_TRACKER_AUTOMATION_ACTOR
            changes.append(
                SettlementTrackerRepairChange(
                    case_id=str(row["case_id"] or ""),
                    grievance_id=str(row["grievance_id"] or ""),
                    member_name=str(row["member_name"] or ""),
                    document_id=str(row["document_id"] or ""),
                    doc_type=str(row["doc_type"] or ""),
                    template_key=_clean_optional_text(row["template_key"]),
                    document_status=str(row["document_status"] or ""),
                    completed_at_utc=_clean_optional_text(row["completed_at_utc"]),
                    docuseal_submission_id=str(row["docuseal_submission_id"] or ""),
                    previous_officer_status=_clean_optional_text(row["officer_status"]),
                    new_officer_status="closed",
                    previous_officer_closed_at_utc=previous_closed_at,
                    new_officer_closed_at_utc=new_closed_at,
                    previous_officer_closed_by=previous_closed_by,
                    new_officer_closed_by=new_closed_by,
                )
            )

        if dry_run:
            return SettlementTrackerRepairResult(
                db_path=db_path,
                dry_run=True,
                affected_case_count=len(changes),
                changes=changes,
            )

        event_cols = _event_columns(con)
        for change in changes:
            con.execute(
                """
                UPDATE cases
                SET officer_status='closed',
                    officer_closed_at_utc=CASE
                      WHEN officer_closed_at_utc IS NULL OR TRIM(officer_closed_at_utc)='' THEN ?
                      ELSE officer_closed_at_utc
                    END,
                    officer_closed_by=CASE
                      WHEN officer_closed_by IS NULL OR TRIM(officer_closed_by)='' THEN ?
                      ELSE officer_closed_by
                    END
                WHERE id=?
                  AND COALESCE(LOWER(TRIM(officer_status)), '') <> 'closed'
                """,
                (
                    change.new_officer_closed_at_utc,
                    change.new_officer_closed_by,
                    change.case_id,
                ),
            )
            _insert_event(
                con,
                cols=event_cols,
                case_id=change.case_id,
                document_id=change.document_id,
                event_type="settlement_tracker_auto_closed",
                details={
                    "case_id": change.case_id,
                    "document_id": change.document_id,
                    "docuseal_submission_id": change.docuseal_submission_id,
                    "doc_type": change.doc_type,
                    "template_key": change.template_key or "",
                    "actor": "docuseal",
                    "actor_display_name": _SETTLEMENT_TRACKER_AUTOMATION_ACTOR,
                    "previous_officer_status": change.previous_officer_status,
                    "new_officer_status": change.new_officer_status,
                    "previous_officer_closed_at_utc": change.previous_officer_closed_at_utc,
                    "new_officer_closed_at_utc": change.new_officer_closed_at_utc,
                    "previous_officer_closed_by": change.previous_officer_closed_by,
                    "new_officer_closed_by": change.new_officer_closed_by,
                    "source": "repair_settlement_tracker_closures",
                },
            )

        con.commit()
        return SettlementTrackerRepairResult(
            db_path=db_path,
            dry_run=False,
            affected_case_count=len(changes),
            changes=changes,
        )
    except Exception:
        if not dry_run:
            con.rollback()
        raise
    finally:
        con.close()
