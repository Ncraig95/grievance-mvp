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

from grievance_api.dues_forms import database, exporter, filters, scanner, sharepoint_sync
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


class DuesFormsFilterTests(unittest.TestCase):
    def test_filename_filter_does_not_ignore_valid_jackson_name(self):
        self.assertEqual(filters.should_ignore_filename("L3106 Stevie Jackson.pdf"), (False, ""))

    def test_filename_filter_ignores_coj_and_perc_card_files(self):
        cases = {
            "COJ employee card.pdf": "filename_contains_coj",
            "cityofjacksonville onboarding.pdf": "filename_contains_cityofjacksonville",
            "city_of_jacksonville form.pdf": "filename_contains_city_of_jacksonville",
            "member perc_card.pdf": "filename_contains_perc_card",
        }
        for filename, reason in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(filters.should_ignore_filename(filename), (True, reason))

    def test_text_filter_ignores_city_of_jacksonville_text(self):
        self.assertEqual(
            filters.should_ignore_text("City of Jacksonville Employee Information"),
            (True, "text_contains_city_of_jacksonville"),
        )

    def test_text_filter_ignores_employee_no_and_pay_grade(self):
        ignored, reason = filters.should_ignore_text("Employee No: 12345\nPay Grade: 10")

        self.assertTrue(ignored)
        self.assertIn("employee_no", reason)
        self.assertIn("pay_grade", reason)

    def test_text_filter_does_not_ignore_valid_dues_authorization(self):
        self.assertEqual(filters.should_ignore_text(SAMPLE_OCR_TEXT), (False, ""))


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
    def test_filename_ignored_pdf_moves_to_ignored_without_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            inbox = data_dir / "inbox"
            (inbox / "COJ packet.pdf").write_bytes(b"coj fake pdf")
            calls: list[str] = []

            def extractor(path: Path):
                calls.append(path.name)
                return SAMPLE_OCR_TEXT, "test"

            result = scanner.scan_once(
                data_dir=data_dir,
                db_path=db_path,
                stable_delay_seconds=0,
                extractor=extractor,
            )

            ignored_rows = database.list_ignored_files(db_path=db_path)
            self.assertEqual(result["ignored"], 1)
            self.assertEqual(result["processed"], 0)
            self.assertEqual(calls, [])
            self.assertEqual(database.count_records(db_path=db_path), 0)
            self.assertEqual(len(ignored_rows), 1)
            self.assertEqual(ignored_rows[0]["source_filename"], "COJ packet.pdf")
            self.assertEqual(ignored_rows[0]["ignored_reason"], "filename_contains_coj")
            self.assertTrue(list((data_dir / "ignored").glob("*/*.pdf")))

    def test_text_ignored_pdf_moves_to_ignored_without_dues_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            inbox = data_dir / "inbox"
            (inbox / "mixed.pdf").write_bytes(b"mixed fake pdf")

            def extractor(path: Path):
                return "Employee No: 12345\nPay Grade: 10\nJob Title: Worker", "test"

            result = scanner.scan_once(
                data_dir=data_dir,
                db_path=db_path,
                stable_delay_seconds=0,
                extractor=extractor,
            )

            ignored_rows = database.list_ignored_files(db_path=db_path)
            self.assertEqual(result["ignored"], 1)
            self.assertEqual(result["processed"], 0)
            self.assertEqual(database.count_records(db_path=db_path), 0)
            self.assertEqual(len(ignored_rows), 1)
            self.assertEqual(ignored_rows[0]["source_filename"], "mixed.pdf")
            self.assertIn("employee_no", ignored_rows[0]["ignored_reason"])
            self.assertTrue(list((data_dir / "ignored").glob("*/*.pdf")))

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
            self.assertTrue(graph.kwargs["recursive"])
            self.assertTrue((data_dir / "inbox" / "ecard.pdf").exists())
            self.assertTrue(database.sharepoint_item_seen(drive_id="drive-1", item_id="item-1", db_path=db_path))

    def test_sharepoint_sync_falls_back_to_documents_library_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)

            class FakeGraph:
                def __init__(self):
                    self.libraries: list[str] = []

                def list_files_in_folder_path(self, **kwargs):  # noqa: ANN003
                    self.libraries.append(kwargs["library"])
                    if kwargs["library"] != "Documents":
                        raise RuntimeError(
                            f"Could not find document library drive named '{kwargs['library']}'"
                        )
                    return [
                        SimpleNamespace(
                            drive_id="drive-1",
                            item_id="item-1",
                            name="ecard.pdf",
                            path="New Member E-Cards/202605/ecard.pdf",
                            web_url="https://example.invalid/ecard.pdf",
                        )
                    ]

                def download_item_bytes(self, **kwargs):  # noqa: ANN003
                    return b"fake remote pdf"

            graph = FakeGraph()
            settings = sharepoint_sync.SharePointSyncSettings(
                enabled=True,
                site_hostname="cwa3106.sharepoint.com",
                site_path="/sites/CWA3106",
                library="Grievances Library - Documents",
                folder_path="New Member E-Cards",
            )

            result = sharepoint_sync.sync_sharepoint_pdfs_to_inbox(
                data_dir=data_dir,
                db_path=db_path,
                settings=settings,
                graph=graph,
            )

            self.assertEqual(graph.libraries[:2], ["Grievances Library - Documents", "Documents"])
            self.assertEqual(result["downloaded"], 1)
            self.assertTrue((data_dir / "inbox" / "ecard.pdf").exists())

    def test_sharepoint_sync_continues_after_download_write_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)

            class FakeGraph:
                def list_files_in_folder_path(self, **kwargs):  # noqa: ANN003
                    return [
                        SimpleNamespace(
                            drive_id="drive-1",
                            item_id="item-1",
                            name="ecard.pdf",
                            path="New Member E-Cards/202605/ecard.pdf",
                            web_url="https://example.invalid/ecard.pdf",
                        )
                    ]

                def download_item_bytes(self, **kwargs):  # noqa: ANN003
                    return b"fake remote pdf"

            settings = sharepoint_sync.SharePointSyncSettings(
                enabled=True,
                site_hostname="cwa3106.sharepoint.com",
                site_path="/sites/GrievancesLibrary",
                library="Documents",
                folder_path="New Member E-Cards",
            )

            with patch.object(Path, "write_bytes", side_effect=FileNotFoundError("missing tmp")):
                result = sharepoint_sync.sync_sharepoint_pdfs_to_inbox(
                    data_dir=data_dir,
                    db_path=db_path,
                    settings=settings,
                    graph=FakeGraph(),
                )

            self.assertEqual(result["downloaded"], 0)
            self.assertEqual(result["download_errors"], 1)


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

    def test_dues_forms_page_renders_manual_scan_button(self):
        from grievance_api.dues_forms.routes import _render_list

        html = _render_list([], selected_status=None, scan_result="Manual scan complete: 0 processed.")

        self.assertIn('/dues-forms/run-scan', html)
        self.assertIn('/dues-forms/ignored', html)
        self.assertIn('Run Manual Scan', html)
        self.assertIn('Ignored Files', html)
        self.assertIn('Manual scan complete', html)

    def test_scan_result_summary_includes_sharepoint_zero_counts(self):
        from grievance_api.dues_forms.routes import _scan_result_summary

        html = _scan_result_summary(
            {
                "processed": 0,
                "needs_review": 0,
                "failed": 0,
                "ignored": 0,
                "duplicates": 0,
                "sharepoint": {
                    "remote_files": 0,
                    "downloaded": 0,
                    "already_downloaded": 0,
                    "duplicate_hashes": 0,
                    "skipped_non_pdf": 0,
                },
            }
        )

        self.assertIn("SharePoint: 0 PDFs downloaded from 0 remote files", html)

    def test_manual_sharepoint_settings_force_sync_enabled(self):
        from grievance_api.dues_forms.routes import _manual_sharepoint_settings

        with patch.dict(os.environ, {"DUES_FORMS_SHAREPOINT_ENABLED": "0"}):
            settings = _manual_sharepoint_settings()

        self.assertTrue(settings.enabled)
        self.assertTrue(settings.recursive)

    def test_ignored_page_renders_ignored_file_details(self):
        from grievance_api.dues_forms.routes import _render_ignored

        html = _render_ignored(
            [
                {
                    "ignored_at": "2026-05-22T12:00:00+00:00",
                    "source_filename": "COJ packet.pdf",
                    "ignored_reason": "filename_contains_coj",
                    "source_path": "/tmp/ignored/COJ packet.pdf",
                    "source_sha256": "abc123",
                }
            ]
        )

        self.assertIn("2026-05-22T12:00:00+00:00", html)
        self.assertIn("COJ packet.pdf", html)
        self.assertIn("filename_contains_coj", html)
        self.assertIn("/tmp/ignored/COJ packet.pdf", html)
        self.assertIn("abc123", html)

    @unittest.skipIf(TestClient is None, "httpx is not installed for FastAPI TestClient")
    def test_routes_render_exports_and_update_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir, db_path = _temp_paths(tmp)
            database.ensure_directories(data_dir=data_dir, db_path=db_path)
            database.insert_ignored_file(
                source_filename="COJ packet.pdf",
                source_path="/tmp/ignored/COJ packet.pdf",
                source_sha256="ignored-route-123",
                ignored_reason="filename_contains_coj",
                db_path=db_path,
            )
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
                ignored_response = client.get("/dues-forms/ignored")
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
            self.assertEqual(ignored_response.status_code, 200)
            self.assertIn("COJ packet.pdf", ignored_response.text)
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

    def test_schema_creates_ignored_files_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            _data_dir, db_path = _temp_paths(tmp)
            database.init_db(db_path)
            con = sqlite3.connect(db_path)
            try:
                columns = [row[1] for row in con.execute("PRAGMA table_info(ignored_files)").fetchall()]
            finally:
                con.close()

            self.assertEqual(columns, list(database.IGNORED_FILES_COLUMNS))
