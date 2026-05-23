from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TABLE_NAME = "dues_deduction_forms"

DB_COLUMNS: tuple[str, ...] = (
    "id",
    "source_filename",
    "source_path",
    "source_sha256",
    "processed_at",
    "extraction_method",
    "review_status",
    "error_message",
    "form_type",
    "contract",
    "first_name",
    "last_name",
    "work_location_address",
    "work_location_state",
    "employee_id",
    "local_no",
    "home_address",
    "city",
    "state",
    "zip",
    "personal_email",
    "personal_cell_phone",
    "timestamp",
    "ip_address",
    "dues_deduction_authorization",
    "electronic_signature",
    "raw_text",
)

EXPORT_COLUMNS: tuple[str, ...] = tuple(column for column in DB_COLUMNS if column != "raw_text")
ALLOWED_REVIEW_STATUSES: tuple[str, ...] = ("needs_review", "processed", "approved", "rejected", "exported")
IGNORED_FILES_TABLE = "ignored_files"
IGNORED_FILES_COLUMNS: tuple[str, ...] = (
    "id",
    "source_filename",
    "source_path",
    "source_sha256",
    "ignored_reason",
    "ignored_at",
)
DUES_SUBDIRS: tuple[str, ...] = ("inbox", "processed", "needs_review", "failed", "ignored", "exports", "raw_text")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_root() -> Path:
    override = os.getenv("DUES_FORMS_PROJECT_ROOT") or os.getenv("GRIEVANCE_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "apps" / "api").exists() or (parent / "config").exists():
            return parent
    return current.parents[2]


def _container_data_root() -> Path | None:
    data_root = Path("/data")
    return data_root if data_root.exists() else None


def default_data_dir() -> Path:
    override = os.getenv("DUES_FORMS_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    container_data_root = _container_data_root()
    if container_data_root is not None:
        return container_data_root / "dues_forms"
    return project_root() / "data" / "dues_forms"


def default_db_path() -> Path:
    override = os.getenv("DUES_FORMS_DB_PATH")
    if override:
        return Path(override).expanduser().resolve()
    container_data_root = _container_data_root()
    if container_data_root is not None:
        return container_data_root / "dues_forms.sqlite3"
    return project_root() / "instance" / "dues_forms.sqlite3"


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    return Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path).expanduser().resolve() if db_path else default_db_path()


def ensure_directories(data_dir: str | Path | None = None, db_path: str | Path | None = None) -> Path:
    root = resolve_data_dir(data_dir)
    for subdir in DUES_SUBDIRS:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    resolve_db_path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return root


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(resolve_db_path(db_path))
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: str | Path | None = None) -> None:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_filename TEXT NOT NULL,
              source_path TEXT,
              source_sha256 TEXT NOT NULL UNIQUE,
              processed_at TEXT NOT NULL,
              extraction_method TEXT,
              review_status TEXT NOT NULL DEFAULT 'needs_review',
              error_message TEXT,
              form_type TEXT,
              contract TEXT,
              first_name TEXT,
              last_name TEXT,
              work_location_address TEXT,
              work_location_state TEXT,
              employee_id TEXT,
              local_no TEXT,
              home_address TEXT,
              city TEXT,
              state TEXT,
              zip TEXT,
              personal_email TEXT,
              personal_cell_phone TEXT,
              timestamp TEXT,
              ip_address TEXT,
              dues_deduction_authorization TEXT,
              electronic_signature TEXT,
              raw_text TEXT
            )
            """
        )
        con.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dues_deduction_forms_review_status "
            f"ON {TABLE_NAME}(review_status, processed_at)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS dues_sharepoint_files (
              drive_id TEXT NOT NULL,
              item_id TEXT NOT NULL,
              name TEXT NOT NULL,
              path TEXT,
              web_url TEXT,
              source_sha256 TEXT,
              local_path TEXT,
              downloaded_at TEXT NOT NULL,
              PRIMARY KEY (drive_id, item_id)
            )
            """
        )
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {IGNORED_FILES_TABLE} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_filename TEXT NOT NULL,
              source_path TEXT,
              source_sha256 TEXT,
              ignored_reason TEXT NOT NULL,
              ignored_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            f"CREATE INDEX IF NOT EXISTS idx_ignored_files_ignored_at "
            f"ON {IGNORED_FILES_TABLE}(ignored_at DESC, id DESC)"
        )
        con.commit()
    finally:
        con.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {column: row[column] for column in row.keys()}


def record_exists(source_sha256: str, db_path: str | Path | None = None) -> bool:
    init_db(db_path)
    with connect(db_path) as con:
        row = con.execute(
            f"SELECT id FROM {TABLE_NAME} WHERE source_sha256=?",
            (source_sha256,),
        ).fetchone()
    return row is not None


