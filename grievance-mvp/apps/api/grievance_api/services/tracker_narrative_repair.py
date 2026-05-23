from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import sqlite3

from .grievance_summary import build_grievance_summary, low_priority_grievance_text_candidates


_REPAIR_SOURCE = "repair_tracker_narratives"


@dataclass(frozen=True)
class TrackerNarrativeRepairChange:
    case_id: str
    grievance_id: str
    member_name: str
    previous_tracking_issue_summary: str
    new_tracking_issue_summary: str
    previous_source: str
    new_source: str
    new_full_text: str


@dataclass(frozen=True)
class TrackerNarrativeRepairUnresolved:
    case_id: str
    grievance_id: str
    member_name: str
    tracking_issue_summary: str
    matched_low_priority_source: str
    reason: str


@dataclass(frozen=True)
class TrackerNarrativeRepairResult:
    db_path: str
    dry_run: bool
    affected_case_count: int
    unresolved_case_count: int
    changes: list[TrackerNarrativeRepairChange]
    unresolved: list[TrackerNarrativeRepairUnresolved]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_payload(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _matches_exact_tracker_text(left: object, right: object) -> bool:
    return _clean_text(left) == _clean_text(right)


def _low_priority_match(
    *,
    candidates: list[tuple[str, str]],
    tracking_issue_summary: str,
) -> tuple[str, str] | None:
    for source, text in candidates:
        if _matches_exact_tracker_text(tracking_issue_summary, text):
            return source, text
    return None


def _insert_repair_event(
    con: sqlite3.Connection,
    *,
    change: TrackerNarrativeRepairChange,
) -> None:
    con.execute(
        """
        INSERT INTO events(case_id, document_id, ts_utc, event_type, details_json)
        VALUES(?,?,?,?,?)
        """,
        (
            change.case_id,
            None,
            _utcnow(),
            "tracker_narrative_repaired",
            json.dumps(
                {
                    "source": _REPAIR_SOURCE,
                    "case_id": change.case_id,
                    "previous_tracking_issue_summary": change.previous_tracking_issue_summary,
                    "new_tracking_issue_summary": change.new_tracking_issue_summary,
                    "previous_source": change.previous_source,
                    "new_source": change.new_source,
                },
                ensure_ascii=False,
            ),
        ),
    )


def repair_tracker_narratives(
    *,
    db_path: str,
    dry_run: bool = True,
) -> TrackerNarrativeRepairResult:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        if not dry_run:
            con.execute("BEGIN IMMEDIATE")

        rows = con.execute(
            """
            SELECT id AS case_id,
                   grievance_id,
                   member_name,
                   intake_payload_json,
                   tracking_issue_summary
            FROM cases
            WHERE COALESCE(TRIM(tracking_issue_summary), '') <> ''
            ORDER BY created_at_utc DESC, id DESC
            """
        ).fetchall()

        changes: list[TrackerNarrativeRepairChange] = []
        unresolved: list[TrackerNarrativeRepairUnresolved] = []

        for row in rows:
            tracking_issue_summary = _clean_text(row["tracking_issue_summary"])
            payload = _parse_payload(row["intake_payload_json"])
            low_priority_candidates = low_priority_grievance_text_candidates(payload)
            low_match = _low_priority_match(
                candidates=low_priority_candidates,
                tracking_issue_summary=tracking_issue_summary,
            )
            if low_match is None:
                continue

            previous_source, _previous_text = low_match
            low_priority_sources = {source for source, _text in low_priority_candidates}
            summary = build_grievance_summary(payload, manual_text=None)
            new_summary = _clean_text(summary.summary)
            new_full_text = _clean_text(summary.full_text)
            if (
                not new_summary
                or not new_full_text
                or not summary.source
                or summary.source in low_priority_sources
                or _matches_exact_tracker_text(new_summary, tracking_issue_summary)
            ):
                unresolved.append(
                    TrackerNarrativeRepairUnresolved(
                        case_id=str(row["case_id"] or ""),
                        grievance_id=str(row["grievance_id"] or ""),
                        member_name=str(row["member_name"] or ""),
                        tracking_issue_summary=tracking_issue_summary,
                        matched_low_priority_source=previous_source,
                        reason="no recoverable higher-priority narrative source",
                    )
                )
                continue

            changes.append(
                TrackerNarrativeRepairChange(
                    case_id=str(row["case_id"] or ""),
                    grievance_id=str(row["grievance_id"] or ""),
                    member_name=str(row["member_name"] or ""),
                    previous_tracking_issue_summary=tracking_issue_summary,
                    new_tracking_issue_summary=new_summary,
                    previous_source=previous_source,
                    new_source=summary.source,
                    new_full_text=new_full_text,
                )
            )

        if dry_run:
            return TrackerNarrativeRepairResult(
                db_path=db_path,
                dry_run=True,
                affected_case_count=len(changes),
                unresolved_case_count=len(unresolved),
                changes=changes,
                unresolved=unresolved,
            )

        for change in changes:
            con.execute(
                """
                UPDATE cases
                SET tracking_issue_summary=?
                WHERE id=?
                  AND TRIM(COALESCE(tracking_issue_summary, ''))=?
                """,
                (
                    change.new_tracking_issue_summary,
                    change.case_id,
                    change.previous_tracking_issue_summary,
                ),
            )
            _insert_repair_event(con, change=change)

        con.commit()
        return TrackerNarrativeRepairResult(
            db_path=db_path,
            dry_run=False,
            affected_case_count=len(changes),
            unresolved_case_count=len(unresolved),
            changes=changes,
            unresolved=unresolved,
        )
    except Exception:
        if not dry_run:
            con.rollback()
        raise
    finally:
        con.close()
