from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from grievance_api.services.audit_backups import (
    AuditBackupFailure,
    AuditBackupOutcome,
    SharePointAuditCopy,
    fanout_audit_backups,
    merge_backup_locations_json,
)
from grievance_api.services.sharepoint_graph import UploadedFileRef


class _FakeGraph:
    def __init__(self, fail_subfolders: set[str] | None = None) -> None:
        self.fail_subfolders = fail_subfolders or set()

    def upload_to_case_subfolder(self, **kwargs):  # noqa: ANN003
        subfolder = str(kwargs["subfolder"])
        if subfolder in self.fail_subfolders:
            raise RuntimeError(f"upload failed for {subfolder}")
        filename = str(kwargs["filename"])
        path = (
            f"{kwargs['case_parent_folder']}/{kwargs['case_folder_name']}/{subfolder}/{filename}".replace("//", "/")
        )
        return UploadedFileRef(
            drive_id="drive",
            item_id=f"item-{subfolder}",
            web_url=f"https://example.test/{subfolder}/{filename}",
            path=path,
        )


class AuditBackupTests(unittest.TestCase):
    def test_fanout_uploads_primary_extra_and_local_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            outcome = fanout_audit_backups(
                graph=_FakeGraph(),
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/Grievances",
                library="Documents",
                case_parent_folder="Grievances",
                case_folder_name="2026001 John Doe",
                primary_subfolder="Audit",
                extra_subfolders=("Audit Backup", "Audit Backup"),
                local_backup_roots=(tmp_dir,),
                filename="statement_audit.zip",
                file_bytes=b"zip-bytes",
            )

            self.assertEqual(len(outcome.sharepoint_copies), 2)
            self.assertEqual(outcome.primary_web_url, "https://example.test/Audit/statement_audit.zip")
            self.assertEqual(len(outcome.local_paths), 1)
            self.assertEqual(len(outcome.failures), 0)

            local_file = Path(outcome.local_paths[0])
            self.assertTrue(local_file.exists())
            self.assertEqual(local_file.read_bytes(), b"zip-bytes")

    def test_primary_url_falls_back_to_secondary_copy_when_primary_fails(self) -> None:
        outcome = fanout_audit_backups(
            graph=_FakeGraph(fail_subfolders={"Audit"}),
            site_hostname="contoso.sharepoint.com",
            site_path="/sites/Grievances",
            library="Documents",
            case_parent_folder="Grievances",
            case_folder_name="2026001 John Doe",
            primary_subfolder="Audit",
            extra_subfolders=("Audit Backup",),
            local_backup_roots=(),
            filename="statement_audit.zip",
            file_bytes=b"zip-bytes",
        )

        self.assertEqual(outcome.primary_web_url, "https://example.test/Audit Backup/statement_audit.zip")
        self.assertEqual(len(outcome.sharepoint_copies), 1)
        self.assertEqual(len(outcome.failures), 1)
        self.assertEqual(outcome.failures[0].destination, "sharepoint:Audit")

    def test_merge_backup_locations_json_dedupes(self) -> None:
        existing = json.dumps(
            {
                "sharepoint": [{"subfolder": "Audit", "path": "Grievances/Case/Audit/file.zip", "web_url": "u1"}],
                "local": ["/mnt/audit/file.zip"],
            }
        )
        outcome = AuditBackupOutcome(
            primary_web_url="u2",
            sharepoint_copies=(
                SharePointAuditCopy(subfolder="Audit", path="Grievances/Case/Audit/file.zip", web_url="u1"),
                SharePointAuditCopy(subfolder="Audit Backup", path="Grievances/Case/Audit Backup/file.zip", web_url="u2"),
            ),
            local_paths=("/mnt/audit/file.zip", "/mnt/audit2/file.zip"),
            failures=(AuditBackupFailure(destination="local:/mnt/bad", error="disk full"),),
        )

        merged_json = merge_backup_locations_json(existing, outcome)
        self.assertIsNotNone(merged_json)
        parsed = json.loads(str(merged_json))
        self.assertEqual(len(parsed["sharepoint"]), 2)
        self.assertEqual(len(parsed["local"]), 2)
        self.assertIn("/mnt/audit2/file.zip", parsed["local"])


if __name__ == "__main__":
    unittest.main()
