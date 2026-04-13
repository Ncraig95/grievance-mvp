from __future__ import annotations

from dataclasses import asdict, dataclass
import sqlite3
import re

from ..db.db import utcnow
from .grievance_id_allocator import format_grievance_id


_NUMERIC_GRIEVANCE_ID_RE = re.compile(r"^(?P<year>\d{4})(?P<seq>\d{3,})$")


@dataclass(frozen=True)
class GrievanceNumberRepairChange:
    case_id: str
    member_name: str
    old_grievance_id: str
    new_grievance_id: str
    old_grievance_number: str | None
    new_grievance_number: str | None
    old_sharepoint_case_folder: str | None
    new_sharepoint_case_folder: str | None
    old_sharepoint_case_web_url: str | None
    new_sharepoint_case_web_url: str | None


@dataclass(frozen=True)
class GrievanceNumberRepairResult:
    db_path: str
    cutoff_id: str
    expected_member_name: str | None
    affected_case_count: int
    sequence_year: int
    sequence_before: int
    sequence_after: int
    dry_run: bool
    changes: list[GrievanceNumberRepairChange]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_numeric_grievance_id(value: str | None) -> tuple[int, int] | None:
    text = str(value or "").strip()
    match = _NUMERIC_GRIEVANCE_ID_RE.fullmatch(text)
    if not match:
        return None
    return int(match.group("year")), int(match.group("seq"))


def _replace_text(value: str | None, *, old: str, new: str) -> str | None:
    if value is None:
        return None
    return value.replace(old, new) if old in value else value


