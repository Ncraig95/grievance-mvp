from __future__ import annotations

import asyncio
import contextlib
import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .core.config import load_config, resolve_config_path
from .core.intake_auth import validate_intake_auth_config
from .core.logging import setup_logging
from .core.officer_auth import validate_external_steward_auth_config, validate_officer_auth_config
from .db.db import Db
from .db.migrate import migrate
from .services.email_templates import EmailTemplateStore
from .services.notification_service import NotificationService
from .services.outreach_service import OutreachService
from .services.provider_factory import build_runtime_providers
from .services.referral_service import ReferralService
from .services.statement_auto_sign import run_statement_auto_sign_worker, statement_auto_sign_enabled
from .dues_forms.routes import router as dues_forms_router
from .web.routes_approval import router as approval_router
from .web.routes_health import router as health_router
from .web.routes_hosted_forms import router as hosted_forms_router
from .web.routes_intake import router as intake_router
from .web.routes_internal_forms import router as internal_forms_router
from .web.routes_notifications import router as notifications_router
from .web.officer_auth import router as officer_auth_router
from .web.routes_officers import router as officers_router
from .web.routes_ops import router as ops_router
from .web.routes_outreach import router as outreach_router
from .web.routes_pay import router as pay_router
from .web.routes_referrals import router as referrals_router
from .web.routes_steward import router as steward_router
from .web.routes_standalone import router as standalone_router
from .web.routes_webhook import router as webhook_router


def create_app() -> FastAPI:
    cfg = load_config(resolve_config_path())
    setup_logging(cfg.log_level)
    validate_intake_auth_config(cfg.intake_auth)
    validate_officer_auth_config(cfg.officer_auth)
    validate_external_steward_auth_config(cfg.external_steward_auth, cfg.officer_auth)

    migrate(cfg.db_path)

    app = FastAPI(title="Grievance MVP API", version="0.2.0")
    app.mount("/static/email", StaticFiles(directory="/app/templates/email/assets"), name="static-email")
    any_auth_enabled = cfg.officer_auth.enabled or cfg.external_steward_auth.enabled
    if any_auth_enabled:
        session_secret = str(cfg.officer_auth.session_secret or cfg.hmac_shared_secret or "").strip()
        if not session_secret:
            raise RuntimeError("session secret required when officer or external steward auth is enabled")
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            same_site="lax",
            https_only=(
                cfg.officer_auth.redirect_uri.startswith("https://")
                or cfg.external_steward_auth.redirect_uri.startswith("https://")
            ),
        )

    app.state.cfg = cfg
    app.state.db = Db(cfg.db_path)
    app.state.logger = logging.getLogger("grievance_api")

    if cfg.email.enabled:
        if not cfg.email.internal_recipients:
            raise RuntimeError("email.internal_recipients must contain at least one address when email.enabled=true")
        if not cfg.email.sender_user_id:
            raise RuntimeError("email.sender_user_id must be set when email.enabled=true")
    if cfg.outreach.enabled and not cfg.outreach.sender_user_id:
        raise RuntimeError("outreach.sender_user_id must be set when outreach.enabled=true")

    providers = build_runtime_providers(cfg)
    app.state.docuseal = providers.docuseal
    app.state.graph = providers.graph
    app.state.mailer = providers.mailer
    app.state.outreach_mailer = providers.outreach_mailer

    app.state.email_templates = EmailTemplateStore(cfg.email.templates_dir)
    app.state.notifications = NotificationService(
        db=app.state.db,
        logger=app.state.logger,
        mailer=app.state.mailer,
        template_store=app.state.email_templates,
        email_cfg=cfg.email,
    )
    app.state.outreach = OutreachService(
        db=app.state.db,
        logger=app.state.logger,
        outreach_cfg=cfg.outreach,
        email_cfg=cfg.email,
        officer_auth_cfg=cfg.officer_auth,
        mailer=app.state.outreach_mailer,
    )
    app.state.referrals = ReferralService(
        db=app.state.db,
        logger=app.state.logger,
        referral_cfg=cfg.referrals,
        email_cfg=cfg.email,
        officer_auth_cfg=cfg.officer_auth,
        mailer=app.state.mailer,
        template_store=app.state.email_templates,
    )

    @app.on_event("startup")
    async def _startup_services() -> None:
        await app.state.outreach.ensure_seed_data()
        app.state.statement_auto_sign_worker = None
        if statement_auto_sign_enabled(cfg):
            app.state.statement_auto_sign_worker = asyncio.create_task(
                run_statement_auto_sign_worker(
                    cfg=cfg,
                    db=app.state.db,
                    docuseal=app.state.docuseal,
                    logger=app.state.logger,
                )
            )

    @app.on_event("shutdown")
    async def _shutdown_services() -> None:
        worker = getattr(app.state, "statement_auto_sign_worker", None)
        if worker:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    app.include_router(health_router)
    app.include_router(officer_auth_router)
    app.include_router(hosted_forms_router)
    app.include_router(intake_router)
    app.include_router(internal_forms_router)
    app.include_router(webhook_router)
    app.include_router(notifications_router)
    app.include_router(approval_router)
    app.include_router(ops_router)
    app.include_router(officers_router)
    app.include_router(outreach_router)
    app.include_router(referrals_router)
    app.include_router(pay_router)
    app.include_router(dues_forms_router)
    app.include_router(steward_router)
    app.include_router(standalone_router)

    return app


app = create_app()
