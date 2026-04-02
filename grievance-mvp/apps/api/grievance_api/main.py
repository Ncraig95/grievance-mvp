from __future__ import annotations

import logging

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .core.config import load_config
from .core.intake_auth import validate_intake_auth_config
from .core.logging import setup_logging
from .core.officer_auth import validate_officer_auth_config
from .db.db import Db
from .db.migrate import migrate
from .services.docuseal_client import DocuSealClient
from .services.email_templates import EmailTemplateStore
from .services.graph_mail import GraphMailer
from .services.notification_service import NotificationService
from .services.sharepoint_graph import GraphUploader
from .web.routes_approval import router as approval_router
from .web.routes_health import router as health_router
from .web.routes_intake import router as intake_router
from .web.routes_notifications import router as notifications_router
from .web.officer_auth import router as officer_auth_router
from .web.routes_officers import router as officers_router
from .web.routes_ops import router as ops_router
from .web.routes_standalone import router as standalone_router
from .web.routes_webhook import router as webhook_router


def create_app() -> FastAPI:
    cfg = load_config("/app/config/config.yaml")
    setup_logging(cfg.log_level)
    validate_intake_auth_config(cfg.intake_auth)
    validate_officer_auth_config(cfg.officer_auth)

    migrate(cfg.db_path)

    app = FastAPI(title="Grievance MVP API", version="0.2.0")
    if cfg.officer_auth.enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=cfg.officer_auth.session_secret,
            same_site="lax",
            https_only=cfg.officer_auth.redirect_uri.startswith("https://"),
        )

    app.state.cfg = cfg
    app.state.db = Db(cfg.db_path)
    app.state.logger = logging.getLogger("grievance_api")

    signature_table_maps: dict[str, dict[str, dict[str, float | int]]] = {}
    for form_key, table_map in cfg.docuseal.signature_table_maps.items():
        signature_table_maps[form_key] = {
            cell_key: {
                "page": int(cell.page),
                "x": float(cell.x),
                "y": float(cell.y),
                "w": float(cell.w),
                "h": float(cell.h),
            }
            for cell_key, cell in table_map.cells.items()
        }

    app.state.docuseal = DocuSealClient(
        cfg.docuseal.base_url,
        cfg.docuseal.api_token,
        public_base_url=cfg.docuseal.public_base_url,
        web_base_url=cfg.docuseal.web_base_url,
        web_email=cfg.docuseal.web_email,
        web_password=cfg.docuseal.web_password,
        submitters_order=cfg.docuseal.submitters_order,
        submitters_order_by_form=cfg.docuseal.submitters_order_by_form,
        signature_layout_mode=cfg.docuseal.signature_layout_mode,
        signature_layout_mode_by_form=cfg.docuseal.signature_layout_mode_by_form,
        signature_table_trace_enabled=cfg.docuseal.signature_table_trace_enabled,
        signature_table_trace_by_form=cfg.docuseal.signature_table_trace_by_form,
        signature_table_guard_enabled=cfg.docuseal.signature_table_guard_enabled,
        signature_table_guard_tolerance=cfg.docuseal.signature_table_guard_tolerance,
        signature_table_guard_min_gap=cfg.docuseal.signature_table_guard_min_gap,
        signature_table_maps=signature_table_maps,
    )

    app.state.graph = GraphUploader(
        tenant_id=cfg.graph.tenant_id,
        client_id=cfg.graph.client_id,
        cert_thumbprint=cfg.graph.cert_thumbprint,
        cert_pem_path=cfg.graph.cert_pem_path,
        dry_run=cfg.email.dry_run,
    )

    app.state.mailer = None
    if cfg.email.enabled:
        if not cfg.email.internal_recipients:
            raise RuntimeError("email.internal_recipients must contain at least one address when email.enabled=true")
        if not cfg.email.sender_user_id:
            raise RuntimeError("email.sender_user_id must be set when email.enabled=true")
        app.state.mailer = GraphMailer(
            tenant_id=cfg.graph.tenant_id,
            client_id=cfg.graph.client_id,
            cert_thumbprint=cfg.graph.cert_thumbprint,
            cert_pem_path=cfg.graph.cert_pem_path,
            sender_user_id=cfg.email.sender_user_id,
            dry_run=cfg.email.dry_run,
        )

    app.state.email_templates = EmailTemplateStore(cfg.email.templates_dir)
    app.state.notifications = NotificationService(
        db=app.state.db,
        logger=app.state.logger,
        mailer=app.state.mailer,
        template_store=app.state.email_templates,
        email_cfg=cfg.email,
    )

    app.include_router(health_router)
    app.include_router(officer_auth_router)
    app.include_router(intake_router)
    app.include_router(webhook_router)
    app.include_router(notifications_router)
    app.include_router(approval_router)
    app.include_router(ops_router)
    app.include_router(officers_router)
    app.include_router(standalone_router)

    return app


app = create_app()