def repair_grievance_numbers(
    *,
    db_path: str,
    cutoff_id: str = "2026026",
    expected_member_name: str | None = "Dean Anderson",
    dry_run: bool = False,
) -> GrievanceNumberRepairResult:
    parsed_cutoff = parse_numeric_grievance_id(cutoff_id)
    if parsed_cutoff is None:
        raise ValueError(f"cutoff_id must be numeric YYYYNNN form: {cutoff_id}")
    year, cutoff_seq = parsed_cutoff
    min_width = max(3, len(cutoff_id) - 4)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT last_seq FROM grievance_id_sequences WHERE year=?",
            (year,),
        ).fetchone()
        sequence_before = int(row["last_seq"]) if row is not None else 0

        case_rows = con.execute(
            """
            SELECT id, grievance_id, grievance_number, member_name,
                   sharepoint_case_folder, sharepoint_case_web_url
            FROM cases
            """
        ).fetchall()

        cutoff_matches: list[sqlite3.Row] = []
        candidates: list[tuple[int, sqlite3.Row]] = []
        for case_row in case_rows:
            grievance_id = str(case_row["grievance_id"] or "").strip()
            if grievance_id == cutoff_id:
                cutoff_matches.append(case_row)
            parsed_case_id = parse_numeric_grievance_id(grievance_id)
            if parsed_case_id is None:
                continue
            case_year, case_seq = parsed_case_id
            if case_year != year or case_seq < cutoff_seq:
                continue
            candidates.append((case_seq, case_row))

        if not cutoff_matches:
            raise RuntimeError(f"cutoff grievance_id not found: {cutoff_id}")

        if expected_member_name:
            wanted = expected_member_name.strip().lower()
            cutoff_matches = [
                case_row
                for case_row in cutoff_matches
                if wanted in str(case_row["member_name"] or "").strip().lower()
            ]
            if not cutoff_matches:
                raise RuntimeError(
                    f"cutoff grievance_id '{cutoff_id}' was found, but not for expected member '{expected_member_name}'"
                )

        if len(cutoff_matches) != 1:
            raise RuntimeError(f"cutoff grievance_id '{cutoff_id}' matched {len(cutoff_matches)} rows")

        candidates.sort(key=lambda item: (item[0], str(item[1]["id"])), reverse=True)
        changes: list[GrievanceNumberRepairChange] = []
        max_new_seq = sequence_before

        for case_seq, case_row in candidates:
            case_id = str(case_row["id"] or "")
            old_grievance_id = str(case_row["grievance_id"] or "").strip()
            new_grievance_id = format_grievance_id(year=year, sequence=case_seq + 1, min_width=min_width)
            old_grievance_number = str(case_row["grievance_number"]).strip() if case_row["grievance_number"] else None
            new_grievance_number = new_grievance_id if old_grievance_number == old_grievance_id else old_grievance_number
            old_sharepoint_case_folder = (
                str(case_row["sharepoint_case_folder"]).strip() if case_row["sharepoint_case_folder"] else None
            )
            new_sharepoint_case_folder = _replace_text(
                old_sharepoint_case_folder,
                old=old_grievance_id,
                new=new_grievance_id,
            )
            old_sharepoint_case_web_url = (
                str(case_row["sharepoint_case_web_url"]).strip() if case_row["sharepoint_case_web_url"] else None
            )
            new_sharepoint_case_web_url = _replace_text(
                old_sharepoint_case_web_url,
                old=old_grievance_id,
                new=new_grievance_id,
            )

            changes.append(
                GrievanceNumberRepairChange(
                    case_id=case_id,
                    member_name=str(case_row["member_name"] or "").strip(),
                    old_grievance_id=old_grievance_id,
                    new_grievance_id=new_grievance_id,
                    old_grievance_number=old_grievance_number,
                    new_grievance_number=new_grievance_number,
                    old_sharepoint_case_folder=old_sharepoint_case_folder,
                    new_sharepoint_case_folder=new_sharepoint_case_folder,
                    old_sharepoint_case_web_url=old_sharepoint_case_web_url,
                    new_sharepoint_case_web_url=new_sharepoint_case_web_url,
                )
            )
            max_new_seq = max(max_new_seq, case_seq + 1)

            if dry_run:
                continue

            con.execute(
                """
                UPDATE cases
                SET grievance_id=?,
                    grievance_number=?,
                    sharepoint_case_folder=?,
                    sharepoint_case_web_url=?
                WHERE id=?
                """,
                (
                    new_grievance_id,
                    new_grievance_number,
                    new_sharepoint_case_folder,
                    new_sharepoint_case_web_url,
                    case_id,
                ),
            )
            con.execute(
                """
                UPDATE documents
                SET sharepoint_generated_url=CASE
                      WHEN sharepoint_generated_url IS NULL THEN NULL
                      ELSE REPLACE(sharepoint_generated_url, ?, ?)
                    END,
                    sharepoint_signed_url=CASE
                      WHEN sharepoint_signed_url IS NULL THEN NULL
                      ELSE REPLACE(sharepoint_signed_url, ?, ?)
                    END,
                    sharepoint_audit_url=CASE
                      WHEN sharepoint_audit_url IS NULL THEN NULL
                      ELSE REPLACE(sharepoint_audit_url, ?, ?)
                    END
                WHERE case_id=?
                """,
                (
                    old_grievance_id,
                    new_grievance_id,
                    old_grievance_id,
                    new_grievance_id,
                    old_grievance_id,
                    new_grievance_id,
                    case_id,
                ),
            )
            con.execute(
                """
                UPDATE events
                SET details_json=REPLACE(details_json, ?, ?)
                WHERE case_id=? AND details_json LIKE '%' || ? || '%'
                """,
                (old_grievance_id, new_grievance_id, case_id, old_grievance_id),
            )

        sequence_after = max(sequence_before, max_new_seq)
        if dry_run:
            con.rollback()
        else:
            con.execute(
                """
                INSERT INTO grievance_id_sequences(year, last_seq, updated_at_utc)
                VALUES(?,?,?)
                ON CONFLICT(year) DO UPDATE SET
                  last_seq=excluded.last_seq,
                  updated_at_utc=excluded.updated_at_utc
                """,
                (year, sequence_after, utcnow()),
            )
            con.commit()

        return GrievanceNumberRepairResult(
            db_path=db_path,
            cutoff_id=cutoff_id,
            expected_member_name=expected_member_name,
            affected_case_count=len(changes),
            sequence_year=year,
            sequence_before=sequence_before,
            sequence_after=sequence_after,
            dry_run=dry_run,
            changes=changes,
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
