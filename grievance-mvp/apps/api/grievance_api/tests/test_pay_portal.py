from __future__ import annotations

import io
import tempfile
import time
import unittest
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services import pay_portal
from grievance_api.services.pay_portal import (
    PayActor,
    approve_irs_rate_candidate,
    calculate_commission_compensation,
    calculate_president_differential,
    create_mileage_attachment,
    create_revision,
    handle_pay_docuseal_completion,
    list_irs_rate_candidates,
    lock_period_and_send_packet,
    parse_irs_mileage_rate_candidates,
    pay_settings,
    pay_packet_signer_order,
    store_attachment,
    store_compensation_stub,
    sync_irs_mileage_rate_candidates,
    upsert_entry,
    upsert_wage_scale,
    validate_receipt_file,
)
from grievance_api.web.routes_pay import approve_pay_irs_rate, pay_page, pay_start_page, pay_view_page


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def upload_local_file_to_folder_path(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            web_url=f"https://sharepoint.local/{kwargs['folder_path']}/{kwargs['filename']}",
            path=f"{kwargs['folder_path']}/{kwargs['filename']}",
        )


class _FakeDocuSeal:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.download_calls: list[str] = []

    def create_submission(self, **kwargs):  # noqa: ANN003
        self.create_calls.append(dict(kwargs))
        return SimpleNamespace(
            submission_id="pay-ds-1",
            signing_link="https://docuseal.local/sign/pay-ds-1",
            raw={},
        )

    def extract_signing_links_by_email(self, raw):  # noqa: ANN001
        _ = raw
        if not self.create_calls:
            return {}
        return {
            str(email).lower(): f"https://docuseal.local/sign/{index}"
            for index, email in enumerate(self.create_calls[-1].get("signers", []), start=1)
        }

    def fetch_signing_links_by_email(self, submission_id: str):
        _ = submission_id
        return {}

    def download_completed_artifacts(self, *, submission_id: str):
        self.download_calls.append(submission_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("signed.pdf", b"%PDF-1.4\nsigned packet\n")
        return {"completed_zip_bytes": buf.getvalue(), "submission": {}}


class _FakeMailer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_mail(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return SimpleNamespace(graph_message_id="mail-1", internet_message_id=None)


class _FakeHttpResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _Request:
    def __init__(self, *, state, session: dict | None = None) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=state)
        self.session = session if session is not None else {}
        self.headers = {"host": "127.0.0.1"}
        self.client = SimpleNamespace(host="127.0.0.1")


def _fake_docx_to_pdf(docx_path: str, out_dir: str, timeout_seconds: int, **kwargs) -> str:  # noqa: ANN001, ANN003
    _ = timeout_seconds
    _ = kwargs
    out = Path(out_dir) / f"{Path(docx_path).stem}.pdf"
    out.write_bytes(f"PDF:{Path(docx_path).name}\n".encode("utf-8"))
    return str(out)


def _fake_fill_docx(**kwargs) -> None:  # noqa: ANN003
    Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(kwargs["output_path"]).write_text(
        "anchor" if kwargs.get("include_signature_placeholders") else "voucher",
        encoding="utf-8",
    )


def _fake_merge(input_paths: list[str], output_path: str) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    payload = "|".join(Path(path).name for path in input_paths)
    Path(output_path).write_bytes(payload.encode("utf-8"))


class PayPortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_tmpdir)
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        self.data_root = str(Path(self.tmpdir.name) / "data")
        migrate(self.db_path)
        self.db = Db(self.db_path)

    async def _cleanup_tmpdir(self) -> None:
        self.tmpdir.cleanup()

    def _cfg(self):
        return SimpleNamespace(
            data_root=self.data_root,
            libreoffice_timeout_seconds=45,
            docx_pdf_engine="libreoffice",
            docx_pdf_graph_temp_folder="_docx_pdf_convert",
            pay_portal=SimpleNamespace(
                enabled=True,
                voucher_template_path=str(Path(self.tmpdir.name) / "voucher.docx"),
                sharepoint_root_folder="Pay Portal",
                receipt_max_file_bytes=1_000_000,
                receipt_max_entry_bytes=5_000_000,
                clamav_host="clamav",
                clamav_port=3310,
                clamav_timeout_seconds=30,
                president_email="",
                treasurer_emails=(),
                president_target_scale="36",
                president_target_multiplier=1.20,
                google_maps_api_key="",
                irs_rates={"2025": "0.70", "2026": "0.725"},
                irs_rate_sync_enabled=True,
                irs_rate_source_urls=("https://www.irs.gov/example",),
                pay_users=(),
            ),
            officer_auth=SimpleNamespace(enabled=False),
            external_steward_auth=SimpleNamespace(enabled=False),
            graph=SimpleNamespace(
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/local",
                document_library="Documents",
            ),
            docuseal=SimpleNamespace(default_template_id=1, template_ids={}, strict_template_ids=False),
            email=SimpleNamespace(
                enabled=True,
                internal_recipients=("treasurer@example.org",),
                max_attachment_bytes=2_000_000,
            ),
        )

    async def _open_period_and_entry(self, *, actor: PayActor | None = None) -> tuple[str, str]:
        actor = actor or PayActor(
            email="officer@example.org",
            display_name="Officer Example",
            role="officer",
            can_view_all=True,
            can_edit_all=False,
            can_lock=False,
        )
        period = await pay_portal.ensure_pay_period(self.db)
        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": str(period["period_start"]),
                "display_name": actor.display_name,
                "hourly_rate": 45,
                "lost_wage_input_type": "hourly",
                "lost_wage_amount": 45,
                "hours": 8,
                "local_number": "3106",
                "notes": "meeting",
            },
            pay_cfg=self._cfg().pay_portal,
        )
        return str(period["id"]), str(entry["id"])

    async def test_pay_settings_falls_back_to_configured_mileage_defaults(self) -> None:
        cfg = SimpleNamespace(
            president_email="president@example.org",
            treasurer_emails=("treasurer@example.org",),
            irs_rates={"2024": "0.67", "2025": "0.70", "2026": "0.725"},
            common_places=(
                {"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL 32205, USA"},
            ),
        )
        settings = await pay_portal.pay_settings(self.db, pay_cfg=cfg)
        self.assertEqual(settings["president_email"], "president@example.org")
        self.assertEqual(settings["treasurer_emails"], ["treasurer@example.org"])
        self.assertEqual(settings["irs_rates"], {"2024": "0.67", "2025": "0.70", "2026": "0.725"})
        self.assertEqual(
            settings["common_places"],
            [{"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL 32205, USA"}],
        )

    async def test_mileage_attachment_updates_entry_totals_and_filename(self) -> None:
        period_id, entry_id = await self._open_period_and_entry()
        actor = PayActor(
            email="officer@example.org",
            display_name="Officer Example",
            role="officer",
            can_view_all=True,
            can_edit_all=False,
            can_lock=False,
        )

        with patch(
            "grievance_api.services.pay_portal.build_mileage_pdf",
            return_value=(b"%PDF-1.4\nmileage\n", pay_portal.Decimal("10.88"), pay_portal.Decimal("15.00")),
        ) as build_pdf:
            result = await create_mileage_attachment(
                db=self.db,
                cfg=self._cfg(),
                period_id=period_id,
                entry_id=entry_id,
                actor=actor,
                name="test test",
                local_number="3106",
                date_str="2026-05-09",
                description="Union business",
                locations=["Union Hall", "Worksite"],
                rate_text="0.725",
            )

        self.assertEqual(result["filename"], "20260509 test test.pdf")
        self.assertEqual(result["attachment_type"], "mileage_pdf")
        self.assertEqual(result["mileage_miles"], 15.0)
        self.assertEqual(result["reimbursement"], 10.88)
        build_pdf.assert_called_once()
        row = await self.db.fetchone(
            "SELECT mileage_miles, mileage_rate, mileage_amount FROM pay_entries WHERE id=?",
            (entry_id,),
        )
        self.assertEqual(row[0], 15.0)
        self.assertEqual(row[1], 0.725)
        self.assertEqual(row[2], 10.88)

    async def test_public_pay_start_page_has_login_links_without_portal_data(self) -> None:
        response = await pay_start_page(_Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db)))
        html = response.body.decode("utf-8")

        self.assertIn("Lost Wage Portal", html)
        self.assertIn("CWA Local 3106", html)
        self.assertIn("Private access required", html)
        self.assertIn("Officers and admins", html)
        self.assertIn("Approved pay users", html)
        self.assertIn('href="/auth/login?next=/pay"', html)
        self.assertIn('href="/auth/steward/login?next=/pay"', html)
        self.assertIn('href="/pay/start" aria-current="page"', html)
        self.assertNotIn("entryForm", html)
        self.assertNotIn("/pay/api/context", html)

    async def test_pay_page_redirects_unauthenticated_users_to_public_start(self) -> None:
        response = await pay_page(_Request(state=SimpleNamespace(cfg=self._cfg(), db=self.db)))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/pay/start")

    async def test_authenticated_pay_home_redirects_by_role(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True

        officer_response = await pay_page(
            _Request(
                state=SimpleNamespace(cfg=cfg, db=self.db),
                session={
                    "officer_user": {
                        "email": "officer@example.org",
                        "display_name": "Officer",
                        "role": "officer",
                        "contract_scopes": [],
                        "exp": int(time.time()) + 3600,
                    }
                },
            )
        )
        self.assertEqual(officer_response.headers["location"], "/pay/entry")

        admin_response = await pay_page(
            _Request(
                state=SimpleNamespace(cfg=cfg, db=self.db),
                session={
                    "officer_user": {
                        "email": "admin@example.org",
                        "display_name": "Admin",
                        "role": "admin",
                        "contract_scopes": [],
                        "exp": int(time.time()) + 3600,
                    }
                },
            )
        )
        self.assertEqual(admin_response.headers["location"], "/pay/treasurer")

    async def test_pay_workspace_pages_are_role_focused(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        officer_request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session={
                "officer_user": {
                    "email": "officer@example.org",
                    "display_name": "Officer",
                    "role": "officer",
                    "contract_scopes": [],
                    "exp": int(time.time()) + 3600,
                }
            },
        )

        entry_html = (await pay_view_page("entry", officer_request)).body.decode("utf-8")
        self.assertIn("First-Time Wage Setup", entry_html)
        self.assertIn('id="entryForm"', entry_html)
        self.assertIn('href="/pay/mileage"', entry_html)
        self.assertIn('href="/pay/president"', entry_html)
        self.assertIn('href="/pay/treasurer"', entry_html)
        self.assertNotIn("Pay User Allowlist", entry_html)

        mileage_html = (await pay_view_page("mileage", officer_request)).body.decode("utf-8")
        self.assertIn("Mileage Tracker", mileage_html)
        self.assertIn('id="mileageForm"', mileage_html)
        self.assertIn('id="commonPlaceSelect"', mileage_html)
        self.assertIn('id="locations"', mileage_html)
        self.assertIn("Add Location", mileage_html)
        self.assertIn("Generate Mileage PDF", mileage_html)
        self.assertIn("/pay/api/entries/${entryId}/mileage", mileage_html)
        self.assertNotIn("Pay User Allowlist", mileage_html)

        treasurer_html = (await pay_view_page("treasurer", officer_request)).body.decode("utf-8")
        self.assertIn("Review, Lock, and Send", treasurer_html)
        self.assertIn("Voucher Packet", treasurer_html)
        self.assertIn("Read-only review access", treasurer_html)
        self.assertNotIn("Lock And Send For Signature", treasurer_html)
        self.assertNotIn("First-Time Wage Setup", treasurer_html)
        self.assertNotIn("Pay User Allowlist", treasurer_html)

        with self.assertRaises(HTTPException) as denied:
            await pay_view_page("admin", officer_request)
        self.assertEqual(denied.exception.status_code, 403)

        admin_request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session={
                "officer_user": {
                    "email": "admin@example.org",
                    "display_name": "Admin",
                    "role": "admin",
                    "contract_scopes": [],
                    "exp": int(time.time()) + 3600,
                }
            },
        )
        admin_html = (await pay_view_page("admin", admin_request)).body.decode("utf-8")
        self.assertIn("Pay User Allowlist", admin_html)
        self.assertIn("IRS Rate Updates", admin_html)
        self.assertNotIn('id="entryForm"', admin_html)

    async def test_irs_rate_parser_detects_official_business_rate(self) -> None:
        candidates = parse_irs_mileage_rate_candidates(
            content="""
              <title>IRS sets 2027 business standard mileage rate at 75.5 cents per mile</title>
              <p>Beginning Jan. 1, 2027, the standard mileage rates are:
              75.5 cents per mile driven for business use.</p>
            """,
            source_url="https://www.irs.gov/newsroom/example",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].rate_year, "2027")
        self.assertEqual(candidates[0].effective_date, "2027-01-01")
        self.assertEqual(candidates[0].rate_per_mile, pay_portal.Decimal("0.755"))

    async def test_irs_sync_stages_new_rate_and_ignores_active_rate(self) -> None:
        cfg = self._cfg().pay_portal

        active = await sync_irs_mileage_rate_candidates(
            self.db,
            pay_cfg=cfg,
            http_get=lambda _url, timeout=15: _FakeHttpResponse(
                "<p>Beginning Jan. 1, 2026, 72.5 cents per mile driven for business use.</p>"
            ),
            source_urls=("https://www.irs.gov/active",),
        )
        self.assertEqual(active["detected"], [])
        self.assertEqual(active["skipped_existing"], 1)

        detected = await sync_irs_mileage_rate_candidates(
            self.db,
            pay_cfg=cfg,
            http_get=lambda _url, timeout=15: _FakeHttpResponse(
                "<p>Beginning Jan. 1, 2027, 75.5 cents per mile driven for business use.</p>"
            ),
            source_urls=("https://www.irs.gov/new",),
        )
        self.assertEqual(len(detected["detected"]), 1)
        self.assertEqual(detected["detected"][0]["rate_year"], "2027")
        self.assertEqual(detected["detected"][0]["rate_per_mile"], "0.755")

        rows = await list_irs_rate_candidates(self.db, status="pending")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")

    async def test_irs_sync_failure_leaves_active_rates_unchanged(self) -> None:
        cfg = self._cfg().pay_portal

        result = await sync_irs_mileage_rate_candidates(
            self.db,
            pay_cfg=cfg,
            http_get=lambda _url, timeout=15: _FakeHttpResponse("<p>No mileage data here.</p>"),
            source_urls=("https://www.irs.gov/malformed",),
        )
        settings = await pay_settings(self.db, pay_cfg=cfg)

        self.assertEqual(result["detected"], [])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(settings["irs_rates"]["2026"], "0.725")

    async def test_irs_rate_approval_updates_active_settings(self) -> None:
        cfg = self._cfg().pay_portal
        result = await sync_irs_mileage_rate_candidates(
            self.db,
            pay_cfg=cfg,
            http_get=lambda _url, timeout=15: _FakeHttpResponse(
                "<p>Beginning Jan. 1, 2027, 75.5 cents per mile driven for business use.</p>"
            ),
            source_urls=("https://www.irs.gov/new",),
        )
        candidate_id = int(result["detected"][0]["id"])

        approved = await approve_irs_rate_candidate(
            self.db,
            candidate_id=candidate_id,
            actor="treasurer@example.org",
            pay_cfg=cfg,
        )
        settings = await pay_settings(self.db, pay_cfg=cfg)

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], "treasurer@example.org")
        self.assertEqual(settings["irs_rates"]["2027"], "0.755")

    async def test_non_treasurer_route_cannot_approve_irs_rate_candidate(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session={
                "officer_user": {
                    "email": "officer@example.org",
                    "display_name": "Officer",
                    "role": "officer",
                    "contract_scopes": [],
                    "exp": int(time.time()) + 3600,
                }
            },
        )

        with self.assertRaises(HTTPException) as exc:
            await approve_pay_irs_rate(1, request)

        self.assertEqual(exc.exception.status_code, 403)

    async def test_president_differential_uses_effective_wage_scale_and_basis(self) -> None:
        await upsert_wage_scale(
            self.db,
            effective_date="2026-01-01",
            weekly_basis_hours=40,
            target_weekly_amount=2065.50,
            actual_weekly_amount=None,
            target_multiplier=1.20,
            target_scale="36",
            actual_scale="base",
            notes=None,
            updated_by="test",
        )
        result = await calculate_president_differential(
            self.db,
            entry_date="2026-05-10",
            weekly_basis_hours=40,
            president_diff_hours=10,
            target_scale="36",
            target_multiplier=1.20,
            lost_wage_input_type="hourly",
            lost_wage_amount=45,
        )
        self.assertEqual(result.diff_rate, pay_portal.Decimal("16.97"))
        self.assertEqual(result.diff_amount, pay_portal.Decimal("169.70"))
        self.assertEqual(result.lost_wage_hourly_rate, pay_portal.Decimal("45.00"))

        await upsert_wage_scale(
            self.db,
            effective_date="2026-01-01",
            weekly_basis_hours=37.5,
            target_weekly_amount=2065.50,
            actual_weekly_amount=None,
            target_multiplier=1.20,
            target_scale="36",
            actual_scale="base",
            notes=None,
            updated_by="test",
        )
        result_37 = await calculate_president_differential(
            self.db,
            entry_date="2026-05-10",
            weekly_basis_hours=37.5,
            president_diff_hours=2,
            target_scale="36",
            target_multiplier=1.20,
            lost_wage_input_type="weekly",
            lost_wage_amount=1900.00,
        )
        self.assertEqual(result_37.diff_amount, pay_portal.Decimal("30.86"))
        self.assertEqual(result_37.lost_wage_hourly_rate, pay_portal.Decimal("50.67"))

    async def test_president_differential_zero_floors_when_actual_exceeds_target(self) -> None:
        await upsert_wage_scale(
            self.db,
            effective_date="2026-01-01",
            weekly_basis_hours=40,
            target_weekly_amount=1000,
            actual_weekly_amount=None,
            target_multiplier=1.20,
            target_scale="36",
            actual_scale="base",
            notes=None,
            updated_by="test",
        )
        result = await calculate_president_differential(
            self.db,
            entry_date="2026-02-01",
            weekly_basis_hours=40,
            president_diff_hours=8,
            target_scale="36",
            target_multiplier=1.20,
            lost_wage_input_type="hourly",
            lost_wage_amount=50,
        )
        self.assertEqual(result.diff_rate, pay_portal.Decimal("0.00"))
        self.assertEqual(result.diff_amount, pay_portal.Decimal("0.00"))

    async def test_entry_accepts_weekly_lost_wage_input(self) -> None:
        await upsert_wage_scale(
            self.db,
            effective_date="2026-01-01",
            weekly_basis_hours=40,
            target_weekly_amount=2065.50,
            actual_weekly_amount=None,
            target_multiplier=1.20,
            target_scale="36",
            actual_scale="base",
            notes=None,
            updated_by="test",
        )
        period = await pay_portal.ensure_pay_period(self.db, for_date=date.fromisoformat("2026-05-10"))
        actor = PayActor("president@example.org", "President", "officer", True, False, False)
        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": "2026-05-10",
                "display_name": "President",
                "lost_wage_input_type": "weekly",
                "lost_wage_amount": 1800,
                "weekly_basis_hours": 40,
                "president_diff_hours": 1,
            },
            pay_cfg=self._cfg().pay_portal,
        )
        self.assertEqual(entry["lost_wage_input_type"], "weekly")
        self.assertEqual(entry["lost_wage_amount"], 1800.0)
        self.assertEqual(entry["lost_wage_hourly_rate"], 45.0)
        self.assertEqual(entry["hourly_rate"], 45.0)
        self.assertEqual(entry["president_diff_amount"], 16.97)

    async def test_commission_compensation_uses_three_month_average_over_160(self) -> None:
        result = calculate_commission_compensation(
            base_wage_input_type="hourly",
            base_wage_amount=45,
            weekly_basis_hours=40,
            commission_month_1_amount=1000,
            commission_month_2_amount=1400,
            commission_month_3_amount=1800,
        )
        self.assertEqual(result.commission_average_monthly, pay_portal.Decimal("1400.00"))
        self.assertEqual(result.commission_hourly_rate, pay_portal.Decimal("8.75"))
        self.assertEqual(result.calculated_hourly_rate, pay_portal.Decimal("53.75"))

    async def test_entry_can_use_saved_commission_lost_wage_profile(self) -> None:
        await upsert_wage_scale(
            self.db,
            effective_date="2026-01-01",
            weekly_basis_hours=40,
            target_weekly_amount=2065.50,
            actual_weekly_amount=None,
            target_multiplier=1.20,
            target_scale="36",
            actual_scale="base",
            notes=None,
            updated_by="test",
        )
        actor = PayActor("president@example.org", "President", "officer", True, False, False)
        with patch("grievance_api.services.pay_portal.scan_with_clamav", return_value="stream: OK"):
            stub = await store_compensation_stub(
                self.db,
                cfg=self._cfg(),
                actor=actor,
                user_email=None,
                base_wage_input_type="hourly",
                base_wage_amount=45,
                weekly_basis_hours=40,
                commission_month_1_amount=1000,
                commission_month_2_amount=1400,
                commission_month_3_amount=1800,
                filename="pay-stub.pdf",
                content_type="application/pdf",
                content=b"%PDF-1.4\nstub\n%%EOF",
            )
        self.assertEqual(stub["calculated_hourly_rate"], 53.75)

        period = await pay_portal.ensure_pay_period(self.db, for_date=date.fromisoformat("2026-05-10"))
        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": "2026-05-10",
                "display_name": "President",
                "lost_wage_input_type": "profile",
                "weekly_basis_hours": 40,
                "president_diff_hours": 2,
            },
            pay_cfg=self._cfg().pay_portal,
        )
        self.assertEqual(entry["lost_wage_input_type"], "profile")
        self.assertEqual(entry["compensation_stub_id"], stub["id"])
        self.assertEqual(entry["lost_wage_hourly_rate"], 53.75)
        self.assertEqual(entry["president_diff_rate"], 8.22)
        self.assertEqual(entry["president_diff_amount"], 16.44)

    async def test_guest_cannot_upload_compensation_stub_for_another_member(self) -> None:
        actor = PayActor("guest@example.org", "Guest", "guest", False, False, False, is_guest=True)
        with self.assertRaises(PermissionError):
            await store_compensation_stub(
                self.db,
                cfg=self._cfg(),
                actor=actor,
                user_email="other@example.org",
                base_wage_input_type="hourly",
                base_wage_amount=45,
                weekly_basis_hours=40,
                commission_month_1_amount=0,
                commission_month_2_amount=0,
                commission_month_3_amount=0,
                filename="pay-stub.pdf",
                content_type="application/pdf",
                content=b"%PDF-1.4\nstub\n%%EOF",
                scan=False,
            )

    async def test_pay_packet_signer_order_puts_filers_before_president(self) -> None:
        signers, president_index = pay_packet_signer_order(
            grouped_entry_emails=["member@example.org", "President@Example.Org", "other@example.org"],
            president_signer_email="president@example.org",
        )
        self.assertEqual(signers, ["member@example.org", "other@example.org", "president@example.org"])
        self.assertEqual(president_index, 3)

    async def test_receipt_validation_blocks_bad_types_and_infected_scan(self) -> None:
        with self.assertRaisesRegex(ValueError, "only PDF"):
            validate_receipt_file(
                filename="receipt.txt",
                content_type="text/plain",
                content=b"hello",
                max_file_bytes=1000,
            )

        period_id, entry_id = await self._open_period_and_entry()
        actor = PayActor("officer@example.org", "Officer Example", "officer", True, False, False)
        with patch("grievance_api.services.pay_portal.scan_with_clamav", side_effect=ValueError("FOUND")):
            with self.assertRaisesRegex(ValueError, "FOUND"):
                await store_attachment(
                    self.db,
                    cfg=self._cfg(),
                    period_id=period_id,
                    entry_id=entry_id,
                    actor=actor,
                    attachment_type="receipt",
                    filename="receipt.pdf",
                    content_type="application/pdf",
                    content=b"%PDF-1.4\n%%EOF",
                )

        with patch("grievance_api.services.pay_portal.scan_with_clamav", return_value="stream: OK"):
            saved = await store_attachment(
                self.db,
                cfg=self._cfg(),
                period_id=period_id,
                entry_id=entry_id,
                actor=actor,
                attachment_type="receipt",
                filename="receipt.pdf",
                content_type="application/pdf",
                content=b"%PDF-1.4\n%%EOF",
            )
        self.assertEqual(saved["scan_status"], "clean")

    async def test_guest_cannot_attach_to_another_users_entry(self) -> None:
        owner = PayActor("owner@example.org", "Owner", "guest", False, False, False, is_guest=True)
        period_id, entry_id = await self._open_period_and_entry(actor=owner)
        guest = PayActor("guest@example.org", "Guest", "guest", False, False, False, is_guest=True)
        with patch("grievance_api.services.pay_portal.scan_with_clamav", return_value="stream: OK"):
            with self.assertRaises(PermissionError):
                await store_attachment(
                    self.db,
                    cfg=self._cfg(),
                    period_id=period_id,
                    entry_id=entry_id,
                    actor=guest,
                    attachment_type="receipt",
                    filename="receipt.pdf",
                    content_type="application/pdf",
                    content=b"%PDF-1.4\n%%EOF",
                )

    async def test_lock_period_builds_voucher_first_packet_and_sends_docuseal(self) -> None:
        cfg = self._cfg()
        await pay_portal.save_pay_settings(
            self.db,
            setting={"president_email": "president@example.org", "treasurer_emails": ["treasurer@example.org"]},
            updated_by="test",
        )
        actor = PayActor("treasurer@example.org", "Treasurer", "treasurer", True, True, True)
        period_id, _ = await self._open_period_and_entry(actor=actor)
        graph = _FakeGraph()
        docuseal = _FakeDocuSeal()
        with patch("grievance_api.services.pay_portal.fill_pay_voucher_docx", _fake_fill_docx), patch(
            "grievance_api.services.pay_portal.merge_pdfs",
            _fake_merge,
        ):
            result = await lock_period_and_send_packet(
                db=self.db,
                cfg=cfg,
                graph=graph,
                docuseal=docuseal,
                period_id=period_id,
                actor=actor,
                president_signer_email=None,
                docx_to_pdf_func=_fake_docx_to_pdf,
        )

        self.assertEqual(result["status"], "awaiting_signature")
        self.assertEqual(docuseal.create_calls[0]["signers"], ["treasurer@example.org", "president@example.org"])
        self.assertEqual(result["signer_order"], ["treasurer@example.org", "president@example.org"])
        self.assertEqual(result["signing_link"], "https://docuseal.local/sign/1")
        self.assertEqual(docuseal.create_calls[0]["metadata"]["president_signer_email"], "president@example.org")
        self.assertIn(b".pdf", docuseal.create_calls[0]["pdf_bytes"])
        packet_row = await self.db.fetchone("SELECT status, docuseal_submission_id FROM pay_packets WHERE period_id=?", (period_id,))
        self.assertEqual(packet_row[0], "awaiting_signature")
        self.assertEqual(packet_row[1], "pay-ds-1")

    async def test_pay_docuseal_completion_files_signed_packet_and_notifies_treasurer(self) -> None:
        cfg = self._cfg()
        period_id, _ = await self._open_period_and_entry()
        packet_dir = Path(self.data_root) / "pay" / period_id / "packet" / "packet-1"
        packet_dir.mkdir(parents=True)
        unsigned = packet_dir / "unsigned.pdf"
        unsigned.write_bytes(b"%PDF-1.4\nunsigned\n")
        await self.db.exec(
            "UPDATE pay_periods SET status='awaiting_signature', sharepoint_folder_path='Pay Portal/2026/test' WHERE id=?",
            (period_id,),
        )
        await self.db.exec(
            """INSERT INTO pay_packets(
                 id, period_id, revision, status, voucher_paths_json, voucher_pdf_paths_json,
                 unsigned_packet_path, unsigned_packet_sha256, docuseal_submission_id,
                 created_at_utc, updated_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "packet-1",
                period_id,
                1,
                "awaiting_signature",
                "[]",
                "[]",
                str(unsigned),
                "sha",
                "pay-ds-1",
                "now",
                "now",
            ),
        )
        graph = _FakeGraph()
        mailer = _FakeMailer()
        result = await handle_pay_docuseal_completion(
            db=self.db,
            cfg=cfg,
            graph=graph,
            mailer=mailer,
            docuseal=_FakeDocuSeal(),
            submission_id="pay-ds-1",
            payload={"event_type": "completed"},
        )
        self.assertEqual(result["handled"], True)
        period_row = await self.db.fetchone("SELECT status FROM pay_periods WHERE id=?", (period_id,))
        self.assertEqual(period_row[0], "completed")
        packet_row = await self.db.fetchone("SELECT status, signed_packet_path FROM pay_packets WHERE id='packet-1'")
        self.assertEqual(packet_row[0], "completed")
        self.assertTrue(Path(packet_row[1]).exists())
        self.assertEqual(mailer.calls[0]["to_recipients"], ["treasurer@example.org"])

    async def test_create_revision_opens_new_period_revision(self) -> None:
        period_id, _ = await self._open_period_and_entry()
        actor = PayActor("treasurer@example.org", "Treasurer", "treasurer", True, True, True)
        revised = await create_revision(self.db, period_id=period_id, actor=actor)
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["status"], "open")
