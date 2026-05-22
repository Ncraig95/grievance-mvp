from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except RuntimeError:
    FastAPI = None
    TestClient = None

from grievance_api.dues_forms import database, exporter, scanner, sharepoint_sync
from grievance_api.dues_forms.parser import parse_dues_form_text
from grievance_api.dues_forms.routes import router as dues_forms_router


SAMPLE_OCR_TEXT = """
Dues (or Equivalent Fee) Deduction Form
Contract: Wire Tech
First Name: Jordan
Last Name: Member
Work Location Address: 123 Work Rd
Work Location State: fl
Employee ID: JS12345
Local No: 3106
Home Address: 456 Home St
City: Jacksonville
State: fl
Zip: 32205
Personal Email Address: jordan@example.org
Personal Cell Phone: (904) 555-1212
Timestamp: 2026-05-22 09:15:00
IP Address: 127.0.0.1
Dues Deduction Authorization: I authorize dues deduction.
This authorization remains in effect until revoked.
Electronic Signature: Jordan Member
"""


def _temp_paths(tmpdir: str) -> tuple[Path, Path]:
    root = Path(tmpdir)
    return root / "data" / "dues_forms", root / "instance" / "dues_forms.sqlite3"


class DuesFormsParserTests(unittest.TestCase):
    def test_parser_handles_sample_ocr_text_and_normalizes_values(self):
        record = parse_dues_form_text(SAMPLE_OCR_TEXT)

        self.assertEqual(record["review_status"], "processed")
        self.assertEqual(record["first_name"], "Jordan")
        self.assertEqual(record["last_name"], "Member")
        self.assertEqual(record["work_location_state"], "FL")
        self.assertEqual(record["state"], "FL")
        self.assertEqual(record["zip"], "32205")
        self.assertEqual(record["personal_cell_phone"], "9045551212")
        self.assertIn("until revoked", str(record["dues_deduction_authorization"]))
        self.assertEqual(record["electronic_signature"], "Jordan Member")

    def test_parser_flags_suspicious_employee_id_for_review(self):
        record = parse_dues_form_text(SAMPLE_OCR_TEXT.replace("JS12345", "J$12345"))

        self.assertEqual(record["employee_id"], "J$12345")
        self.assertEqual(record["review_status"], "needs_review")
        self.assertIn("suspicious", str(record["error_message"]))


class DuesFormsScannerTests(unittest.TestCase):
    def test_duplicate_pdf_hashes_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            inbox = data_dir / "inbox"
            (inbox / "first.pdf").write_bytes(b"same fake pdf")

            calls: list[str] = []

            def extractor(path: Path):
                calls.append(path.name)
                return SAMPLE_OCR_TEXT, "test"

            first = scanner.scan_once(
                data_dir=data_dir,
                db_path=db_path,
                stable_delay_seconds=0,
                extractor=extractor,
            )
            (inbox / "second.pdf").write_bytes(b"same fake pdf")
            second = scanner.scan_once(
                data_dir=data_dir,
                db_path=db_path,
                stable_delay_seconds=0,
                extractor=extractor,
            )

            self.assertEqual(first["processed"], 1)
            self.assertEqual(second["duplicates"], 1)
            self.assertEqual(calls, ["first.pdf"])
            self.assertEqual(database.count_records(db_path=db_path), 1)

    def test_scanner_moves_files_by_outcome_and_saves_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            inbox = data_dir / "inbox"
            (inbox / "ok.pdf").write_bytes(b"ok pdf")
            (inbox / "review.pdf").write_bytes(b"review pdf")
            (inbox / "bad.pdf").write_bytes(b"bad pdf")

            def extractor(path: Path):
                if path.name == "bad.pdf":
                    raise RuntimeError("ocr failed")
                if path.name == "review.pdf":
                    return SAMPLE_OCR_TEXT.replace("Employee ID: JS12345\n", "Employee ID:\n"), "test"
                return SAMPLE_OCR_TEXT, "test"

            result = scanner.scan_once(
                data_dir=data_dir,
                db_path=db_path,
                stable_delay_seconds=0,
                extractor=extractor,
            )

            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["needs_review"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(list(inbox.glob("*.pdf")), [])
            self.assertTrue(list((data_dir / "processed").glob("*/*.pdf")))
            self.assertTrue(list((data_dir / "needs_review").glob("*/*.pdf")))
            self.assertTrue(list((data_dir / "failed").glob("*/*.pdf")))
            self.assertGreaterEqual(len(list((data_dir / "raw_text").glob("*.txt"))), 2)


class DuesFormsSharePointSyncTests(unittest.TestCase):
    def test_sharepoint_sync_downloads_pdf_once_to_local_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)

            class FakeGraph:
                def __init__(self):
                    self.download_calls = 0

                def list_files_in_folder_path(self, **kwargs):  # noqa: ANN003
                    self.kwargs = kwargs
                    return [
                        SimpleNamespace(
                            drive_id="drive-1",
                            item_id="item-1",
                            name="ecard.pdf",
                            path="New Member E-Cards/ecard.pdf",
                            web_url="https://example.invalid/ecard.pdf",
                        ),
                        SimpleNamespace(
                            drive_id="drive-1",
                            item_id="item-2",
                            name="notes.txt",
                            path="New Member E-Cards/notes.txt",
                            web_url=None,
                        ),
                    ]

                def download_item_bytes(self, **kwargs):  # noqa: ANN003
                    self.download_calls += 1
                    return b"fake remote pdf"

            graph = FakeGraph()
            settings = sharepoint_sync.SharePointSyncSettings(
                enabled=True,
                site_hostname="cwa3106.sharepoint.com",
                site_path="/sites/CWA3106",
                library="Grievances Library - Documents",
                folder_path="New Member E-Cards",
            )

            first = sharepoint_sync.sync_sharepoint_pdfs_to_inbox(
                data_dir=data_dir,
                db_path=db_path,
                settings=settings,
                graph=graph,
            )
            second = sharepoint_sync.sync_sharepoint_pdfs_to_inbox(
                data_dir=data_dir,
                db_path=db_path,
                settings=settings,
                graph=graph,
            )

            self.assertEqual(first["downloaded"], 1)
            self.assertEqual(first["skipped_non_pdf"], 1)
            self.assertEqual(second["already_downloaded"], 1)
            self.assertEqual(graph.download_calls, 1)
            self.assertTrue((data_dir / "inbox" / "ecard.pdf").exists())
            self.assertTrue(database.sharepoint_item_seen(drive_id="drive-1", item_id="item-1", db_path=db_path))



