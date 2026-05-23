from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from . import database


def csv_path(data_dir: str | Path | None = None) -> Path:
    return database.resolve_data_dir(data_dir) / "exports" / "dues_deduction_forms.csv"


def xlsx_path(data_dir: str | Path | None = None) -> Path:
    return database.resolve_data_dir(data_dir) / "exports" / "dues_deduction_forms.xlsx"


def export_csv(
    *,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> Path:
    database.ensure_directories(data_dir=data_dir, db_path=db_path)
    out_path = csv_path(data_dir)
    records = rows if rows is not None else database.list_records(db_path=db_path)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=database.EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({column: record.get(column, "") for column in database.EXPORT_COLUMNS})
    return out_path


def export_xlsx(
    *,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> Path | None:
    try:
        from openpyxl import Workbook
    except ImportError:
        return None

    database.ensure_directories(data_dir=data_dir, db_path=db_path)
    out_path = xlsx_path(data_dir)
    records = rows if rows is not None else database.list_records(db_path=db_path)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Dues Deduction Forms"
    sheet.append(list(database.EXPORT_COLUMNS))
    for record in records:
        sheet.append([record.get(column, "") for column in database.EXPORT_COLUMNS])
    sheet.freeze_panes = "A2"
    workbook.save(out_path)
    return out_path


def regenerate_exports(
    *,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> dict[str, Path | None]:
    records = database.list_records(db_path=db_path)
    return {
        "csv": export_csv(db_path=db_path, data_dir=data_dir, rows=records),
        "xlsx": export_xlsx(db_path=db_path, data_dir=data_dir, rows=records),
    }
