from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    audit_backup_subfolders: tuple[str, ...]
    audit_local_backup_roots: tuple[str, ...]
    client_supplied_subfolder: str


@dataclass(frozen=True)
class DocuSealConfig:
    base_url: str
    api_token: str
    webhook_secret: str
    public_base_url: str | None
    web_base_url: str | None
    web_email: str | None
    web_password: str | None
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
    test_mode: bool = False


@dataclass(frozen=True)
class GrievanceIdConfig:
    mode: str
    timezone: str
    min_width: int
    separator: str


@dataclass(frozen=True)
class LayoutPolicyConfig:
    enabled: bool
    grievance_number_fallback: str | None
    single_line_ellipsis: bool
    max_chars: dict[str, int]


@dataclass(frozen=True)
class RenderingConfig:
    normalize_split_placeholders: bool
    layout_policies: dict[str, LayoutPolicyConfig]


@dataclass(frozen=True)
class DocumentPolicyConfig:
    folder_resolution: str
    default_signer_field: str
    default_requires_signature: bool


@dataclass(frozen=True)
class IntakeAuthConfig:
    shared_header_name: str
    shared_header_value: str
    cloudflare_access_client_id: str
    cloudflare_access_client_secret: str


def _default_intake_auth() -> IntakeAuthConfig:
    return IntakeAuthConfig(
        shared_header_name="X-Intake-Key",
        shared_header_value="",
        cloudflare_access_client_id="",
        cloudflare_access_client_secret="",
    )


def _default_rendering() -> RenderingConfig:
    return RenderingConfig(
        normalize_split_placeholders=True,
        layout_policies={},
    )


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
    intake_auth: IntakeAuthConfig = field(default_factory=_default_intake_auth)
    rendering: RenderingConfig = field(default_factory=_default_rendering)
    document_policies: dict[str, DocumentPolicyConfig] = field(default_factory=dict)
    wait_for_grievance_number_before_signature: bool = True
    require_approver_decision: bool = True
    log_level: str = "INFO"


