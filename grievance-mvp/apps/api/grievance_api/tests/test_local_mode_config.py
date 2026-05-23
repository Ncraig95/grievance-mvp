from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from grievance_api.core.config import load_config, resolve_config_path
from grievance_api.services.docuseal_client import DocuSealClient
from grievance_api.services.graph_mail import GraphMailer
from grievance_api.services.local_providers import LocalDocuSealClient, LocalGraphMailer, LocalSharePointUploader
from grievance_api.services.provider_factory import RuntimeProviders, assert_local_safe_providers, build_runtime_providers
from grievance_api.services.sharepoint_graph import GraphUploader


class LocalModeConfigTests(unittest.TestCase):
    def _write_config(self, *, overrides: dict | None = None) -> str:
        raw = {
            "db_path": "/tmp/prod.sqlite3",
            "data_root": "/tmp/prod-data",
            "docx_template_path": "/tmp/template.docx",
            "doc_templates": {"grievance_form": "/tmp/template.docx"},
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
                "base_url": "https://docuseal.example.invalid",
                "api_token": "token",
                "webhook_secret": "webhook-prod",
                "public_base_url": "https://docuseal.example.invalid",
                "default_template_id": 1,
                "template_ids": {},
            },
            "email": {
                "enabled": True,
                "sender_user_id": "sender@example.invalid",
                "templates_dir": "/tmp/templates",
                "internal_recipients": ["team@example.invalid"],
                "allow_signer_copy_link": True,
                "artifact_delivery_mode": "sharepoint_link",
                "max_attachment_bytes": 2_000_000,
                "resend_cooldown_seconds": 0,
                "dry_run": False,
            },
            "grievance_id": {
                "mode": "auto",
                "timezone": "America/New_York",
                "min_width": 3,
                "separator": "",
            },
            "intake_auth": {
                "shared_header_name": "X-Intake-Key",
                "shared_header_value": "",
                "cloudflare_access_client_id": "",
                "cloudflare_access_client_secret": "",
            },
        }
        if overrides:
            raw.update(overrides)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        try:
            yaml.safe_dump(raw, tmp)
            tmp.flush()
        finally:
            tmp.close()
        return tmp.name

    def test_production_is_default_when_app_mode_unset(self) -> None:
        path = self._write_config()
        try:
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(path)
        finally:
            os.unlink(path)

        self.assertEqual(cfg.app_mode, "production")
        self.assertEqual(cfg.db_path, "/tmp/prod.sqlite3")
        self.assertEqual(cfg.data_root, "/tmp/prod-data")
        self.assertEqual(cfg.docuseal.webhook_secret, "webhook-prod")

    def test_resolve_config_path_uses_app_config_path_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_config_path(), "/app/config/config.yaml")
        with patch.dict(os.environ, {"APP_CONFIG_PATH": "/tmp/local-config.yaml"}, clear=True):
            self.assertEqual(resolve_config_path(), "/tmp/local-config.yaml")

    def test_local_mode_accepts_fake_secret_overrides_and_paths(self) -> None:
        path = self._write_config(overrides={"app_mode": "production"})
        try:
            env = {
                "APP_MODE": "local",
                "LOCAL_DB_PATH": "/data/local-safe/test.sqlite3",
                "LOCAL_DATA_ROOT": "/data/local-safe/grievances",
                "LOCAL_HMAC_SHARED_SECRET": "fake-local-hmac",
                "LOCAL_DOCUSEAL_WEBHOOK_SECRET": "fake-local-webhook",
            }
            with patch.dict(os.environ, env, clear=True):
                cfg = load_config(path)
        finally:
            os.unlink(path)

        self.assertEqual(cfg.app_mode, "local")
        self.assertEqual(cfg.db_path, "/data/local-safe/test.sqlite3")
        self.assertEqual(cfg.data_root, "/data/local-safe/grievances")
        self.assertEqual(cfg.hmac_shared_secret, "fake-local-hmac")
        self.assertEqual(cfg.docuseal.webhook_secret, "fake-local-webhook")

    def test_local_mode_selects_only_local_providers(self) -> None:
        path = self._write_config(overrides={"app_mode": "local"})
        try:
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(path)
        finally:
            os.unlink(path)

        providers = build_runtime_providers(cfg)
        self.assertIsInstance(providers.docuseal, LocalDocuSealClient)
        self.assertIsInstance(providers.graph, LocalSharePointUploader)
        self.assertIsInstance(providers.mailer, LocalGraphMailer)

    def test_local_mode_guard_rejects_real_provider_instances(self) -> None:
        path = self._write_config(overrides={"app_mode": "local"})
        try:
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(path)
        finally:
            os.unlink(path)

        real = RuntimeProviders(
            docuseal=DocuSealClient("https://docuseal.example.invalid", "token"),
            graph=GraphUploader(
                tenant_id="tenant",
                client_id="client",
                cert_thumbprint="thumb",
                cert_pem_path="/tmp/cert.pem",
            ),
            mailer=GraphMailer(
                tenant_id="tenant",
                client_id="client",
                cert_thumbprint="thumb",
                cert_pem_path="/tmp/cert.pem",
                sender_user_id="sender@example.invalid",
            ),
            outreach_mailer=None,
        )
        with self.assertRaisesRegex(RuntimeError, "real outbound provider"):
            assert_local_safe_providers(cfg, real)

    def test_local_mode_guard_rejects_cloudflare_intake_auth(self) -> None:
        path = self._write_config(
            overrides={
                "app_mode": "local",
                "intake_auth": {
                    "shared_header_name": "X-Intake-Key",
                    "shared_header_value": "",
                    "cloudflare_access_client_id": "fake-client-id",
                    "cloudflare_access_client_secret": "fake-client-secret",
                },
            }
        )
        try:
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(path)
                with self.assertRaisesRegex(RuntimeError, "Cloudflare Access"):
                    build_runtime_providers(cfg)
        finally:
            os.unlink(path)
