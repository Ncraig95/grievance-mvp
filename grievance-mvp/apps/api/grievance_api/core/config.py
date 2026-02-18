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
    case_parent_folder: str
    generated_subfolder: str
    signed_subfolder: str
    audit_subfolder: str


@dataclass(frozen=True)
class DocuSealConfig:
    base_url: str
    api_token: str
    webhook_secret: str
    public_base_url: str | None
    default_template_id: int | None
    template_ids: dict[str, int]


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
    dry_run: bool


@dataclass(frozen=True)
class GrievanceIdConfig:
    mode: str
    timezone: str
    min_width: int
    separator: str


@dataclass(frozen=True)
class AppConfig:
    hmac_shared_secret: str
    db_path: str
    data_root: str
    docx_template_path: str
    doc_templates: dict[str, str]
    libreoffice_timeout_seconds: int
    graph: GraphConfig
    docuseal: DocuSealConfig
    email: EmailConfig
    grievance_id: GrievanceIdConfig


def _as_recipients(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    return ()


def _as_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key = str(k).strip()
        val = str(v).strip()
        if key and val:
            out[key] = val
    return out


def _as_int_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        try:
            out[key] = int(v)
        except Exception:
            continue
    return out


def _normalize_delivery_mode(value: object) -> str:
    mode = str(value or "sharepoint_link").strip().lower()
    if mode not in {"sharepoint_link", "attach_pdf"}:
        return "sharepoint_link"
    return mode


def _normalize_grievance_mode(value: object) -> str:
    mode = str(value or "auto").strip().lower()
    if mode not in {"auto", "manual"}:
        return "auto"
    return mode


def _normalize_grievance_separator(value: object) -> str:
    # Current policy is no separator in produced IDs (YYYY + sequence).
    _ = value
    return ""


def load_config(path: str) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    graph_raw = raw.get("graph", {}) or {}
    email_raw = raw.get("email", {}) or {}
    grievance_raw = raw.get("grievance_id", {}) or {}

    sender_user_id = str(email_raw.get("sender_user_id", "")).strip()

    return AppConfig(
        hmac_shared_secret=str(raw.get("hmac_shared_secret", "")).strip(),
        db_path=str(raw["db_path"]),
        data_root=str(raw["data_root"]),
        docx_template_path=str(raw["docx_template_path"]),
        doc_templates=_as_mapping(raw.get("doc_templates")),
        libreoffice_timeout_seconds=int(raw.get("libreoffice_timeout_seconds", 45)),
        graph=GraphConfig(
            tenant_id=str(graph_raw.get("tenant_id", "")).strip(),
            client_id=str(graph_raw.get("client_id", "")).strip(),
            cert_pem_path=str(graph_raw.get("cert_pem_path", "")).strip(),
            cert_thumbprint=str(graph_raw.get("cert_thumbprint", "")).strip(),
            site_hostname=str(graph_raw.get("site_hostname", "")).strip(),
            site_path=str(graph_raw.get("site_path", "")).strip(),
            document_library=str(graph_raw.get("document_library", "Documents")).strip() or "Documents",
            case_parent_folder=str(graph_raw.get("case_parent_folder", "Grievances")).strip() or "Grievances",
            generated_subfolder=str(graph_raw.get("generated_subfolder", "Generated")).strip() or "Generated",
            signed_subfolder=str(graph_raw.get("signed_subfolder", "Signed")).strip() or "Signed",
            audit_subfolder=str(graph_raw.get("audit_subfolder", "Audit")).strip() or "Audit",
        ),
        docuseal=DocuSealConfig(
            base_url=str(raw["docuseal"]["base_url"]).strip(),
            api_token=str(raw["docuseal"]["api_token"]).strip(),
            webhook_secret=str(raw["docuseal"]["webhook_secret"]).strip(),
            public_base_url=(str(raw["docuseal"].get("public_base_url", "")).strip() or None),
            default_template_id=(
                int(raw["docuseal"]["default_template_id"])
                if raw["docuseal"].get("default_template_id") is not None
                else None
            ),
            template_ids=_as_int_mapping(raw["docuseal"].get("template_ids")),
        ),
        email=EmailConfig(
            enabled=bool(email_raw.get("enabled", bool(sender_user_id))),
            sender_user_id=sender_user_id,
            templates_dir=str(email_raw.get("templates_dir", "/app/templates/email")).strip(),
            internal_recipients=_as_recipients(email_raw.get("internal_recipients")),
            derek_email=(str(email_raw.get("derek_email", "")).strip() or None),
            approval_request_url_base=(str(email_raw.get("approval_request_url_base", "")).strip() or None),
            allow_signer_copy_link=bool(email_raw.get("allow_signer_copy_link", False)),
            artifact_delivery_mode=_normalize_delivery_mode(email_raw.get("artifact_delivery_mode")),
            max_attachment_bytes=int(email_raw.get("max_attachment_bytes", 2_000_000)),
            resend_cooldown_seconds=int(email_raw.get("resend_cooldown_seconds", 300)),
            dry_run=bool(email_raw.get("dry_run", False)),
        ),
        grievance_id=GrievanceIdConfig(
            mode=_normalize_grievance_mode(grievance_raw.get("mode")),
            timezone=str(grievance_raw.get("timezone", "America/New_York")).strip() or "America/New_York",
            min_width=max(1, int(grievance_raw.get("min_width", 3))),
            separator=_normalize_grievance_separator(grievance_raw.get("separator")),
        ),
    )
