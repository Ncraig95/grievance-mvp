from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest

from fastapi import HTTPException

from grievance_api.core.config import AppConfig, DocuSealConfig, EmailConfig, GraphConfig, GrievanceIdConfig
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services.grievance_id_allocator import (
    GrievanceIdAllocator,
    current_year_in_timezone,
    format_grievance_id,
    max_sequence_for_year,
    parse_case_folder_identifier,
)
from grievance_api.services.sharepoint_graph import CaseFolderRef
from grievance_api.web.routes_intake import _validate_grievance_input_mode


class ParserTests(unittest.TestCase):
    def test_parse_valid_folder_prefixes(self) -> None:
        self.assertEqual(parse_case_folder_identifier("2026001 John Doe"), (2026, 1))
        self.assertEqual(parse_case_folder_identifier("2026999 Jane Doe"), (2026, 999))
        self.assertEqual(parse_case_folder_identifier("20261000 Member"), (2026, 1000))

    def test_parse_ignores_non_matching_names(self) -> None:
        self.assertIsNone(parse_case_folder_identifier("Case 2026001 John Doe"))
        self.assertIsNone(parse_case_folder_identifier("20261 John Doe"))
        self.assertIsNone(parse_case_folder_identifier("20261000John Doe"))

    def test_year_max_and_formatting(self) -> None:
        names = [
            "2026001 Alice",
            "2026999 Bob",
            "20261000 Charlie",
            "2025123 Legacy",
            "not-a-case",
        ]
        self.assertEqual(max_sequence_for_year(names, year=2026), 1000)
        self.assertEqual(format_grievance_id(year=2026, sequence=1, min_width=3), "2026001")
        self.assertEqual(format_grievance_id(year=2026, sequence=999, min_width=3), "2026999")
        self.assertEqual(format_grievance_id(year=2026, sequence=1000, min_width=3), "20261000")
        self.assertEqual(format_grievance_id(year=2026, sequence=12544, min_width=3), "202612544")


class SequenceReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        migrate(self.tmp.name)
        self.db = Db(self.tmp.name)

    async def asyncTearDown(self) -> None:
        try:
            import os

            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    async def test_floor_seq_overflow(self) -> None:
        first = await self.db.reserve_next_grievance_seq(year=2026, floor_seq=999)
        second = await self.db.reserve_next_grievance_seq(year=2026, floor_seq=12543)
        self.assertEqual(first, 1000)
        self.assertEqual(second, 12544)

    async def test_parallel_allocations_are_unique(self) -> None:
        async def _reserve() -> int:
            return await self.db.reserve_next_grievance_seq(year=2026, floor_seq=0)

        results = await asyncio.gather(*[_reserve() for _ in range(20)])
        self.assertEqual(len(results), len(set(results)))
        self.assertEqual(sorted(results), list(range(1, 21)))


class AllocatorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        migrate(self.tmp.name)
        self.db = Db(self.tmp.name)

    async def asyncTearDown(self) -> None:
        try:
            import os

            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    async def test_allocator_uses_sharepoint_max_and_reserves_folder(self) -> None:
        year = current_year_in_timezone("America/New_York")

        class FakeGraph:
            def __init__(self):
                self.last_reserved_id: str | None = None

            def list_case_folder_names(self, **kwargs):  # noqa: ANN003
                _ = kwargs
                return [f"{year}999 Jane Doe", f"{year}1000 Other Person", "legacy-folder"]

            def ensure_case_folder(self, **kwargs):  # noqa: ANN003
                self.last_reserved_id = kwargs["grievance_id"]
                return CaseFolderRef(
                    drive_id="drive",
                    folder_id="folder",
                    folder_name=f"{kwargs['grievance_id']} {kwargs['member_name']}",
                    web_url="https://example.sharepoint.test/folder",
                )

        cfg = AppConfig(
            hmac_shared_secret="x",
            db_path=self.tmp.name,
            data_root="/tmp",
            docx_template_path="/tmp/template.docx",
            doc_templates={},
            libreoffice_timeout_seconds=45,
            graph=GraphConfig(
                tenant_id="tenant",
                client_id="client",
                cert_pem_path="/tmp/cert.pem",
                cert_thumbprint="thumb",
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                document_library="Documents",
                case_parent_folder="Grievances",
                generated_subfolder="Generated",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
                audit_backup_subfolders=(),
                audit_local_backup_roots=(),
                client_supplied_subfolder="Client supplied data",
                failed_processes_folder="config files/failed",
            ),
            docuseal=DocuSealConfig(
                base_url="http://docuseal",
                api_token="token",
                webhook_secret="secret",
                public_base_url=None,
                web_base_url=None,
                web_email=None,
                web_password=None,
                default_template_id=None,
                template_ids={},
            ),
            email=EmailConfig(
                enabled=False,
                sender_user_id="",
                templates_dir="/tmp/templates",
                internal_recipients=(),
                derek_email=None,
                approval_request_url_base=None,
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=300,
                dry_run=False,
            ),
            grievance_id=GrievanceIdConfig(mode="auto", timezone="America/New_York", min_width=3, separator=""),
        )

        graph = FakeGraph()
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=self.db,
            graph=graph,  # type: ignore[arg-type]
            logger=logging.getLogger("test"),
        )

        allocation = await allocator.allocate_and_reserve_folder(
            member_name="John Doe",
            correlation_id="test-correlation",
        )
        expected_id = f"{year}1001"
        self.assertEqual(allocation.grievance_id, expected_id)
        self.assertEqual(allocation.sequence, 1001)
        self.assertEqual(graph.last_reserved_id, expected_id)
        self.assertEqual(allocation.case_folder_name, f"{expected_id} John Doe")

    async def test_preview_returns_first_id_from_empty_state(self) -> None:
        year = current_year_in_timezone("America/New_York")

        class FakeGraph:
            def list_case_folder_names(self, **kwargs):  # noqa: ANN003
                _ = kwargs
                return []

        cfg = AppConfig(
            hmac_shared_secret="x",
            db_path=self.tmp.name,
            data_root="/tmp",
            docx_template_path="/tmp/template.docx",
            doc_templates={},
            libreoffice_timeout_seconds=45,
            graph=GraphConfig(
                tenant_id="tenant",
                client_id="client",
                cert_pem_path="/tmp/cert.pem",
                cert_thumbprint="thumb",
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                document_library="Documents",
                case_parent_folder="Grievances",
                generated_subfolder="Generated",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
                audit_backup_subfolders=(),
                audit_local_backup_roots=(),
                client_supplied_subfolder="Client supplied data",
                failed_processes_folder="config files/failed",
            ),
            docuseal=DocuSealConfig(
                base_url="http://docuseal",
                api_token="token",
                webhook_secret="secret",
                public_base_url=None,
                web_base_url=None,
                web_email=None,
                web_password=None,
                default_template_id=None,
                template_ids={},
            ),
            email=EmailConfig(
                enabled=False,
                sender_user_id="",
                templates_dir="/tmp/templates",
                internal_recipients=(),
                derek_email=None,
                approval_request_url_base=None,
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=300,
                dry_run=False,
            ),
            grievance_id=GrievanceIdConfig(mode="auto", timezone="America/New_York", min_width=3, separator=""),
        )
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=self.db,
            graph=FakeGraph(),  # type: ignore[arg-type]
            logger=logging.getLogger("test"),
        )

        preview = await allocator.preview_next_grievance_id()

        self.assertEqual(preview.grievance_id, f"{year}001")
        self.assertEqual(preview.sequence, 1)
        self.assertEqual(preview.sharepoint_max_seq, 0)
        self.assertEqual(preview.db_last_seq, 0)

    async def test_preview_uses_higher_db_sequence_without_reserving(self) -> None:
        year = current_year_in_timezone("America/New_York")

        class FakeGraph:
            def list_case_folder_names(self, **kwargs):  # noqa: ANN003
                _ = kwargs
                return [f"{year}004 Existing Person"]

        cfg = AppConfig(
            hmac_shared_secret="x",
            db_path=self.tmp.name,
            data_root="/tmp",
            docx_template_path="/tmp/template.docx",
            doc_templates={},
            libreoffice_timeout_seconds=45,
            graph=GraphConfig(
                tenant_id="tenant",
                client_id="client",
                cert_pem_path="/tmp/cert.pem",
                cert_thumbprint="thumb",
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                document_library="Documents",
                case_parent_folder="Grievances",
                generated_subfolder="Generated",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
                audit_backup_subfolders=(),
                audit_local_backup_roots=(),
                client_supplied_subfolder="Client supplied data",
                failed_processes_folder="config files/failed",
            ),
            docuseal=DocuSealConfig(
                base_url="http://docuseal",
                api_token="token",
                webhook_secret="secret",
                public_base_url=None,
                web_base_url=None,
                web_email=None,
                web_password=None,
                default_template_id=None,
                template_ids={},
            ),
            email=EmailConfig(
                enabled=False,
                sender_user_id="",
                templates_dir="/tmp/templates",
                internal_recipients=(),
                derek_email=None,
                approval_request_url_base=None,
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=300,
                dry_run=False,
            ),
            grievance_id=GrievanceIdConfig(mode="auto", timezone="America/New_York", min_width=3, separator=""),
        )
        await self.db.exec(
            """
            INSERT INTO grievance_id_sequences(year, last_seq, updated_at_utc)
            VALUES(?,?,?)
            """,
            (year, 8, "2026-04-13T00:00:00+00:00"),
        )
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=self.db,
            graph=FakeGraph(),  # type: ignore[arg-type]
            logger=logging.getLogger("test"),
        )

        preview = await allocator.preview_next_grievance_id()
        stored = await self.db.fetchone("SELECT last_seq FROM grievance_id_sequences WHERE year=?", (year,))

        self.assertEqual(preview.grievance_id, f"{year}009")
        self.assertEqual(preview.sequence, 9)
        self.assertEqual(preview.sharepoint_max_seq, 4)
        self.assertEqual(preview.db_last_seq, 8)
        self.assertEqual(int(stored[0]), 8)

    async def test_preview_respects_higher_sharepoint_sequence(self) -> None:
        year = current_year_in_timezone("America/New_York")

        class FakeGraph:
            def list_case_folder_names(self, **kwargs):  # noqa: ANN003
                _ = kwargs
                return [f"{year}015 Existing Person"]

        cfg = AppConfig(
            hmac_shared_secret="x",
            db_path=self.tmp.name,
            data_root="/tmp",
            docx_template_path="/tmp/template.docx",
            doc_templates={},
            libreoffice_timeout_seconds=45,
            graph=GraphConfig(
                tenant_id="tenant",
                client_id="client",
                cert_pem_path="/tmp/cert.pem",
                cert_thumbprint="thumb",
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                document_library="Documents",
                case_parent_folder="Grievances",
                generated_subfolder="Generated",
                signed_subfolder="Signed",
                audit_subfolder="Audit",
                audit_backup_subfolders=(),
                audit_local_backup_roots=(),
                client_supplied_subfolder="Client supplied data",
                failed_processes_folder="config files/failed",
            ),
            docuseal=DocuSealConfig(
                base_url="http://docuseal",
                api_token="token",
                webhook_secret="secret",
                public_base_url=None,
                web_base_url=None,
                web_email=None,
                web_password=None,
                default_template_id=None,
                template_ids={},
            ),
            email=EmailConfig(
                enabled=False,
                sender_user_id="",
                templates_dir="/tmp/templates",
                internal_recipients=(),
                derek_email=None,
                approval_request_url_base=None,
                allow_signer_copy_link=False,
                artifact_delivery_mode="sharepoint_link",
                max_attachment_bytes=2_000_000,
                resend_cooldown_seconds=300,
                dry_run=False,
            ),
            grievance_id=GrievanceIdConfig(mode="auto", timezone="America/New_York", min_width=3, separator=""),
        )
        await self.db.exec(
            """
            INSERT INTO grievance_id_sequences(year, last_seq, updated_at_utc)
            VALUES(?,?,?)
            """,
            (year, 8, "2026-04-13T00:00:00+00:00"),
        )
        allocator = GrievanceIdAllocator(
            cfg=cfg,
            db=self.db,
            graph=FakeGraph(),  # type: ignore[arg-type]
            logger=logging.getLogger("test"),
        )

        preview = await allocator.preview_next_grievance_id()

        self.assertEqual(preview.grievance_id, f"{year}016")
        self.assertEqual(preview.sequence, 16)
        self.assertEqual(preview.sharepoint_max_seq, 15)
        self.assertEqual(preview.db_last_seq, 8)


class IntakeModeValidationTests(unittest.TestCase):
    def test_auto_mode_rejects_incoming_grievance_id(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_grievance_input_mode("auto", "2026001")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_manual_mode_requires_grievance_id(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _validate_grievance_input_mode("manual", "")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_valid_modes_pass(self) -> None:
        _validate_grievance_input_mode("auto", "")
        _validate_grievance_input_mode("manual", "2026001")


if __name__ == "__main__":
    unittest.main()
