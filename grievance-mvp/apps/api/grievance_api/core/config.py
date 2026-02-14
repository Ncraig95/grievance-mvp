from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import yaml

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
class AppConfig:
    hmac_shared_secret: str
    db_path: str
    data_root: str
    docx_template_path: str
    libreoffice_timeout_seconds: int
    graph: GraphConfig
    docuseal: DocuSealConfig

def load_config(path: str) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))

    return AppConfig(
        hmac_shared_secret=raw["hmac_shared_secret"],
        db_path=raw["db_path"],
        data_root=raw["data_root"],
        docx_template_path=raw["docx_template_path"],
        libreoffice_timeout_seconds=int(raw.get("libreoffice_timeout_seconds", 45)),
        graph=GraphConfig(**raw["graph"]),
        docuseal=DocuSealConfig(**raw["docuseal"]),
    )
