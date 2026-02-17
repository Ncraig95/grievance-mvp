from __future__ import annotations

import logging

from fastapi import FastAPI

from .core.config import load_config
from .core.logging import setup_logging
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
from .web.routes_webhook import router as webhook_router


def create_app() -> FastAPI:
    setup_logging()
    cfg = load_config("/app/config/config.yaml")

    migrate(cfg.db_path)

    app = FastAPI(title="Grievance MVP API", version="0.2.0")

    app.state.cfg = cfg
    app.state.db = Db(cfg.db_path)
    app.state.logger = logging.getLogger("grievance_api")

    app.state.docuseal = DocuSealClient(
        cfg.docuseal.base_url,
        cfg.docuseal.api_token,
        public_base_url=cfg.docuseal.public_base_url,
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
    app.include_router(intake_router)
    app.include_router(webhook_router)
    app.include_router(notifications_router)
    app.include_router(approval_router)

    return app


app = create_app()
