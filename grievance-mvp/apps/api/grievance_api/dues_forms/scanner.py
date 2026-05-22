from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import database, exporter, sharepoint_sync
from .parser import parse_dues_form_text, text_needs_ocr

ExtractResult = tuple[str, str]
Extractor = Callable[[Path], ExtractResult]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_size_is_stable(path: Path, *, delay_seconds: float = 1.0) -> bool:
    try:
        first = path.stat().st_size
    except FileNotFoundError:
        return False
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    try:
        second = path.stat().st_size
    except FileNotFoundError:
        return False
    return first == second


def _extract_direct_text(pdf_path: Path, logger: logging.Logger | None = None) -> str:
    try:
        import pdfplumber
    except ImportError:
        if logger:
            logger.info("pdfplumber is not available; using OCR for %s", pdf_path.name)
        return ""

    try:
        parts: list[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(part for part in parts if part)
    except Exception as exc:
        if logger:
            logger.warning("direct text extraction failed for %s: %s", pdf_path.name, exc)
        return ""


def _render_pdf_to_pngs(pdf_path: Path, output_prefix: Path) -> list[Path]:
    try:
        subprocess.run(
            ["pdftoppm", "-r", "200", "-png", str(pdf_path), str(output_prefix)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdftoppm not found; install Ubuntu package poppler-utils") from exc
    images = sorted(output_prefix.parent.glob(f"{output_prefix.name}-*.png"))
    if not images:
        raise RuntimeError("PDF render failed: pdftoppm did not produce PNG output")
    return images


def _ocr_png(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("tesseract not found; install Ubuntu package tesseract-ocr") from exc
    return proc.stdout or ""


def _extract_ocr_text(pdf_path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="dues-form-ocr-") as tmp:
        output_prefix = Path(tmp) / "page"
        images = _render_pdf_to_pngs(pdf_path, output_prefix)
        return "\n".join(_ocr_png(image) for image in images).strip()


def extract_pdf_text(pdf_path: Path, logger: logging.Logger | None = None) -> ExtractResult:
    direct_text = _extract_direct_text(pdf_path, logger=logger)
    if direct_text and not text_needs_ocr(direct_text):
        return direct_text, "direct"

    if logger:
        logger.info("using OCR fallback for %s", pdf_path.name)
    ocr_text = _extract_ocr_text(pdf_path)
    if not ocr_text.strip():
        raise RuntimeError("OCR produced no text")
    return ocr_text, "ocr"


def _month_folder(now: datetime | None = None) -> str:
    active = now or datetime.now(timezone.utc)
    return active.strftime("%Y-%m")


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"unable to find unique destination for {path}")


def move_pdf(pdf_path: Path, *, data_dir: Path, bucket: str) -> Path:
    dest_dir = data_dir / bucket / _month_folder()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_destination(dest_dir / pdf_path.name)
    shutil.move(str(pdf_path), str(dest))
    return dest


def _save_raw_text(*, data_dir: Path, source_sha256: str, raw_text: str) -> Path:
    raw_dir = data_dir / "raw_text"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{source_sha256}.txt"
    raw_path.write_text(raw_text or "", encoding="utf-8")
    return raw_path


def _base_record(*, pdf_path: Path, source_sha256: str) -> dict[str, Any]:
    return {
        "source_filename": pdf_path.name,
        "source_path": str(pdf_path),
        "source_sha256": source_sha256,
        "processed_at": database.utcnow(),
        "form_type": "dues_deduction_form",
    }


def process_pdf(
    pdf_path: Path,
    *,
    data_dir: Path,
    db_path: Path,
    extractor: Extractor | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    source_sha256 = sha256_file(pdf_path)
    if database.record_exists(source_sha256, db_path=db_path):
        dest = move_pdf(pdf_path, data_dir=data_dir, bucket="processed")
        if logger:
            logger.info("skipped duplicate %s; moved to %s", pdf_path.name, dest)
        return {"status": "duplicate", "source_sha256": source_sha256, "destination": dest}

    raw_text = ""
    extraction_method = "failed"
    record_id: int | None = None
    try:
        if extractor is not None:
            raw_text, extraction_method = extractor(pdf_path)
        else:
            raw_text, extraction_method = extract_pdf_text(pdf_path, logger=logger)
        parsed = parse_dues_form_text(raw_text)
        _save_raw_text(data_dir=data_dir, source_sha256=source_sha256, raw_text=raw_text)
        record = _base_record(pdf_path=pdf_path, source_sha256=source_sha256)
        record.update(parsed)
        record["extraction_method"] = extraction_method
        record_id = database.insert_record(record, db_path=db_path)
        bucket = "processed" if record["review_status"] == "processed" else "needs_review"
        dest = move_pdf(pdf_path, data_dir=data_dir, bucket=bucket)
        database.update_source_path(record_id, str(dest), db_path=db_path)
        if logger:
            logger.info("%s imported as %s; moved to %s", pdf_path.name, record["review_status"], dest)
        return {
            "status": str(record["review_status"]),
            "record_id": record_id,
            "source_sha256": source_sha256,
            "destination": dest,
        }
    except sqlite3.IntegrityError:
        dest = move_pdf(pdf_path, data_dir=data_dir, bucket="processed")
        if logger:
            logger.info("skipped duplicate %s after parse; moved to %s", pdf_path.name, dest)
        return {"status": "duplicate", "source_sha256": source_sha256, "destination": dest}
    except Exception as exc:
        error = str(exc)
        failed_record = _base_record(pdf_path=pdf_path, source_sha256=source_sha256)
        failed_record.update(
            {
                "extraction_method": extraction_method,
                "review_status": "needs_review",
                "error_message": error,
                "raw_text": raw_text,
            }
        )
        try:
            record_id = database.insert_record(failed_record, db_path=db_path)
        except sqlite3.IntegrityError:
            record_id = None
        dest = move_pdf(pdf_path, data_dir=data_dir, bucket="failed")
        if record_id is not None:
            database.update_source_path(record_id, str(dest), db_path=db_path)
        if raw_text:
            _save_raw_text(data_dir=data_dir, source_sha256=source_sha256, raw_text=raw_text)
        if logger:
            logger.exception("%s failed; moved to %s", pdf_path.name, dest)
        return {
            "status": "failed",
            "record_id": record_id,
            "source_sha256": source_sha256,
            "destination": dest,
            "error": error,
        }


def scan_once(
    *,
    data_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    stable_delay_seconds: float = 1.0,
    extractor: Extractor | None = None,
    sharepoint_settings: sharepoint_sync.SharePointSyncSettings | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    log = logger or logging.getLogger("grievance_api.dues_forms.scanner")
    resolved_data_dir = database.ensure_directories(data_dir=data_dir, db_path=db_path)
    resolved_db_path = database.resolve_db_path(db_path)
    database.init_db(resolved_db_path)

    inbox = resolved_data_dir / "inbox"
    summary: dict[str, Any] = {
        "scanned": 0,
        "processed": 0,
        "needs_review": 0,
        "failed": 0,
        "duplicates": 0,
        "skipped_unstable": 0,
        "sharepoint": {},
        "exports": {},
    }
    active_sharepoint_settings = sharepoint_settings or sharepoint_sync.SharePointSyncSettings.from_env()
    if active_sharepoint_settings.enabled:
        summary["sharepoint"] = sharepoint_sync.sync_sharepoint_pdfs_to_inbox(
            data_dir=resolved_data_dir,
            db_path=resolved_db_path,
            settings=active_sharepoint_settings,
            logger=log,
        )
    pdfs = sorted(path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")
    for pdf_path in pdfs:
        if not file_size_is_stable(pdf_path, delay_seconds=stable_delay_seconds):
            summary["skipped_unstable"] += 1
            log.info("skipping %s because file size is still changing", pdf_path.name)
            continue
        summary["scanned"] += 1
        result = process_pdf(
            pdf_path,
            data_dir=resolved_data_dir,
            db_path=resolved_db_path,
            extractor=extractor,
            logger=log,
        )
        status = result["status"]
        if status == "processed":
            summary["processed"] += 1
        elif status == "needs_review":
            summary["needs_review"] += 1
        elif status == "duplicate":
            summary["duplicates"] += 1
        elif status == "failed":
            summary["failed"] += 1

    summary["exports"] = exporter.regenerate_exports(db_path=resolved_db_path, data_dir=resolved_data_dir)
    log.info(
        "scan complete: scanned=%s processed=%s needs_review=%s failed=%s duplicates=%s skipped_unstable=%s",
        summary["scanned"],
        summary["processed"],
        summary["needs_review"],
        summary["failed"],
        summary["duplicates"],
        summary["skipped_unstable"],
    )
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan local dues deduction PDF forms once.")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit.")
    parser.add_argument("--data-dir", help="Override dues forms data directory.")
    parser.add_argument("--db-path", help="Override dues forms SQLite database path.")
    parser.add_argument(
        "--stable-delay",
        type=float,
        default=1.0,
        help="Seconds to wait while checking that incoming PDF size is stable.",
    )
    parser.add_argument(
        "--sharepoint-sync",
        action="store_true",
        help="Download PDFs from the configured SharePoint folder into the local inbox before scanning.",
    )
    parser.add_argument("--graph-config", help="Path to config.yaml containing Graph app credentials.")
    parser.add_argument("--sharepoint-site-hostname", help="SharePoint hostname, for example cwa3106.sharepoint.com.")
    parser.add_argument("--sharepoint-site-path", help="SharePoint site path, for example /sites/CWA3106.")
    parser.add_argument(
        "--sharepoint-library",
        default=None,
        help="SharePoint document library name. Defaults to Grievances Library - Documents.",
    )
    parser.add_argument(
        "--sharepoint-folder",
        default=None,
        help="Folder path within the document library. Defaults to New Member E-Cards.",
    )
    parser.add_argument("--sharepoint-recursive", action="store_true", help="Include PDFs in nested SharePoint folders.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _ = args.once
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        scan_once(
            data_dir=args.data_dir,
            db_path=args.db_path,
            stable_delay_seconds=max(0.0, float(args.stable_delay)),
            sharepoint_settings=sharepoint_sync.SharePointSyncSettings.from_env(
                enabled=True if args.sharepoint_sync else None,
                config_path=args.graph_config,
                site_hostname=args.sharepoint_site_hostname,
                site_path=args.sharepoint_site_path,
                library=args.sharepoint_library,
                folder_path=args.sharepoint_folder,
                recursive=True if args.sharepoint_recursive else None,
            ),
        )
    except Exception as exc:
        logging.getLogger("grievance_api.dues_forms.scanner").exception("dues form scan failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