def insert_record(record: dict[str, Any], db_path: str | Path | None = None) -> int:
    init_db(db_path)
    payload = {column: record.get(column) for column in DB_COLUMNS if column != "id"}
    payload["review_status"] = payload.get("review_status") or "needs_review"
    payload["processed_at"] = payload.get("processed_at") or utcnow()
    columns = tuple(payload.keys())
    placeholders = ", ".join("?" for _column in columns)
    sql = f"INSERT INTO {TABLE_NAME}({', '.join(columns)}) VALUES({placeholders})"
    with connect(db_path) as con:
        cur = con.execute(sql, tuple(payload[column] for column in columns))
        con.commit()
        return int(cur.lastrowid)


def update_source_path(record_id: int, source_path: str, db_path: str | Path | None = None) -> None:
    init_db(db_path)
    with connect(db_path) as con:
        con.execute(f"UPDATE {TABLE_NAME} SET source_path=? WHERE id=?", (source_path, record_id))
        con.commit()


def update_review_status(record_id: int, review_status: str, db_path: str | Path | None = None) -> dict[str, Any]:
    if review_status not in ALLOWED_REVIEW_STATUSES:
        raise ValueError(f"unsupported review_status: {review_status}")
    init_db(db_path)
    with connect(db_path) as con:
        cur = con.execute(
            f"UPDATE {TABLE_NAME} SET review_status=? WHERE id=?",
            (review_status, record_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"dues form not found: {record_id}")
        con.commit()
    row = get_record(record_id, db_path=db_path)
    if row is None:
        raise KeyError(f"dues form not found: {record_id}")
    return row


def get_record(record_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as con:
        row = con.execute(f"SELECT * FROM {TABLE_NAME} WHERE id=?", (record_id,)).fetchone()
    return row_to_dict(row)


def list_records(review_status: str | None = None, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    params: tuple[Any, ...] = ()
    where = ""
    if review_status:
        where = "WHERE review_status=?"
        params = (review_status,)
    with connect(db_path) as con:
        rows = con.execute(
            f"SELECT * FROM {TABLE_NAME} {where} ORDER BY processed_at DESC, id DESC",
            params,
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def count_records(db_path: str | Path | None = None) -> int:
    init_db(db_path)
    with connect(db_path) as con:
        row = con.execute(f"SELECT COUNT(*) AS count FROM {TABLE_NAME}").fetchone()
    return int(row["count"] if row else 0)


def insert_ignored_file(
    *,
    source_filename: str,
    source_path: str | None,
    source_sha256: str | None,
    ignored_reason: str,
    db_path: str | Path | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as con:
        cur = con.execute(
            f"""
            INSERT INTO {IGNORED_FILES_TABLE}(
              source_filename, source_path, source_sha256, ignored_reason, ignored_at
            )
            VALUES(?,?,?,?,?)
            """,
            (source_filename, source_path, source_sha256, ignored_reason, utcnow()),
        )
        con.commit()
        return int(cur.lastrowid)


def list_ignored_files(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as con:
        rows = con.execute(
            f"SELECT * FROM {IGNORED_FILES_TABLE} ORDER BY ignored_at DESC, id DESC"
        ).fetchall()
    return [row_to_dict(row) or {} for row in rows]


def count_ignored_files(db_path: str | Path | None = None) -> int:
    init_db(db_path)
    with connect(db_path) as con:
        row = con.execute(f"SELECT COUNT(*) AS count FROM {IGNORED_FILES_TABLE}").fetchone()
    return int(row["count"] if row else 0)


def sharepoint_item_seen(*, drive_id: str, item_id: str, db_path: str | Path | None = None) -> bool:
    init_db(db_path)
    with connect(db_path) as con:
        row = con.execute(
            "SELECT 1 FROM dues_sharepoint_files WHERE drive_id=? AND item_id=?",
            (drive_id, item_id),
        ).fetchone()
    return row is not None


def mark_sharepoint_item_downloaded(
    *,
    drive_id: str,
    item_id: str,
    name: str,
    path: str,
    web_url: str,
    source_sha256: str,
    local_path: str,
    db_path: str | Path | None = None,
) -> None:
    init_db(db_path)
    with connect(db_path) as con:
        con.execute(
            """
            INSERT INTO dues_sharepoint_files(
              drive_id, item_id, name, path, web_url, source_sha256, local_path, downloaded_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(drive_id, item_id) DO UPDATE SET
              name=excluded.name,
              path=excluded.path,
              web_url=excluded.web_url,
              source_sha256=excluded.source_sha256,
              local_path=excluded.local_path,
              downloaded_at=excluded.downloaded_at
            """,
            (drive_id, item_id, name, path, web_url, source_sha256, local_path, utcnow()),
        )
        con.commit()
