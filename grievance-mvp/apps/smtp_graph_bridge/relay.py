#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from urllib.parse import quote

import msal
import requests
import yaml
from aiosmtpd.controller import Controller

LOGGER = logging.getLogger("smtp_graph_bridge")
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


def _decode_header_value(raw_value: str | None) -> str:
    if not raw_value:
        return ""
    try:
        return str(make_header(decode_header(raw_value)))
    except Exception:
        return raw_value


def _addresses_from_headers(values: list[str] | None) -> list[str]:
    if not values:
        return []
    addresses = []
    for _, email_address in getaddresses(values):
        candidate = (email_address or "").strip()
        if candidate:
            addresses.append(candidate)
    return addresses


def _unique_addresses(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(normalized)
    return result


def _required(raw: dict[str, Any], key: str, context: str) -> str:
    value = str(raw.get(key, "")).strip()
    if not value:
        raise ValueError(f"Missing required config key '{context}.{key}'")
    return value


@dataclass(frozen=True)
class RelayConfig:
    tenant_id: str
    client_id: str
    cert_pem_path: Path
    cert_thumbprint: str
    sender_user_id: str

    @classmethod
    def load(cls, config_path: Path) -> "RelayConfig":
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        graph_raw = data.get("graph") or {}
        email_raw = data.get("email") or {}

        cert_path = Path(_required(graph_raw, "cert_pem_path", "graph"))
        if not cert_path.is_file():
            raise FileNotFoundError(f"Graph cert file not found: {cert_path}")

        sender_override = os.getenv("GRAPH_SENDER_USER_ID", "").strip()
        sender_user_id = sender_override or _required(email_raw, "sender_user_id", "email")
        return cls(
            tenant_id=_required(graph_raw, "tenant_id", "graph"),
            client_id=_required(graph_raw, "client_id", "graph"),
            cert_pem_path=cert_path,
            cert_thumbprint=_required(graph_raw, "cert_thumbprint", "graph"),
            sender_user_id=sender_user_id,
        )


class GraphMailer:
    def __init__(self, cfg: RelayConfig) -> None:
        self._cfg = cfg
        private_key = cfg.cert_pem_path.read_text(encoding="utf-8")
        self._app = msal.ConfidentialClientApplication(
            client_id=cfg.client_id,
            authority=f"https://login.microsoftonline.com/{cfg.tenant_id}",
            client_credential={
                "private_key": private_key,
                "thumbprint": cfg.cert_thumbprint,
            },
        )

    def _acquire_access_token(self) -> str:
        token = self._app.acquire_token_silent(scopes=GRAPH_SCOPE, account=None)
        if not token:
            token = self._app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        access_token = token.get("access_token")
        if access_token:
            return access_token
        error_summary = token.get("error_description") or json.dumps(token)
        raise RuntimeError(f"Graph token acquisition failed: {error_summary}")

    def send_message(self, message: dict[str, Any]) -> None:
        access_token = self._acquire_access_token()
        sender = quote(self._cfg.sender_user_id, safe="@")
        endpoint = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"message": message, "saveToSentItems": True},
            timeout=30,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Graph sendMail failed ({response.status_code}): {response.text[:500]}")


def _extract_body_and_attachments(parsed: Any) -> tuple[str, str, list[dict[str, Any]]]:
    body_html = ""
    body_text = ""
    attachments: list[dict[str, Any]] = []

    if parsed.is_multipart():
        for part in parsed.walk():
            if part.is_multipart():
                continue
            payload_bytes = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            disposition = (part.get_content_disposition() or "").lower()
            content_type = part.get_content_type() or "application/octet-stream"
            charset = part.get_content_charset() or "utf-8"

            if filename or disposition == "attachment":
                attachment_name = _decode_header_value(filename) or "attachment.bin"
                attachments.append(
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": attachment_name,
                        "contentType": content_type,
                        "contentBytes": base64.b64encode(payload_bytes).decode("ascii"),
                    }
                )
                continue

            decoded = payload_bytes.decode(charset, errors="replace")
            if content_type == "text/html" and not body_html:
                body_html = decoded
            elif content_type == "text/plain" and not body_text:
                body_text = decoded
    else:
        payload_bytes = parsed.get_payload(decode=True) or b""
        charset = parsed.get_content_charset() or "utf-8"
        decoded = payload_bytes.decode(charset, errors="replace")
        if parsed.get_content_type() == "text/html":
            body_html = decoded
        else:
            body_text = decoded

    if body_html:
        return "HTML", body_html, attachments
    return "Text", body_text, attachments


def _build_graph_message(parsed: Any, envelope_recipients: list[str]) -> dict[str, Any]:
    subject = _decode_header_value(parsed.get("Subject")) or "(no subject)"
    content_type, content, attachments = _extract_body_and_attachments(parsed)
    header_to = _unique_addresses(_addresses_from_headers(parsed.get_all("To", [])))
    header_cc = _unique_addresses(_addresses_from_headers(parsed.get_all("Cc", [])))
    envelope_all = _unique_addresses([value.strip() for value in envelope_recipients if value.strip()])

    if not header_to and not header_cc:
        header_to = envelope_all.copy()

    known_lower = {address.lower() for address in [*header_to, *header_cc]}
    header_bcc = [address for address in envelope_all if address.lower() not in known_lower]

    if not header_to and header_cc:
        header_to = header_cc
        header_cc = []

    if not header_to:
        raise ValueError("No recipients found in envelope or headers")

    message: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": content_type, "content": content},
        "toRecipients": [{"emailAddress": {"address": address}} for address in header_to],
    }
    if header_cc:
        message["ccRecipients"] = [{"emailAddress": {"address": address}} for address in header_cc]
    if header_bcc:
        message["bccRecipients"] = [{"emailAddress": {"address": address}} for address in header_bcc]
    if attachments:
        message["attachments"] = attachments

    reply_to_values = _addresses_from_headers(parsed.get_all("Reply-To", []))
    if reply_to_values:
        message["replyTo"] = [{"emailAddress": {"address": reply_to_values[0]}}]
    return message


class RelayHandler:
    def __init__(self, mailer: GraphMailer) -> None:
        self._mailer = mailer

    async def handle_DATA(self, _server: Any, _session: Any, envelope: Any) -> str:
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(envelope.content)
            message = _build_graph_message(parsed, envelope.rcpt_tos or [])
            self._mailer.send_message(message)
            recipients = envelope.rcpt_tos or []
            LOGGER.info("Relayed SMTP message to Graph for %s recipient(s)", len(recipients))
            return "250 Message accepted for relay"
        except Exception:
            LOGGER.exception("SMTP relay failed")
            return "451 Unable to relay message to Graph"


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config_path = Path(os.getenv("GRAPH_CONFIG_PATH", "/app/config/config.yaml"))
    cfg = RelayConfig.load(config_path)
    host = os.getenv("SMTP_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("SMTP_BIND_PORT", "1025"))

    controller = Controller(RelayHandler(GraphMailer(cfg)), hostname=host, port=port)
    controller.start()
    LOGGER.info("SMTP Graph bridge listening on %s:%s using sender %s", host, port, cfg.sender_user_id)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested")
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
