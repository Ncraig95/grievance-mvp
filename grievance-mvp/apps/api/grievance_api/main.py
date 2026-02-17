
from __future__ import annotations
import logging
import subprocess
import time
import re
from fastapi import FastAPI
from .core.config import load_config, DocuSealConfig
from .core.logging import setup_logging
from .db.migrate import migrate
from .db.db import Db
from .services.docuseal_client import DocuSealClient
from .services.sharepoint_graph import GraphUploader
from .web.routes_health import router as health_router
from .web.routes_intake import router as intake_router
from .web.routes_webhook import router as webhook_router

def get_cloudflare_tunnel_url() -> str | None:
    """Get the public URL from the cloudflared logs."""
    try:
        output = subprocess.check_output(["docker", "logs", "cloudflared"], stderr=subprocess.STDOUT, text=True)
        # Look for the URL in the logs
        match = re.search(r"(https?://[a-zA-Z0-9-]+\.trycloudflare\.com)", output)
        if match:
            return match.group(1)
    except subprocess.CalledProcessError as e:
        logging.getLogger("grievance_api").error(f"Error getting cloudflare tunnel URL: {e.output}")
    return None

def update_docuseal_url(app: FastAPI, tunnel_url: str):
    """Update the DocuSeal APP_URL in the container and the client."""
    docuseal_config: DocuSealConfig = app.state.cfg.docuseal
    docuseal_config.host = tunnel_url.split("://")[1]
    docuseal_config.protocol = tunnel_url.split("://")[0]
    app.state.docuseal = DocuSealClient(docuseal_config.base_url, docuseal_config.api_token)
    
    # Update the APP_URL in the running docuseal container
    try:
        subprocess.run([
            "docker", "compose", "exec", "-T", "docuseal",
            "rails", "runner",
            f'Setting.first_or_create.update!(app_url: "{docuseal_config.base_url}")'
        ], check=True)
    except subprocess.CalledProcessError as e:
        logging.getLogger("grievance_api").error(f"Error updating DocuSeal APP_URL: {e}")

async def startup_event():
    """On startup, get the public URL and update DocuSeal."""
    app = FastAPI.current_app()
    tunnel_url = None
    # Retry for a bit to give cloudflared time to start
    for _ in range(5):
        tunnel_url = get_cloudflare_tunnel_url()
        if tunnel_url:
            break
        time.sleep(5)
    
    if tunnel_url:
        update_docuseal_url(app, tunnel_url)
    else:
        logging.getLogger("grievance_api").error("Could not get Cloudflare tunnel URL. DocuSeal may not work correctly.")

def create_app() -> FastAPI:
    setup_logging()
    cfg = load_config("/app/config/config.yaml")

    # Ensure DB schema exists
    migrate(cfg.db_path)

    app = FastAPI(title="Grievance MVP API", version="0.1.0")
    app.add_event_handler("startup", startup_event)

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
