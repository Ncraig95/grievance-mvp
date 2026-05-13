from __future__ import annotations

import io
import random
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
    calculate_pay_profile_snapshot,
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
    upsert_pay_profile,
    upsert_wage_scale,
    validate_receipt_file,
)
from grievance_api.services.sharepoint_graph import DirectoryUserRef
from grievance_api.web.routes_pay import (
    InternalRoleAssignmentRequest,
    PayDemoArtifactRequest,
    PayDemoFeedbackStatusRequest,
    PayDemoFeedbackRequest,
    PayDemoSettingsUpdateRequest,
    PayProfileUpsertRequest,
    approve_pay_irs_rate,
    create_pay_profile,
    generate_pay_demo_output_files,
    pay_context,
    pay_demo_context,
    pay_directory_users,
    pay_page,
    pay_start_page,
    pay_view_page,
    remove_pay_internal_role,
    save_pay_internal_role,
    submit_pay_demo_feedback,
    update_pay_demo_feedback,
    update_pay_demo_settings,
)


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


class _InlineExecutor:
    def submit(self, fn, **kwargs):  # noqa: ANN001
        fn(**kwargs)
        return SimpleNamespace(done=lambda: True)


class _DirectoryGraphStub:
    def __init__(self, rows: list[DirectoryUserRef]) -> None:
        self.rows = rows
        self.last_search: str | None = None
        self.last_limit: int | None = None

    def search_directory_users(self, search_text: str, *, limit: int = 10) -> list[DirectoryUserRef]:
        self.last_search = search_text
        self.last_limit = limit
        return list(self.rows)


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


