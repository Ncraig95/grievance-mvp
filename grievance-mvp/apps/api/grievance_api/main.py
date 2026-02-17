
from __future__ import annotations
import logging
from fastapi import FastAPI
from .core.config import load_config
from .core.logging import setup_logging
from .db.migrate import migrate
from .db.db import Db
from .services.docuseal_client import DocuSealClient
from .services.sharepoint_graph import GraphUploader
from .web.routes_health import router as health_router
from .web.routes_intake import router as intake_router
from .web.routes_webhook import router as webhook_router

def create_app() -> FastAPI:
    setup_logging()
    cfg = load_config("/app/config/config.yaml")

    # Ensure DB schema exists
    migrate(cfg.db_path)

    app = FastAPI(title="Grievance MVP API", version="0.1.0")

    app.state.cfg = cfg
    app.state.db = Db(cfg.db_path)

    app.state.logger = logging.getLogger("grievance_api")

    app.state.docuseal = DocuSealClient(cfg.docuseal.base_url, cfg.docuseal.api_token)

    app.state.graph = GraphUploader(
        tenant_id=cfg.graph.tenant_id,
        client_id=cfg.graph.client_id,
        cert_thumbprint=cfg.graph.cert_thumbprint,
        cert_pem_path=cfg.graph.cert_pem_path,
    )

    app.include_router(health_router)
    app.include_router(intake_router)
    app.include_router(webhook_router)

    return app

app = create_app()
