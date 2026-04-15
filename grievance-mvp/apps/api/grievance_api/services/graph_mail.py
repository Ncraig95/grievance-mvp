from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path
import time
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

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict | None = None,
        data: str | bytes | None = None,
        content_type: str = "application/json",
    ) -> requests.Response:
        token = self.token()
        r = requests.request(
            method=method,
            url=f"https://graph.microsoft.com/v1.0{endpoint}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
            json=payload,
            data=data,
            timeout=self.timeout_seconds,
        )
        if 200 <= r.status_code < 300:
            return r
        raise RuntimeError(f"Graph mail call failed ({method} {endpoint}): {r.status_code} {r.text[:500]}")

    def _request_with_backoff(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict | None = None,
        data: str | bytes | None = None,
        content_type: str = "application/json",
        max_attempts: int = 4,
    ) -> requests.Response:
        last_error: RuntimeError | None = None
        for attempt in range(max_attempts):
            token = self.token()
            response = requests.request(
                method=method,
                url=f"https://graph.microsoft.com/v1.0{endpoint}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                },
                json=payload,
                data=data,
                timeout=self.timeout_seconds,
            )
            if 200 <= response.status_code < 300:
                return response
            retry_after = response.headers.get("Retry-After", "").strip()
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < max_attempts:
                delay_seconds = 2 ** attempt
                if retry_after.isdigit():
                    delay_seconds = max(delay_seconds, int(retry_after))
                time.sleep(delay_seconds)
                continue
            last_error = RuntimeError(
                f"Graph mail call failed ({method} {endpoint}): {response.status_code} {response.text[:500]}"
            )
            break
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Graph mail call failed without response ({method} {endpoint})")

    @staticmethod
    def _normalized_recipients(to_recipients: list[str]) -> list[str]:
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
        return unique_recipients

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

        unique_recipients = self._normalized_recipients(to_recipients)
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
        draft_resp = self._request_with_backoff("POST", f"/users/{sender}/messages", payload=message)
        draft = draft_resp.json()
        message_id = str(draft["id"])
        internet_message_id = draft.get("internetMessageId")

        self._request_with_backoff("POST", f"/users/{sender}/messages/{quote(message_id, safe='')}/send", payload={})
        return SentGraphMail(graph_message_id=message_id, internet_message_id=internet_message_id)

    def send_mime_mail(
        self,
        *,
        to_recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
        attachments: list[MailAttachment] | None = None,
        custom_headers: dict[str, str] | None = None,
        from_display_name: str | None = None,
        reply_to_address: str | None = None,
        reply_to_name: str | None = None,
    ) -> SentGraphMail:
        if not self.sender_user_id.strip():
            raise RuntimeError("outreach.sender_user_id is required for MIME mail delivery")

        unique_recipients = self._normalized_recipients(to_recipients)
        if not unique_recipients:
            raise RuntimeError("send_mime_mail has no valid recipients after normalization")

        if self.dry_run:
            synthetic = f"dryrun-mime-{quote(self.sender_user_id, safe='')}-{len(unique_recipients)}"
            return SentGraphMail(graph_message_id=synthetic, internet_message_id=None)

        message = EmailMessage()
        message["From"] = (
            formataddr((from_display_name, self.sender_user_id))
            if from_display_name
            else self.sender_user_id
        )
        message["To"] = ", ".join(unique_recipients)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False)
        if reply_to_address:
            message["Reply-To"] = (
                formataddr((reply_to_name, reply_to_address))
                if reply_to_name
                else reply_to_address
            )
        if custom_headers:
            for header_name, header_value in custom_headers.items():
                if not str(header_name or "").strip() or not str(header_value or "").strip():
                    continue
                message[str(header_name)] = str(header_value)

        message.set_content(text_body or "")
        if html_body:
            message.add_alternative(html_body, subtype="html")
        if attachments:
            for attachment in attachments:
                content_type = str(attachment.content_type or "application/octet-stream")
                maintype, _, subtype = content_type.partition("/")
                message.add_attachment(
                    attachment.content_bytes,
                    maintype=maintype or "application",
                    subtype=subtype or "octet-stream",
                    filename=attachment.filename,
                )

        mime_body = base64.b64encode(message.as_bytes()).decode("ascii")
        sender = quote(self.sender_user_id, safe="")
        draft_resp = self._request_with_backoff(
            "POST",
            f"/users/{sender}/messages",
            data=mime_body,
            content_type="text/plain",
        )
        draft = draft_resp.json()
        message_id = str(draft["id"])
        internet_message_id = draft.get("internetMessageId")
        self._request_with_backoff(
            "POST",
            f"/users/{sender}/messages/{quote(message_id, safe='')}/send",
            payload={},
        )
        return SentGraphMail(graph_message_id=message_id, internet_message_id=internet_message_id)
