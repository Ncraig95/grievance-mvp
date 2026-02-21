from __future__ import annotations

import argparse
import html
import json
import os
import sys

from grievance_api.core.config import load_config
from grievance_api.services.graph_mail import GraphMailer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send operational alert email through Microsoft Graph.")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Plain-text alert body")
    parser.add_argument(
        "--recipient",
        action="append",
        default=[],
        help="Optional recipient override (repeatable). Defaults to email.internal_recipients.",
    )
    parser.add_argument(
        "--config-path",
        default=os.getenv("APP_CONFIG_PATH", "/app/config/config.yaml"),
        help="Path to application config file",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config_path)
    recipients = [r.strip() for r in args.recipient if r and r.strip()]
    if not recipients:
        recipients = list(cfg.email.internal_recipients)

    if not cfg.email.enabled:
        print("email is disabled in config; alert not sent", file=sys.stderr)
        return 1
    if not recipients:
        print("no recipients configured for alert email", file=sys.stderr)
        return 1
    if not cfg.email.sender_user_id:
        print("email.sender_user_id is required to send alert email", file=sys.stderr)
        return 1

    subject = args.subject.strip()
    if cfg.email.test_mode and not subject.upper().startswith("[TEST]"):
        subject = f"[TEST] {subject}"

    text_body = args.body.strip()
    html_body = f"<pre>{html.escape(text_body)}</pre>"

    mailer = GraphMailer(
        tenant_id=cfg.graph.tenant_id,
        client_id=cfg.graph.client_id,
        cert_thumbprint=cfg.graph.cert_thumbprint,
        cert_pem_path=cfg.graph.cert_pem_path,
        sender_user_id=cfg.email.sender_user_id,
        dry_run=cfg.email.dry_run,
    )
    sent = mailer.send_mail(
        to_recipients=recipients,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        custom_headers={"X-Grievance-Alert": "watchdog"},
    )
    print(
        json.dumps(
            {
                "ok": True,
                "recipient_count": len(recipients),
                "graph_message_id": sent.graph_message_id,
                "internet_message_id": sent.internet_message_id,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
