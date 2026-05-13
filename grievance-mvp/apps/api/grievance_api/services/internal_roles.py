from __future__ import annotations

from typing import Any

from ..db.db import Db, utcnow


INTERNAL_ROLE_VALUES = {"president", "treasurer"}
INTERNAL_ROLE_STATUSES = {"active", "disabled"}


def normalize_internal_role(value: object) -> str:
    role = str(value or "").strip().lower()
    if role not in INTERNAL_ROLE_VALUES:
        raise ValueError("role must be president or treasurer")
    return role


def normalize_role_status(value: object) -> str:
    status = str(value or "active").strip().lower()
    if status not in INTERNAL_ROLE_STATUSES:
        raise ValueError("status must be active or disabled")
    return status


def normalize_role_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("principal_email must be a valid email address")
    return email


def normalize_optional_role_text(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _role_assignment_row(row: tuple[Any, ...]) -> dict[str, object]:
    return {
        "assignment_id": int(row[0]),
        "principal_id": normalize_optional_role_text(row[1]),
        "principal_email": str(row[2] or ""),
        "principal_display_name": normalize_optional_role_text(row[3]),
        "role": str(row[4] or ""),
        "status": str(row[5] or ""),
        "created_at_utc": str(row[6] or ""),
        "updated_at_utc": str(row[7] or ""),
        "assigned_by": str(row[8] or ""),
    }


async def list_internal_role_assignments(db: Db) -> list[dict[str, object]]:
    rows = await db.fetchall(
        """
        SELECT id, principal_id, principal_email, principal_display_name,
               role, status, created_at_utc, updated_at_utc, assigned_by
        FROM internal_role_assignments
        ORDER BY role, lower(principal_display_name), lower(principal_email), id
        """
    )
    return [_role_assignment_row(row) for row in rows]


async def internal_role_assignment_by_id(db: Db, assignment_id: int) -> dict[str, object] | None:
    row = await db.fetchone(
        """
        SELECT id, principal_id, principal_email, principal_display_name,
               role, status, created_at_utc, updated_at_utc, assigned_by
        FROM internal_role_assignments
        WHERE id=?
        """,
        (assignment_id,),
    )
    return _role_assignment_row(row) if row else None


async def active_internal_roles_for_user(
    db: Db,
    *,
    user_id: str | None,
    email: str | None,
) -> tuple[str, ...]:
    clauses: list[str] = []
    params: list[object] = []
    normalized_user_id = str(user_id or "").strip().lower()
    normalized_email = str(email or "").strip().lower()
    if normalized_user_id:
        clauses.append("lower(COALESCE(principal_id, '')) = ?")
        params.append(normalized_user_id)
    if normalized_email:
        clauses.append("lower(COALESCE(principal_email, '')) = ?")
        params.append(normalized_email)
    if not clauses:
        return ()

    rows = await db.fetchall(
        f"""
        SELECT role
        FROM internal_role_assignments
        WHERE status='active' AND ({' OR '.join(clauses)})
        ORDER BY role
        """,
        tuple(params),
    )
    return tuple(sorted({str(row[0] or "").strip().lower() for row in rows if str(row[0] or "").strip()}))


async def upsert_internal_role_assignment(
    db: Db,
    *,
    principal_id: str | None,
    principal_email: str,
    principal_display_name: str | None,
    role: str,
    status: str = "active",
    assigned_by: str,
) -> dict[str, object]:
    normalized_email = normalize_role_email(principal_email)
    normalized_role = normalize_internal_role(role)
    normalized_status = normalize_role_status(status)
    normalized_principal_id = normalize_optional_role_text(principal_id)
    normalized_display_name = normalize_optional_role_text(principal_display_name)
    now = utcnow()
    await db.exec(
        """
        INSERT INTO internal_role_assignments(
          principal_id, principal_email, principal_display_name, role, status,
          created_at_utc, updated_at_utc, assigned_by
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(principal_email, role) DO UPDATE SET
          principal_id=excluded.principal_id,
          principal_display_name=excluded.principal_display_name,
          status=excluded.status,
          updated_at_utc=excluded.updated_at_utc,
          assigned_by=excluded.assigned_by
        """,
        (
            normalized_principal_id,
            normalized_email,
            normalized_display_name,
            normalized_role,
            normalized_status,
            now,
            now,
            assigned_by,
        ),
    )
    row = await db.fetchone(
        """
        SELECT id, principal_id, principal_email, principal_display_name,
               role, status, created_at_utc, updated_at_utc, assigned_by
        FROM internal_role_assignments
        WHERE principal_email=? AND role=?
        """,
        (normalized_email, normalized_role),
    )
    if not row:
        raise RuntimeError("failed to save internal role assignment")
    return _role_assignment_row(row)


async def delete_internal_role_assignment(db: Db, assignment_id: int) -> dict[str, object]:
    existing = await internal_role_assignment_by_id(db, assignment_id)
    if not existing:
        raise ValueError("assignment_id not found")
    await db.exec("DELETE FROM internal_role_assignments WHERE id=?", (assignment_id,))
    return existing
