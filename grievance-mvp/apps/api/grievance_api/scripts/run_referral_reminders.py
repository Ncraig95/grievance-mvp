from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from grievance_api.core.config import load_config
from grievance_api.db.db import Db
from grievance_api.db.migrate import migrate
from grievance_api.services.email_templates import EmailTemplateStore
from grievance_api.services.graph_mail import GraphMailer
from grievance_api.services.referral_service import ReferralService


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process due referral reminders.")
    parser.add_argument(
        "--config-path",
        default=os.getenv("APP_CONFIG_PATH", "/app/config/config.yaml"),
        help="Path to application config file",
    )
    return parser.parse_args()


async def _run(config_path: str) -> dict[str, object]:
    cfg = load_config(config_path)
    migrate(cfg.db_path)
    db = Db(cfg.db_path)
    logger = logging.getLogger("grievance_api.referral_reminders")
    mailer = None
    if cfg.email.enabled:
        mailer = GraphMailer(
            tenant_id=cfg.graph.tenant_id,
            client_id=cfg.graph.client_id,
            cert_thumbprint=cfg.graph.cert_thumbprint,
            cert_pem_path=cfg.graph.cert_pem_path,
            sender_user_id=cfg.email.sender_user_id,
            dry_run=cfg.email.dry_run,
        )
    service = ReferralService(
        db=db,
        logger=logger,
        referral_cfg=cfg.referrals,
        email_cfg=cfg.email,
        officer_auth_cfg=cfg.officer_auth,
        mailer=mailer,
        template_store=EmailTemplateStore(cfg.email.templates_dir),
    )
    return {"ok": True, **(await service.run_due())}


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        result = asyncio.run(_run(args.config_path))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