def _fake_watermark(input_path: str, output_path: str | None = None, *, text: str = "DEMO") -> None:
    target = Path(output_path or input_path)
    if output_path:
        target.write_bytes(Path(input_path).read_bytes())
    target.write_text(target.read_text(encoding="utf-8") + f"|WATERMARK:{text}", encoding="utf-8")


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
                common_places=(),
                common_places_sharepoint_folder="Local3106/Mileage/Config",
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

    @staticmethod
    def _staff_session(role: str, *, email: str = "admin@example.org") -> dict[str, object]:
        return {
            "officer_user": {
                "email": email,
                "display_name": email,
                "role": role,
                "contract_scopes": [],
                "exp": int(time.time()) + 3600,
            }
        }

    async def _save_profile(
        self,
        *,
        email: str,
        display_name: str | None = None,
        pay_basis: str = "hourly",
        base_wage_input_type: str = "hourly",
        base_wage_amount: float = 45,
        weekly_basis_hours: float = 40,
        commission_month_1_amount: float = 0,
        commission_month_2_amount: float = 0,
        commission_month_3_amount: float = 0,
    ) -> dict[str, object]:
        return await upsert_pay_profile(
            self.db,
            principal_id=None,
            principal_email=email,
            principal_display_name=display_name or email,
            pay_basis=pay_basis,
            base_wage_input_type=base_wage_input_type,
            base_wage_amount=base_wage_amount,
            weekly_basis_hours=weekly_basis_hours,
            commission_month_1_amount=commission_month_1_amount,
            commission_month_2_amount=commission_month_2_amount,
            commission_month_3_amount=commission_month_3_amount,
            status="active",
            notes=None,
            updated_by="test",
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
        await self._save_profile(email=actor.email, display_name=actor.display_name)
        period = await pay_portal.ensure_pay_period(self.db)
        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": str(period["period_start"]),
                "display_name": actor.display_name,
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
        demo_settings = await pay_portal.pay_demo_settings(self.db)
        self.assertTrue(demo_settings["demo_mode_enabled"])
        self.assertEqual(demo_settings["demo_cycle_title"], "Training Demo Cycle")
        self.assertIn(
            {"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL 32205, USA"},
            settings["common_places"],
        )
        self.assertIn(
            {"label": "State Road 16", "address": "4875 State Road 16, St. Augustine, FL 32092, USA"},
            settings["common_places"],
        )

    async def test_legacy_common_places_pick_up_seeded_defaults(self) -> None:
        cfg = SimpleNamespace(
            president_email="",
            treasurer_emails=(),
            irs_rates={},
            common_places=(),
        )
        await self.db.upsert_app_setting(
            setting_key="pay_portal",
            setting={"common_places": [{"label": "Legacy", "address": "1 Existing St"}]},
            updated_by="test",
        )
        settings = await pay_portal.pay_settings(self.db, pay_cfg=cfg)
        self.assertIn({"label": "Legacy", "address": "1 Existing St"}, settings["common_places"])
        self.assertIn(
            {"label": "State Road 16", "address": "4875 State Road 16, St. Augustine, FL 32092, USA"},
            settings["common_places"],
        )

    async def test_pay_settings_common_places_can_be_overridden_for_removal(self) -> None:
        cfg = SimpleNamespace(
            president_email="",
            treasurer_emails=(),
            irs_rates={},
            common_places=(),
        )
        saved = await pay_portal.save_pay_settings(
            self.db,
            setting={"common_places": [{"label": "Only", "address": "1 Test St"}]},
            updated_by="test",
            pay_cfg=cfg,
        )
        settings = await pay_portal.pay_settings(self.db, pay_cfg=cfg)
        self.assertEqual(saved["common_places"], [{"label": "Only", "address": "1 Test St"}])
        self.assertEqual(settings["common_places"], [{"label": "Only", "address": "1 Test St"}])

    async def test_pay_demo_feedback_is_saved_for_training_review(self) -> None:
        actor = PayActor(
            email="officer@example.org",
            display_name="Officer Example",
            role="officer",
            can_view_all=True,
            can_edit_all=False,
            can_lock=False,
        )
        saved = await pay_portal.create_pay_demo_feedback(
            self.db,
            actor=actor,
            screen="mileage",
            category="missing",
            demo_step=2,
            demo_cycle_title="Officer Training",
            comment="Show the generated mileage PDF before attaching it.",
        )
        rows = await pay_portal.list_pay_demo_feedback(self.db)

        self.assertEqual(saved["screen"], "mileage")
        self.assertEqual(saved["category"], "missing")
        self.assertEqual(saved["demo_step"], 2)
        self.assertEqual(saved["demo_cycle_title"], "Officer Training")
        self.assertEqual(rows[0]["actor_email"], "officer@example.org")
        self.assertEqual(rows[0]["comment"], "Show the generated mileage PDF before attaching it.")

        closed = await pay_portal.update_pay_demo_feedback_status(self.db, feedback_id=saved["id"], status="closed")
        self.assertEqual(closed["status"], "closed")

    async def test_pay_demo_feedback_api_respects_demo_mode_setting(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session=self._staff_session("officer"),
        )
        saved = await submit_pay_demo_feedback(
            PayDemoFeedbackRequest(
                screen="demo",
                category="suggestion",
                demo_step=4,
                demo_cycle_title="May training",
                comment="Add a printable checklist.",
            ),
            request,
        )
        self.assertEqual(saved["category"], "suggestion")
        self.assertEqual(saved["demo_step"], 4)

        admin_request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session=self._staff_session("admin"),
        )
        updated = await update_pay_demo_settings(
            PayDemoSettingsUpdateRequest(demo_mode_enabled=False, demo_cycle_title="Paused Demo"),
            admin_request,
        )
        self.assertFalse(updated["demo_mode_enabled"])
        with self.assertRaises(HTTPException) as denied:
            await submit_pay_demo_feedback(
                PayDemoFeedbackRequest(screen="demo", category="suggestion", comment="Try while disabled."),
                request,
            )
        self.assertEqual(denied.exception.status_code, 400)

        reopened = await update_pay_demo_feedback(
            int(saved["id"]),
            PayDemoFeedbackStatusRequest(status="closed"),
            admin_request,
        )
        self.assertEqual(reopened["status"], "closed")

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

    async def test_pay_demo_context_is_isolated_from_live_period_records(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session=self._staff_session("officer", email="officer@example.org"),
        )
        context = await pay_demo_context(request)
        pay_periods = await self.db.fetchall("SELECT id FROM pay_periods")

        self.assertEqual(context["period"]["id"], "demo")
        self.assertEqual(context["period"]["status"], "demo")
        self.assertEqual(context["entries"], [])
        self.assertEqual(context["demo_artifacts"], [])
        self.assertEqual(pay_periods, [])

    async def test_pay_demo_artifact_generation_writes_demo_only_files(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        Path(cfg.pay_portal.voucher_template_path).write_text("template", encoding="utf-8")
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session=self._staff_session("officer", email="officer@example.org"),
        )
        with patch(
            "grievance_api.services.pay_portal.build_mileage_pdf",
            return_value=(b"%PDF-1.4\nmileage\n", pay_portal.Decimal("24.65"), pay_portal.Decimal("34.00")),
        ) as build_mileage, patch(
            "grievance_api.services.pay_portal.fill_pay_voucher_docx",
            side_effect=_fake_fill_docx,
        ) as fill_docx, patch(
            "grievance_api.services.pay_portal.watermark_pdf",
            side_effect=_fake_watermark,
        ) as watermark, patch(
            "grievance_api.services.pay_portal.merge_pdfs",
            side_effect=_fake_merge,
        ) as merge, patch(
            "grievance_api.web.routes_pay.docx_to_pdf",
            side_effect=_fake_docx_to_pdf,
        ), patch(
            "grievance_api.web.routes_pay._DEMO_JOB_EXECUTOR",
            _InlineExecutor(),
        ), patch(
            "grievance_api.services.pay_portal._prefetch_google_legs",
            return_value=None,
        ):
            result = await generate_pay_demo_output_files(
                PayDemoArtifactRequest(demo_step=4, demo_cycle_title="Officer Training"),
                request,
            )
        pay_periods = await self.db.fetchall("SELECT id FROM pay_periods")
        filenames = {row["filename"] for row in result["rows"]}
        artifact_dir = Path(self.data_root) / "pay" / "demo" / "officerexample.org"
        packet_text = (artifact_dir / "demo-payroll-packet.pdf").read_text(encoding="utf-8")

        self.assertEqual(filenames, {"demo-payroll-packet.pdf"})
        self.assertEqual(result["status"], "completed")
        self.assertIn("job_id", result)
        self.assertEqual(pay_periods, [])
        self.assertIn("demo-voucher-01", packet_text)
        self.assertIn("demo-voucher-10", packet_text)
        self.assertIn("demo-mileage-01", packet_text)
        self.assertIn("demo-mileage-10", packet_text)
        self.assertIn("WATERMARK:DEMO", packet_text)
        self.assertEqual(build_mileage.call_count, 10)
        self.assertEqual(fill_docx.call_count, 10)
        merge.assert_called_once()
        watermark.assert_called_once()
        filled_entries = [entry for call in fill_docx.call_args_list for entry in call.kwargs["entries"]]
        self.assertEqual(len({row["display_name"] for row in filled_entries}), 10)
        self.assertGreater(len({row["lost_wage_hourly_rate"] for row in filled_entries}), 1)
        self.assertGreater(len({row["entry_date"] for row in filled_entries}), 3)
        self.assertEqual(len(filled_entries), 38)
        self.assertEqual(sorted(len(call.kwargs["entries"]) for call in fill_docx.call_args_list)[:7], [2] * 7)
        self.assertEqual(sorted(len(call.kwargs["entries"]) for call in fill_docx.call_args_list)[7:], [8] * 3)
        self.assertTrue(all(float(row["mileage_amount"]) > 0 for row in filled_entries))
        self.assertTrue(all(str(row["notes"]).startswith("DEMO TRAINING - ") for row in filled_entries))

    async def test_pay_demo_packet_entries_have_demo_narratives_and_varied_days(self) -> None:
        rng = random.Random("training-test")
        entries = pay_portal._demo_entries_for_packet(  # noqa: SLF001
            period_start="2026-05-03",
            demo_step=4,
            person={
                "display_name": "Demo Steward",
                "email": "demo.steward@cwa3106.local",
                "address": "11700 Phillips Hwy, Jacksonville, FL 32256, USA",
                "role": "steward",
            },
            day_offsets=[1, 5, 9],
            hourly_rate=pay_portal.Decimal("42.25"),
            mileage_miles=pay_portal.Decimal("34.00"),
            mileage_rate=pay_portal.Decimal("0.725"),
            mileage_amount=pay_portal.Decimal("24.65"),
            rng=rng,
        )

        self.assertEqual(len(entries), 3)
        self.assertEqual({row["entry_date"] for row in entries}, {"2026-05-04", "2026-05-08", "2026-05-12"})
        self.assertTrue(all(str(row["notes"]).startswith("DEMO TRAINING - ") for row in entries))
        self.assertTrue(all(row["lost_wage_hourly_rate"] == 42.25 for row in entries))
        self.assertTrue(all(float(row["mileage_amount"]) > 0 for row in entries))

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
        self.assertIn('id="entryForm"', entry_html)
        self.assertNotIn('name="display_name"', entry_html)
        self.assertNotIn("Lost Wage Type", entry_html)
        self.assertNotIn("Lost Wage Amount", entry_html)
        self.assertNotIn("Mileage Miles<input", entry_html)
        self.assertNotIn("Mileage Amount<input", entry_html)
        self.assertNotIn("IRS Rate<input", entry_html)
        self.assertIn("Daily Tally", entry_html)
        self.assertIn('id="dailyTallyBody"', entry_html)
        self.assertIn('href="/pay/mileage"', entry_html)
        self.assertIn('href="/pay/demo"', entry_html)
        self.assertIn('href="/pay/president"', entry_html)
        self.assertIn('href="/pay/treasurer"', entry_html)
        self.assertNotIn("Pay User Allowlist", entry_html)

        mileage_html = (await pay_view_page("mileage", officer_request)).body.decode("utf-8")
        self.assertIn("Mileage Tracker", mileage_html)
        self.assertIn('id="mileageForm"', mileage_html)
        self.assertNotIn('label for="name"', mileage_html)
        self.assertIn('type="hidden" id="name" name="name"', mileage_html)
        self.assertIn('id="commonPlaceSelect"', mileage_html)
        self.assertIn('id="irsRateDisplay"', mileage_html)
        self.assertNotIn('id="irs_rate"', mileage_html)
        self.assertIn('id="locations"', mileage_html)
        self.assertIn("Add Location", mileage_html)
        self.assertIn("Generate Mileage PDF", mileage_html)
        self.assertIn("rate: null", mileage_html)
        self.assertIn("/pay/api/entries/${entryId}/mileage", mileage_html)
        self.assertNotIn("Pay User Allowlist", mileage_html)

        demo_html = (await pay_view_page("demo", officer_request)).body.decode("utf-8")
        self.assertIn("Training Demo Cycle", demo_html)
        self.assertIn('id="demoCyclePanel"', demo_html)
        self.assertIn('id="demoChecklist"', demo_html)
        self.assertIn('id="demoActivityLog"', demo_html)
        self.assertIn('id="demoFilesBody"', demo_html)
        self.assertIn('id="generateDemoFilesBtn"', demo_html)
        self.assertIn('id="demoFeedbackForm"', demo_html)
        self.assertIn("/pay/api/demo/context", demo_html)
        self.assertIn("/pay/api/demo/artifacts", demo_html)
        self.assertIn("/pay/api/demo/feedback", demo_html)
        self.assertNotIn("Lock And Send For Signature", demo_html)

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
        self.assertIn("Microsoft Role Assignments", admin_html)
        self.assertIn("Save Pay Profile", admin_html)
        self.assertIn("Demo Mode", admin_html)
        self.assertIn('id="demoSettingsForm"', admin_html)
        self.assertIn("/pay/api/demo/settings", admin_html)
        self.assertIn("IRS Rate Updates", admin_html)
        self.assertIn('id="commonPlaceForm"', admin_html)
        self.assertIn('id="commonPlacesBody"', admin_html)
        self.assertNotIn('id="entryForm"', admin_html)

    async def test_pay_admin_can_search_internal_microsoft_users(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        graph = _DirectoryGraphStub(
            [
                DirectoryUserRef(
                    id="oid-president-1",
                    display_name="Pat President",
                    email="president@cwa3106.com",
                    user_principal_name="president@cwa3106.com",
                )
            ]
        )
        request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db, graph=graph),
            session=self._staff_session("admin"),
        )

        response = await pay_directory_users(request, search="pres", limit=8)

        self.assertEqual(response.count, 1)
        self.assertEqual(response.rows[0].principal_id, "oid-president-1")
        self.assertEqual(response.rows[0].email, "president@cwa3106.com")
        self.assertEqual(graph.last_search, "pres")
        self.assertEqual(graph.last_limit, 8)

    async def test_pay_admin_can_assign_president_and_treasurer_from_internal_accounts(self) -> None:
        cfg = self._cfg()
        cfg.officer_auth.enabled = True
        admin_request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session=self._staff_session("admin"),
        )

        president = await save_pay_internal_role(
            InternalRoleAssignmentRequest(
                principal_id="oid-president-1",
                principal_email="president@cwa3106.com",
                principal_display_name="Pat President",
                role="president",
            ),
            admin_request,
        )
        treasurer = await save_pay_internal_role(
            InternalRoleAssignmentRequest(
                principal_id="oid-treasurer-1",
                principal_email="treasurer@cwa3106.com",
                principal_display_name="Terry Treasurer",
                role="treasurer",
            ),
            admin_request,
        )

        settings = await pay_settings(self.db, pay_cfg=cfg.pay_portal)
        self.assertEqual(settings["president_email"], "president@cwa3106.com")
        context = await pay_context(admin_request)
        self.assertEqual(
            {(row["principal_email"], row["role"]) for row in context["internal_roles"]},
            {("president@cwa3106.com", "president"), ("treasurer@cwa3106.com", "treasurer")},
        )
        profile = await create_pay_profile(
            PayProfileUpsertRequest(
                principal_id="oid-president-1",
                principal_email="president@cwa3106.com",
                principal_display_name="Pat President",
                pay_basis="president",
                base_wage_input_type="hourly",
                base_wage_amount=45,
                commission_month_1_amount=999,
                commission_month_2_amount=999,
                commission_month_3_amount=999,
            ),
            admin_request,
        )
        self.assertEqual(profile["pay_basis"], "president")
        self.assertEqual(profile["commission_hourly_rate"], 0.0)
        context_with_profile = await pay_context(admin_request)
        self.assertIn("pay_profiles", context_with_profile)
        self.assertEqual(context_with_profile["pay_profiles"][0]["principal_email"], "president@cwa3106.com")

        treasurer_request = _Request(
            state=SimpleNamespace(cfg=cfg, db=self.db),
            session=self._staff_session("officer", email="treasurer@cwa3106.com"),
        )
        redirect = await pay_page(treasurer_request)
        self.assertEqual(redirect.headers["location"], "/pay/treasurer")

        deleted = await remove_pay_internal_role(int(president["assignment_id"]), admin_request)
        self.assertEqual(deleted["role"], "president")
        settings_after_delete = await pay_settings(self.db, pay_cfg=cfg.pay_portal)
        self.assertEqual(settings_after_delete["president_email"], "")
        self.assertEqual(treasurer["role"], "treasurer")

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

    def test_common_places_config_parses_sharepoint_file_shapes(self) -> None:
        json_rows = pay_portal.parse_common_places_config(
            filename="places.json",
            content='{"places":[{"name":"Union Hall","address":"4076 Union Hall Pl, Jacksonville, FL"}]}',
        )
        csv_rows = pay_portal.parse_common_places_config(
            filename="addresses.csv",
            content='label,address\nPhillips Hwy,"11700 Phillips Hwy, Jacksonville, FL"\n',
        )
        text_rows = pay_portal.parse_common_places_config(
            filename="legacy.txt",
            content="Penman Rd=98 Penman Rd S, Jacksonville Beach, FL\n",
        )

        merged = pay_portal.merge_common_places(json_rows, csv_rows, text_rows)
        self.assertEqual(
            merged,
            [
                {"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL"},
                {"label": "Phillips Hwy", "address": "11700 Phillips Hwy, Jacksonville, FL"},
                {"label": "Penman Rd", "address": "98 Penman Rd S, Jacksonville Beach, FL"},
            ],
        )

    def test_common_places_cache_writes_app_side_file(self) -> None:
        path = pay_portal.write_common_places_cache(
            data_root=self.data_root,
            places=[
                {"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL"},
                {"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL"},
            ],
        )

        self.assertEqual(path, Path(self.data_root) / "pay" / "common_places.json")
        self.assertEqual(
            pay_portal.load_common_places_cache(data_root=self.data_root),
            [{"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL"}],
        )

    def test_sharepoint_common_places_loader_reads_config_folder(self) -> None:
        class _SharePointConfigGraph:
            def __init__(self) -> None:
                self.list_call: dict[str, object] | None = None

            def list_files_in_folder_path(self, **kwargs):  # noqa: ANN003
                self.list_call = dict(kwargs)
                return [
                    SimpleNamespace(drive_id="drive", item_id="places", name="places.csv"),
                    SimpleNamespace(drive_id="drive", item_id="ignored", name="notes.pdf"),
                ]

            def download_item_bytes(self, *, drive_id: str, item_id: str) -> bytes:
                if (drive_id, item_id) == ("drive", "places"):
                    return b'label,address\nUnion Hall,"4076 Union Hall Pl, Jacksonville, FL"\n'
                return b""

        graph = _SharePointConfigGraph()
        rows = pay_portal.load_sharepoint_common_places(
            graph=graph,
            graph_cfg=SimpleNamespace(
                site_hostname="contoso.sharepoint.com",
                site_path="/sites/cwa3106",
                document_library="Officer Eboard committee - Documents",
            ),
            pay_cfg=SimpleNamespace(common_places_sharepoint_folder=r"Local3106\Mileage\Config"),
        )

        self.assertEqual(graph.list_call["folder_path"], "Local3106/Mileage/Config")
        self.assertEqual(graph.list_call["library"], "Officer Eboard committee - Documents")
        self.assertEqual(rows, [{"label": "Union Hall", "address": "4076 Union Hall Pl, Jacksonville, FL"}])

    def test_sharepoint_common_places_loader_accepts_full_synced_path(self) -> None:
        class _SharePointConfigGraph:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def list_files_in_folder_path(self, **kwargs):  # noqa: ANN003
                self.calls.append(dict(kwargs))
                if kwargs["library"] == "Documents":
                    raise RuntimeError("wrong library")
                return [SimpleNamespace(drive_id="drive", item_id="nested", name="nested/places.csv")]

            def download_item_bytes(self, *, drive_id: str, item_id: str) -> bytes:
                if (drive_id, item_id) == ("drive", "nested"):
                    return b'label,address\nNested,"123 Nested St, Jacksonville, FL"\n'
                return b""

        graph = _SharePointConfigGraph()
        rows = pay_portal.load_sharepoint_common_places(
            graph=graph,
            graph_cfg=SimpleNamespace(
                site_hostname="cwa3106.sharepoint.com",
                site_path="/sites/GrievancesLibrary",
                document_library="Documents",
            ),
            pay_cfg=SimpleNamespace(
                common_places_sharepoint_library="",
                common_places_sharepoint_folder=(
                    r"CWA 3106\Officer Eboard committee - Documents\Local3106\Mileage\Config"
                ),
            ),
        )

        self.assertEqual(graph.calls[0]["library"], "Officer Eboard committee - Documents")
        self.assertEqual(graph.calls[0]["folder_path"], "Local3106/Mileage/Config")
        self.assertTrue(graph.calls[0]["recursive"])
        self.assertEqual(rows, [{"label": "Nested", "address": "123 Nested St, Jacksonville, FL"}])

    def test_daily_pay_narrative_groups_notes_by_date(self) -> None:
        lines = pay_portal.pay_entry_daily_narrative_lines(
            [
                {"entry_date": "2026-05-11", "notes": "Prepared grievance packet"},
                {"entry_date": "2026-05-10", "notes": "Met with member\nand steward"},
                {"entry_date": "2026-05-10", "notes": "Reviewed payroll records"},
                {"entry_date": "2026-05-12", "notes": ""},
            ]
        )

        self.assertEqual(
            lines,
            [
                "Sun 05/10/2026: Met with member and steward; Reviewed payroll records",
                "Mon 05/11/2026: Prepared grievance packet",
            ],
        )

    def test_voucher_entry_amounts_use_profile_lost_wage_rate(self) -> None:
        amounts = pay_portal._entry_amounts(  # noqa: SLF001
            {
                "hours": 4,
                "hourly_rate": 1,
                "lost_wage_hourly_rate": 38.5,
                "mileage_amount": 12.34,
            }
        )

        self.assertEqual(amounts["hours"], pay_portal.Decimal("154.00"))
        self.assertEqual(amounts["mileage"], pay_portal.Decimal("12.34"))

    def test_voucher_narrative_line_splits_date_label_for_back_page(self) -> None:
        label, narrative = pay_portal._split_narrative_line(  # noqa: SLF001
            "Mon 05/04/2026: DEMO TRAINING - reviewed mileage."
        )

        self.assertEqual(label, "Mon 05/04/2026")
        self.assertEqual(narrative, "DEMO TRAINING - reviewed mileage.")

    def test_voucher_front_receipt_narrative_rows_are_removed(self) -> None:
        class _TableXml:
            def __init__(self) -> None:
                self.removed = []

            def remove(self, row_tr):  # noqa: ANN001
                self.removed.append(row_tr)

        receipt_row = SimpleNamespace(
            cells=[SimpleNamespace(text="Attach necessary receipts - Explain reason for expense:")],
            _tr="receipt-row",
        )
        keep_row = SimpleNamespace(cells=[SimpleNamespace(text="Hours")], _tr="keep-row")
        table_xml = _TableXml()
        doc = SimpleNamespace(
            tables=[
                SimpleNamespace(rows=[keep_row], _tbl=_TableXml()),
                SimpleNamespace(rows=[receipt_row], _tbl=table_xml),
                SimpleNamespace(rows=[receipt_row], _tbl=_TableXml()),
            ]
        )

        pay_portal._remove_front_receipt_explanation_rows(doc)  # noqa: SLF001

        self.assertEqual(table_xml.removed, ["receipt-row"])

    def test_voucher_totals_row_found_by_label(self) -> None:
        totals_row = SimpleNamespace(
            cells=[SimpleNamespace(text="TOTALS"), SimpleNamespace(text="$"), SimpleNamespace(text="$")]
        )
        doc = SimpleNamespace(
            tables=[
                SimpleNamespace(rows=[SimpleNamespace(cells=[SimpleNamespace(text="Hours")])]),
                SimpleNamespace(rows=[totals_row]),
            ]
        )

        self.assertIs(pay_portal._find_totals_row(doc), totals_row)  # noqa: SLF001

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

    def test_pay_profile_snapshot_calculates_supported_pay_bases(self) -> None:
        hourly = calculate_pay_profile_snapshot(
            pay_basis="hourly",
            base_wage_input_type="weekly",
            base_wage_amount=45,
            weekly_basis_hours=40,
        )
        weekly = calculate_pay_profile_snapshot(
            pay_basis="weekly",
            base_wage_input_type="hourly",
            base_wage_amount=1800,
            weekly_basis_hours=40,
        )
        commission = calculate_pay_profile_snapshot(
            pay_basis="commission",
            base_wage_input_type="hourly",
            base_wage_amount=45,
            weekly_basis_hours=40,
            commission_month_1_amount=1000,
            commission_month_2_amount=1400,
            commission_month_3_amount=1800,
        )
        president = calculate_pay_profile_snapshot(
            pay_basis="president",
            base_wage_input_type="hourly",
            base_wage_amount=45,
            weekly_basis_hours=40,
            commission_month_1_amount=1000,
            commission_month_2_amount=1400,
            commission_month_3_amount=1800,
        )
        expense_only = calculate_pay_profile_snapshot(
            pay_basis="expense_only",
            base_wage_input_type="weekly",
            base_wage_amount=1800,
            weekly_basis_hours=40,
        )

        self.assertEqual(hourly["base_wage_input_type"], "hourly")
        self.assertEqual(hourly["calculated_hourly_rate"], pay_portal.Decimal("45.00"))
        self.assertEqual(weekly["base_wage_input_type"], "weekly")
        self.assertEqual(weekly["calculated_hourly_rate"], pay_portal.Decimal("45.00"))
        self.assertEqual(commission["commission_hourly_rate"], pay_portal.Decimal("8.75"))
        self.assertEqual(commission["calculated_hourly_rate"], pay_portal.Decimal("53.75"))
        self.assertEqual(president["commission_hourly_rate"], pay_portal.Decimal("0.00"))
        self.assertEqual(president["calculated_hourly_rate"], pay_portal.Decimal("45.00"))
        self.assertEqual(expense_only["calculated_hourly_rate"], pay_portal.Decimal("0.00"))

    async def test_president_entry_uses_stored_base_wage_profile_without_commission(self) -> None:
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
        profile = await self._save_profile(
            email="president@example.org",
            display_name="President",
            pay_basis="president",
            base_wage_input_type="weekly",
            base_wage_amount=1800,
            commission_month_1_amount=1000,
            commission_month_2_amount=1400,
            commission_month_3_amount=1800,
        )
        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": "2026-05-10",
                "display_name": "President",
                "hours": 1,
                "president_diff_hours": 3,
            },
            pay_cfg=self._cfg().pay_portal,
        )
        self.assertEqual(profile["commission_hourly_rate"], 0.0)
        self.assertEqual(entry["lost_wage_input_type"], "president")
        self.assertEqual(entry["lost_wage_amount"], 1800.0)
        self.assertEqual(entry["lost_wage_hourly_rate"], 45.0)
        self.assertEqual(entry["hourly_rate"], 45.0)
        self.assertEqual(entry["president_diff_hours"], 1.0)
        self.assertEqual(entry["president_diff_amount"], 16.97)
        treasurer_entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=PayActor("treasurer@example.org", "Treasurer", "treasurer", True, True, True),
            data={
                "user_email": "president@example.org",
                "entry_date": "2026-05-11",
                "display_name": "President",
                "hours": 1,
                "president_diff_hours": 2,
            },
            pay_cfg=self._cfg().pay_portal,
        )
        self.assertEqual(treasurer_entry["president_diff_hours"], 2.0)
        self.assertEqual(treasurer_entry["president_diff_amount"], 33.94)

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

    async def test_entry_uses_saved_commission_pay_profile(self) -> None:
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
        actor = PayActor("commission@example.org", "Commission", "officer", True, False, False)
        profile = await self._save_profile(
            email="commission@example.org",
            display_name="Commission",
            pay_basis="commission",
            base_wage_input_type="hourly",
            base_wage_amount=45,
            weekly_basis_hours=40,
            commission_month_1_amount=1000,
            commission_month_2_amount=1400,
            commission_month_3_amount=1800,
        )
        period = await pay_portal.ensure_pay_period(self.db, for_date=date.fromisoformat("2026-05-10"))
        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": "2026-05-10",
                "display_name": "Typed Overwrite",
                "hours": 2,
                "hourly_rate": 1,
                "lost_wage_amount": 1,
            },
            pay_cfg=self._cfg().pay_portal,
        )
        self.assertEqual(profile["calculated_hourly_rate"], 53.75)
        self.assertEqual(entry["display_name"], "Commission")
        self.assertEqual(entry["lost_wage_input_type"], "commission")
        self.assertIsNone(entry["compensation_stub_id"])
        self.assertEqual(entry["lost_wage_hourly_rate"], 53.75)
        self.assertEqual(entry["hourly_rate"], 53.75)
        self.assertEqual(entry["president_diff_amount"], 0.0)

    async def test_missing_profile_allows_expenses_only_and_rejects_lost_wage_hours(self) -> None:
        period = await pay_portal.ensure_pay_period(self.db, for_date=date.fromisoformat("2026-05-10"))
        actor = PayActor("no-profile@example.org", "No Profile", "officer", True, False, False)

        expense_entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=actor,
            data={
                "entry_date": "2026-05-10",
                "mileage_miles": 10,
                "mileage_rate": 0.725,
                "mileage_amount": 7.25,
            },
            pay_cfg=self._cfg().pay_portal,
        )

        self.assertEqual(expense_entry["lost_wage_hourly_rate"], 0.0)
        self.assertEqual(expense_entry["mileage_amount"], 7.25)

        with self.assertRaisesRegex(ValueError, "pay profile required"):
            await upsert_entry(
                self.db,
                period_id=str(period["id"]),
                actor=actor,
                data={
                    "entry_date": "2026-05-11",
                    "hours": 1,
                },
                pay_cfg=self._cfg().pay_portal,
            )

    async def test_treasurer_entry_for_another_user_uses_target_users_profile(self) -> None:
        await self._save_profile(
            email="member@example.org",
            display_name="Member Example",
            pay_basis="weekly",
            base_wage_input_type="weekly",
            base_wage_amount=2000,
            weekly_basis_hours=40,
        )
        period = await pay_portal.ensure_pay_period(self.db, for_date=date.fromisoformat("2026-05-10"))
        treasurer = PayActor("treasurer@example.org", "Treasurer", "treasurer", True, True, True)

        entry = await upsert_entry(
            self.db,
            period_id=str(period["id"]),
            actor=treasurer,
            data={
                "user_email": "member@example.org",
                "entry_date": "2026-05-10",
                "hours": 2,
                "hourly_rate": 1,
                "lost_wage_amount": 1,
            },
            pay_cfg=self._cfg().pay_portal,
        )

        self.assertEqual(entry["user_email"], "member@example.org")
        self.assertEqual(entry["display_name"], "Member Example")
        self.assertEqual(entry["lost_wage_input_type"], "weekly")
        self.assertEqual(entry["lost_wage_amount"], 2000.0)
        self.assertEqual(entry["lost_wage_hourly_rate"], 50.0)

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