def _as_recipients(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    return ()


def _as_str_tuple(value: object) -> tuple[str, ...]:
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


def _as_int_value_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        try:
            parsed = int(v)
        except Exception:
            continue
        if parsed <= 0:
            continue
        out[key] = parsed
    return out


def _normalize_delivery_mode(value: object) -> str:
    mode = str(value or "sharepoint_link").strip().lower()
    if mode not in {"sharepoint_link", "attach_pdf"}:
        return "sharepoint_link"
    return mode


def _normalize_log_level(value: object) -> str:
    level = str(value or "INFO").strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return "INFO"
    return level


def _normalize_grievance_mode(value: object) -> str:
    mode = str(value or "auto").strip().lower()
    if mode not in {"auto", "manual"}:
        return "auto"
    return mode


def _normalize_grievance_separator(value: object) -> str:
    # Current policy is no separator in produced IDs (YYYY + sequence).
    _ = value
    return ""


def _normalize_grievance_fallback(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"grievance_id", "none", ""}:
        return "grievance_id" if text == "grievance_id" else None
    return None


def _normalize_folder_resolution(value: object) -> str:
    mode = str(value or "default").strip().lower()
    if mode not in {"default", "existing_exact_grievance_id"}:
        return "default"
    return mode


def load_config(path: str) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    docuseal_raw = raw.get("docuseal", {}) or {}
    graph_raw = raw.get("graph", {}) or {}
    email_raw = raw.get("email", {}) or {}
    grievance_raw = raw.get("grievance_id", {}) or {}
    intake_auth_raw = raw.get("intake_auth", {}) or {}
    rendering_raw = raw.get("rendering", {}) or {}
    document_policies_raw = raw.get("document_policies", {}) or {}

    sender_user_id = str(email_raw.get("sender_user_id", "")).strip()
    raw_layout_policies = rendering_raw.get("layout_policies", {})
    parsed_layout_policies: dict[str, LayoutPolicyConfig] = {}
    if isinstance(raw_layout_policies, dict):
        for raw_key, raw_policy in raw_layout_policies.items():
            key = str(raw_key).strip()
            if not key or not isinstance(raw_policy, dict):
                continue
            parsed_layout_policies[key] = LayoutPolicyConfig(
                enabled=bool(raw_policy.get("enabled", False)),
                grievance_number_fallback=_normalize_grievance_fallback(
                    raw_policy.get("grievance_number_fallback")
                ),
                single_line_ellipsis=bool(raw_policy.get("single_line_ellipsis", False)),
                max_chars=_as_int_value_mapping(raw_policy.get("max_chars")),
            )
    parsed_document_policies: dict[str, DocumentPolicyConfig] = {}
    if isinstance(document_policies_raw, dict):
        for raw_key, raw_policy in document_policies_raw.items():
            key = str(raw_key).strip()
            if not key or not isinstance(raw_policy, dict):
                continue
            parsed_document_policies[key] = DocumentPolicyConfig(
                folder_resolution=_normalize_folder_resolution(raw_policy.get("folder_resolution")),
                default_signer_field=str(raw_policy.get("default_signer_field", "")).strip(),
                default_requires_signature=bool(raw_policy.get("default_requires_signature", True)),
            )

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
            audit_backup_subfolders=_as_str_tuple(graph_raw.get("audit_backup_subfolders")),
            audit_local_backup_roots=_as_str_tuple(graph_raw.get("audit_local_backup_roots")),
            client_supplied_subfolder=(
                str(graph_raw.get("client_supplied_subfolder", "Client supplied data")).strip()
                or "Client supplied data"
            ),
        ),
        docuseal=DocuSealConfig(
            base_url=str(docuseal_raw["base_url"]).strip(),
            api_token=str(docuseal_raw["api_token"]).strip(),
            webhook_secret=str(docuseal_raw["webhook_secret"]).strip(),
            public_base_url=(str(docuseal_raw.get("public_base_url", "")).strip() or None),
            web_base_url=(
                str(docuseal_raw.get("web_base_url", "")).strip()
                or os.getenv("DOCUSEAL_WEB_BASE_URL", "").strip()
                or str(docuseal_raw.get("public_base_url", "")).strip()
                or None
            ),
            web_email=(
                str(docuseal_raw.get("web_email", "")).strip()
                or os.getenv("DOCUSEAL_WEB_EMAIL", "").strip()
                or os.getenv("DOCUSEAL_ADMIN_EMAIL", "").strip()
                or None
            ),
            web_password=(
                str(docuseal_raw.get("web_password", "")).strip()
                or os.getenv("DOCUSEAL_WEB_PASSWORD", "").strip()
                or os.getenv("DOCUSEAL_ADMIN_PASSWORD", "").strip()
                or None
            ),
            default_template_id=(
                int(docuseal_raw["default_template_id"])
                if docuseal_raw.get("default_template_id") is not None
                else None
            ),
            template_ids=_as_int_mapping(docuseal_raw.get("template_ids")),
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
            test_mode=bool(email_raw.get("test_mode", False)),
        ),
        grievance_id=GrievanceIdConfig(
            mode=_normalize_grievance_mode(grievance_raw.get("mode")),
            timezone=str(grievance_raw.get("timezone", "America/New_York")).strip() or "America/New_York",
            min_width=max(1, int(grievance_raw.get("min_width", 3))),
            separator=_normalize_grievance_separator(grievance_raw.get("separator")),
        ),
        intake_auth=IntakeAuthConfig(
            shared_header_name=(
                str(intake_auth_raw.get("shared_header_name", "X-Intake-Key")).strip() or "X-Intake-Key"
            ),
            shared_header_value=(
                str(intake_auth_raw.get("shared_header_value", "")).strip()
                or os.getenv("INTAKE_SHARED_HEADER_VALUE", "").strip()
            ),
            cloudflare_access_client_id=(
                str(intake_auth_raw.get("cloudflare_access_client_id", "")).strip()
                or os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
            ),
            cloudflare_access_client_secret=(
                str(intake_auth_raw.get("cloudflare_access_client_secret", "")).strip()
                or os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()
            ),
        ),
        rendering=RenderingConfig(
            normalize_split_placeholders=bool(rendering_raw.get("normalize_split_placeholders", True)),
            layout_policies=parsed_layout_policies,
        ),
        document_policies=parsed_document_policies,
        wait_for_grievance_number_before_signature=bool(
            raw.get("wait_for_grievance_number_before_signature", True)
        ),
        require_approver_decision=bool(raw.get("require_approver_decision", True)),
        log_level=_normalize_log_level(raw.get("log_level")),
    )
