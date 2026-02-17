
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import yaml

from pydantic_settings import BaseSettings, SettingsConfigDict

class DocuSealConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="docuseal_",
        case_sensitive=False,
        env_file=".env",
        extra="ignore",
    )
    api_token: str
    host: str = "127.0.0.1"
    port: int = 3000
    protocol: str = "http"
    webhook_secret: str
    db_user: str = "docuseal"
    db_password: str
    db_name: str = "docuseal"
    image: str = "docuseal/docuseal:latest"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

@dataclass(frozen=True)
class GraphConfig:
    tenant_id: str
    client_id: str
    cert_pem_path: str
    cert_thumbprint: str
    site_hostname: str
    site_path: str
    document_library: str

@dataclass(frozen=True)
class DocuSealConfig:
    base_url: str
    api_token: str
    webhook_secret: str

@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    sender_user_id: str
    templates_dir: str
    internal_recipients: tuple[str, ...]
    derek_email: str | None
    approval_request_url_base: str | None
    allow_signer_copy_link: bool
    artifact_delivery_mode: str
    max_attachment_bytes: int
    resend_cooldown_seconds: int

@dataclass(frozen=True)
class AppConfig:
    hmac_shared_secret: str
    db_path: str
    data_root: str
    docx_template_path: str
    libreoffice_timeout_seconds: int
    graph: GraphConfig
    docuseal: DocuSealConfig
    email: EmailConfig

def _as_recipients(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    return ()

def load_config(path: str) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    email_raw = raw.get("email", {}) or {}
    sender_user_id = str(email_raw.get("sender_user_id", "")).strip()
    artifact_delivery_mode = str(email_raw.get("artifact_delivery_mode", "sharepoint_link")).strip().lower()
    if artifact_delivery_mode not in {"sharepoint_link", "attach_pdf"}:
        artifact_delivery_mode = "sharepoint_link"

    return AppConfig(
        hmac_shared_secret=raw["hmac_shared_secret"],
        db_path=raw["db_path"],
        data_root=raw["data_root"],
        docx_template_path=raw["docx_template_path"],
        libreoffice_timeout_seconds=int(raw.get("libreoffice_timeout_seconds", 45)),
        graph=GraphConfig(**raw["graph"]),
        docuseal=DocuSealConfig(**raw["docuseal"]),
        email=EmailConfig(
            enabled=bool(email_raw.get("enabled", bool(sender_user_id))),
            sender_user_id=sender_user_id,
            templates_dir=str(email_raw.get("templates_dir", "/app/templates/email")),
            internal_recipients=_as_recipients(email_raw.get("internal_recipients")),
            derek_email=(str(email_raw.get("derek_email", "")).strip() or None),
            approval_request_url_base=(str(email_raw.get("approval_request_url_base", "")).strip() or None),
            allow_signer_copy_link=bool(email_raw.get("allow_signer_copy_link", False)),
            artifact_delivery_mode=artifact_delivery_mode,
            max_attachment_bytes=int(email_raw.get("max_attachment_bytes", 2_000_000)),
            resend_cooldown_seconds=int(email_raw.get("resend_cooldown_seconds", 300)),
        ),
    )
