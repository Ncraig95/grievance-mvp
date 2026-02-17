from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import msal
import requests


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    content_type: str
    content_bytes: bytes


@dataclass(frozen=True)
class SentGraphMail:
    graph_message_id: str
    internet_message_id: str | None


class GraphMailer:
    """App-only Graph mail sender that returns stable message ids for audits."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        cert_thumbprint: str,
        cert_pem_path: str,
        sender_user_id: str,
        timeout_seconds: int = 30,
        dry_run: bool = False,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.cert_thumbprint = cert_thumbprint
        self.cert_pem_path = cert_pem_path
        self.sender_user_id = sender_user_id
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._app: msal.ConfidentialClientApplication | None = None

    def _load_cert_credential(self) -> dict:
        pem = Path(self.cert_pem_path).read_text(encoding="utf-8")
        return {"private_key": pem, "thumbprint": self.cert_thumbprint}

    def token(self) -> str:
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                authority=self._authority,
                client_credential=self._load_cert_credential(),
            )
        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            err = result.get("error")
            desc = result.get("error_description")
            raise RuntimeError(f"Graph token failure: {err} {desc}")
        return str(result["access_token"])

    def _request(self, method: str, endpoint: str, *, payload: dict | None = None) -> requests.Response:
        token = self.token()
        r = requests.request(
            method=method,
            url=f"https://graph.microsoft.com/v1.0{endpoint}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        if 200 <= r.status_code < 300:
            return r
        raise RuntimeError(f"Graph mail call failed ({method} {endpoint}): {r.status_code} {r.text[:500]}")

    def send_mail(
        self,
        *,
        to_recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
        attachments: list[MailAttachment] | None = None,
        custom_headers: dict[str, str] | None = None,
    ) -> SentGraphMail:
        if not self.sender_user_id.strip():
            raise RuntimeError("email.sender_user_id is required for Graph mail delivery")

        unique_recipients: list[str] = []
        seen: set[str] = set()
        for raw in to_recipients:
            email = raw.strip()
            if not email:
                continue
            lowered = email.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            unique_recipients.append(email)
        if not unique_recipients:
            raise RuntimeError("send_mail has no valid recipients after normalization")

        if self.dry_run:
            synthetic = f"dryrun-{quote(self.sender_user_id, safe='')}-{len(unique_recipients)}"
            return SentGraphMail(graph_message_id=synthetic, internet_message_id=None)

        message: dict = {
            "subject": subject,
            "body": {
                "contentType": "HTML" if html_body else "Text",
                "content": html_body if html_body else text_body,
            },
            "toRecipients": [{"emailAddress": {"address": email}} for email in unique_recipients],
        }
        if custom_headers:
            message["internetMessageHeaders"] = [{"name": k, "value": v} for k, v in custom_headers.items()]
        if attachments:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": a.filename,
                    "contentType": a.content_type,
                    "contentBytes": base64.b64encode(a.content_bytes).decode("ascii"),
                }
                for a in attachments
            ]

        sender = quote(self.sender_user_id, safe="")
        draft_resp = self._request("POST", f"/users/{sender}/messages", payload=message)
        draft = draft_resp.json()
        message_id = str(draft["id"])
        internet_message_id = draft.get("internetMessageId")

        self._request("POST", f"/users/{sender}/messages/{quote(message_id, safe='')}/send", payload={})
        return SentGraphMail(graph_message_id=message_id, internet_message_id=internet_message_id)
