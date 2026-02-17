
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
class AppConfig:
    hmac_shared_secret: str
    db_path: str
    data_root: str
    docx_template_path: str
    libreoffice_timeout_seconds: int
    graph: GraphConfig
    docuseal: DocuSealConfig

@lru_cache
def load_config(path: str = "config/config.yaml") -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    docuseal_config = DocuSealConfig()

    return AppConfig(
        hmac_shared_secret=raw["hmac_shared_secret"],
        db_path=raw["db_path"],
        data_root=raw["data_root"],
        docx_template_path=raw["docx_template_path"],
        libreoffice_timeout_seconds=int(raw.get("libreoffice_timeout_seconds", 45)),
        graph=GraphConfig(**raw["graph"]),
        docuseal=docuseal_config,
    )
