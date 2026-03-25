from __future__ import annotations

import os
import tempfile
import unittest

import yaml

from grievance_api.core.config import load_config


class ConfigSignatureTableMapTests(unittest.TestCase):
    def _write_config(self, *, docuseal_overrides: dict) -> str:
        raw = {
            "db_path": "/tmp/grievances.sqlite3",
            "data_root": "/tmp/grievances",
            "docx_template_path": "/tmp/template.docx",
            "doc_templates": {},
            "graph": {
                "tenant_id": "tenant",
                "client_id": "client",
                "cert_pem_path": "/tmp/cert.pem",
                "cert_thumbprint": "thumb",
                "site_hostname": "contoso.sharepoint.com",
                "site_path": "/sites/Grievances",
                "document_library": "Documents",
                "case_parent_folder": "Grievances",
                "generated_subfolder": "Generated",
                "signed_subfolder": "Signed",
                "audit_subfolder": "Audit",
                "audit_backup_subfolders": [],
                "audit_local_backup_roots": [],
                "client_supplied_subfolder": "Client supplied data",
                "failed_processes_folder": "config files/failed",
            },
            "docuseal": {
                "base_url": "http://docuseal:3000",
                "api_token": "token",
                "webhook_secret": "secret",
                "default_template_id": 1,
                **docuseal_overrides,
            },
            "email": {
                "enabled": False,
                "sender_user_id": "",
                "templates_dir": "/tmp/templates",
                "internal_recipients": [],
                "allow_signer_copy_link": False,
                "artifact_delivery_mode": "sharepoint_link",
                "max_attachment_bytes": 2_000_000,
                "resend_cooldown_seconds": 300,
                "dry_run": False,
            },
            "grievance_id": {
                "mode": "auto",
                "timezone": "America/New_York",
                "min_width": 3,
                "separator": "",
            },
        }
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            tmp.write(yaml.safe_dump(raw))
            tmp.flush()
        finally:
            tmp.close()
        return tmp.name

    def test_load_config_parses_valid_signature_table_map(self) -> None:
        path = self._write_config(
            docuseal_overrides={
                "signature_table_guard_enabled": True,
                "signature_table_guard_tolerance": 0.02,
                "signature_table_guard_min_gap": 0.01,
                "submitters_order": "random",
                "submitters_order_by_form": {
                    "settlement_form_3106": "random",
                    "statement_of_occurrence": "preserved",
                },
                "signature_table_maps": {
                    "settlement_form_3106": {
                        "cells": {
                            "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 0.30, "h": 0.05},
                            "signer1_date": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                        }
                    }
                }
            }
        )
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)

        self.assertIn("settlement_form_3106", cfg.docuseal.signature_table_maps)
        self.assertTrue(cfg.docuseal.signature_table_guard_enabled)
        self.assertAlmostEqual(cfg.docuseal.signature_table_guard_tolerance, 0.02)
        self.assertAlmostEqual(cfg.docuseal.signature_table_guard_min_gap, 0.01)
        self.assertEqual(cfg.docuseal.submitters_order, "random")
        self.assertEqual(cfg.docuseal.submitters_order_by_form.get("settlement_form_3106"), "random")
        self.assertEqual(cfg.docuseal.submitters_order_by_form.get("statement_of_occurrence"), "preserved")
        parsed = cfg.docuseal.signature_table_maps["settlement_form_3106"]
        self.assertIn("signer1_signature", parsed.cells)
        self.assertAlmostEqual(parsed.cells["signer1_signature"].x, 0.40)
        self.assertEqual(parsed.cells["signer1_signature"].page, 0)

    def test_load_config_ignores_malformed_signature_table_map(self) -> None:
        path = self._write_config(
            docuseal_overrides={
                "signature_table_maps": {
                    "settlement_form_3106": {
                        "cells": {
                            "signer1_signature": {"page": 0, "x": 0.40, "y": 0.67, "w": 1.30, "h": 0.05},
                            "invalid_cell_name": {"page": 0, "x": 0.72, "y": 0.67, "w": 0.20, "h": 0.05},
                        }
                    }
                }
            }
        )
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)

        self.assertNotIn("settlement_form_3106", cfg.docuseal.signature_table_maps)
        self.assertTrue(cfg.docuseal.signature_table_guard_enabled)
        self.assertAlmostEqual(cfg.docuseal.signature_table_guard_tolerance, 0.015)
        self.assertAlmostEqual(cfg.docuseal.signature_table_guard_min_gap, 0.005)
        self.assertEqual(cfg.docuseal.submitters_order, "preserved")
        self.assertEqual(cfg.docuseal.submitters_order_by_form, {})

    def test_statement_forms_can_be_pinned_to_generic_no_trace_layout(self) -> None:
        path = self._write_config(
            docuseal_overrides={
                "signature_layout_mode_by_form": {
                    "statement_of_occurrence": "generic",
                    "grievance_form": "generic",
                },
                "signature_table_trace_by_form": {
                    "statement_of_occurrence": False,
                    "grievance_form": False,
                },
            }
        )
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)

        self.assertEqual(cfg.docuseal.signature_layout_mode_by_form.get("statement_of_occurrence"), "generic")
        self.assertEqual(cfg.docuseal.signature_layout_mode_by_form.get("grievance_form"), "generic")
        self.assertFalse(cfg.docuseal.signature_table_trace_by_form.get("statement_of_occurrence"))
        self.assertFalse(cfg.docuseal.signature_table_trace_by_form.get("grievance_form"))


if __name__ == "__main__":
    unittest.main()