class DuesFormsExportTests(unittest.TestCase):
    def test_csv_export_contains_expected_columns_and_omits_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            record = {
                "source_filename": "sample.pdf",
                "source_path": "/tmp/sample.pdf",
                "source_sha256": "abc123",
                "processed_at": database.utcnow(),
                "extraction_method": "test",
                **parse_dues_form_text(SAMPLE_OCR_TEXT),
            }
            database.insert_record(record, db_path=db_path)

            paths = exporter.regenerate_exports(db_path=db_path, data_dir=data_dir)
            header = Path(paths["csv"]).read_text(encoding="utf-8").splitlines()[0]

            self.assertIn("source_filename", header)
            self.assertIn("source_sha256", header)
            self.assertIn("review_status", header)
            self.assertNotIn("raw_text", header)
            if paths["xlsx"] is not None:
                self.assertTrue(Path(paths["xlsx"]).exists())


class DuesFormsRouteTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "httpx is not installed for FastAPI TestClient")
    def test_routes_render_exports_and_update_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            record_id = database.insert_record(
                {
                    "source_filename": "sample.pdf",
                    "source_path": "/tmp/sample.pdf",
                    "source_sha256": "route123",
                    "processed_at": database.utcnow(),
                    "extraction_method": "test",
                    **parse_dues_form_text(SAMPLE_OCR_TEXT),
                },
                db_path=db_path,
            )
            assert FastAPI is not None
            app = FastAPI()
            app.state.cfg = SimpleNamespace(
                officer_auth=SimpleNamespace(enabled=False),
                external_steward_auth=SimpleNamespace(enabled=False),
            )
            app.include_router(dues_forms_router)

            with patch.dict(
                os.environ,
                {"DUES_FORMS_DATA_DIR": str(data_dir), "DUES_FORMS_DB_PATH": str(db_path)},
            ):
                client = TestClient(app, client=("127.0.0.1", 50000))
                listing = client.get("/dues-forms")
                detail = client.get(f"/dues-forms/{record_id}")
                csv_response = client.get("/dues-forms/export.csv")
                status_response = client.post(
                    f"/dues-forms/{record_id}/status",
                    data={"review_status": "approved"},
                    follow_redirects=False,
                )

            self.assertEqual(listing.status_code, 200)
            self.assertIn("Dues Forms", listing.text)
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Jordan Member", detail.text)
            self.assertEqual(csv_response.status_code, 200)
            self.assertEqual(status_response.status_code, 303)
            self.assertEqual(database.get_record(record_id, db_path=db_path)["review_status"], "approved")


class DuesFormsSchemaTests(unittest.TestCase):
    def test_schema_creates_expected_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            _data_dir, db_path = _temp_paths(tmp)
            database.init_db(db_path)
            con = sqlite3.connect(db_path)
            try:
                columns = [row[1] for row in con.execute("PRAGMA table_info(dues_deduction_forms)").fetchall()]
            finally:
                con.close()

            self.assertEqual(columns, list(database.DB_COLUMNS))
