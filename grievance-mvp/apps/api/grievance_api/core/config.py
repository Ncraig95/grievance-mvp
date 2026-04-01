from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_LOG = logging.getLogger(__name__)


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
    failed_processes_folder: str
    standalone_parent_folder: str = "Standalone Forms"


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
    strict_template_ids: bool = False
    submitters_order: str = "preserved"
    submitters_order_by_form: dict[str, str] = field(default_factory=dict)
    signature_layout_mode: str = "table_preferred"
    signature_layout_mode_by_form: dict[str, str] = field(default_factory=dict)
    signature_table_trace_enabled: bool = True
    signature_table_trace_by_form: dict[str, bool] = field(default_factory=dict)
    signature_table_guard_enabled: bool = True
    signature_table_guard_tolerance: float = 0.015
    signature_table_guard_min_gap: float = 0.005
    signature_table_maps: dict[str, "FormSignatureTableMap"] = field(default_factory=dict)


@dataclass(frozen=True)
class SignatureTableCell:
    page: int
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class FormSignatureTableMap:
    cells: dict[str, SignatureTableCell] = field(default_factory=dict)


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
    test_mode_by_form: dict[str, bool] = field(default_factory=dict)


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
    staged_flow_enabled: bool = False
    auto_advance: bool = False
    store_all_stage_artifacts: bool = False
    input_source: str = ""


@dataclass(frozen=True)
class IntakeAuthConfig:
    shared_header_name: str
    shared_header_value: str
    cloudflare_access_client_id: str
    cloudflare_access_client_secret: str


@dataclass(frozen=True)
class StandaloneSharepointStorageConfig:
    root_folder: str | None = None
    label_prefix: str = ""
    sequence_scope: str = "none"
    year_subfolders: bool = False
    upload_generated: bool = True
    upload_signed: bool = True
    upload_audit: bool = True


@dataclass(frozen=True)
class StandaloneFormConfig:
    template_path: str
    form_label: str
    sharepoint_folder_label: str
    signer_count: int = 1
    default_signer_email: str = ""
    sharepoint_storage: StandaloneSharepointStorageConfig = field(default_factory=StandaloneSharepointStorageConfig)


@dataclass(frozen=True)
class OfficerTrackingConfig:
    roster: tuple[str, ...] = ()


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


def _default_officer_tracking() -> OfficerTrackingConfig:
    return OfficerTrackingConfig()


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
    officer_tracking: OfficerTrackingConfig = field(default_factory=_default_officer_tracking)
    document_policies: dict[str, DocumentPolicyConfig] = field(default_factory=dict)
    standalone_forms: dict[str, StandaloneFormConfig] = field(default_factory=dict)
    docx_pdf_engine: str = "libreoffice"
    docx_pdf_graph_temp_folder: str = "_docx_pdf_convert"
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


def _as_bool_mapping(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}

    def _to_bool(raw: object) -> bool | None:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int):
            return raw != 0
        text = str(raw or "").strip().lower()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off"}:
            return False
        return None

    out: dict[str, bool] = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        parsed = _to_bool(v)
        if parsed is None:
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


def _normalize_docx_pdf_engine(value: object) -> str:
    engine = str(value or "libreoffice").strip().lower()
    if engine in {"libreoffice", "soffice"}:
        return "libreoffice"
    if engine in {"graph_word_online", "graph", "microsoft_word_online", "word_online"}:
        return "graph_word_online"
    return "libreoffice"


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _normalize_signature_layout_mode(value: object) -> str:
    mode = str(value or "table_preferred").strip().lower()
    if mode not in {"table_preferred", "generic"}:
        return "table_preferred"
    return mode


def _normalize_submitters_order(value: object) -> str:
    mode = str(value or "preserved").strip().lower()
    if mode not in {"preserved", "random"}:
        return "preserved"
    return mode


def _as_submitters_order_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = _normalize_submitters_order(v)
    return out


def _as_signature_layout_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = _normalize_signature_layout_mode(v)
    return out


def _is_valid_cell_key(raw_key: object) -> bool:
    key = str(raw_key or "").strip().lower()
    return bool(key) and bool(re.match(r"^signer\d+_(signature|date)$", key))


def _parse_signature_cell(raw: object) -> SignatureTableCell | None:
    if not isinstance(raw, dict):
        return None
    try:
        page = int(raw.get("page", 0))
        x = float(raw["x"])
        y = float(raw["y"])
        w = float(raw["w"])
        h = float(raw["h"])
    except Exception:
        return None
    if page < 0:
        return None
    if x < 0.0 or y < 0.0 or w <= 0.0 or h <= 0.0:
        return None
    if (x + w) > 1.0 or (y + h) > 1.0:
        return None
    return SignatureTableCell(page=page, x=x, y=y, w=w, h=h)


def _as_signature_table_maps(value: object) -> dict[str, FormSignatureTableMap]:
    if not isinstance(value, dict):
        return {}

    out: dict[str, FormSignatureTableMap] = {}
    for raw_form_key, raw_form_map in value.items():
        form_key = str(raw_form_key or "").strip()
        if not form_key:
            continue
        if not isinstance(raw_form_map, dict):
            _LOG.warning("config_signature_table_map_invalid_form", extra={"form_key": form_key})
            continue

        raw_cells = raw_form_map.get("cells", raw_form_map)
        if not isinstance(raw_cells, dict):
            _LOG.warning("config_signature_table_map_missing_cells", extra={"form_key": form_key})
            continue

        cells: dict[str, SignatureTableCell] = {}
        for raw_cell_key, raw_cell in raw_cells.items():
            cell_key = str(raw_cell_key or "").strip().lower()
            if not _is_valid_cell_key(cell_key):
                _LOG.warning(
                    "config_signature_table_map_invalid_cell_key",
                    extra={"form_key": form_key, "cell_key": str(raw_cell_key or "")},
                )
                continue

            parsed = _parse_signature_cell(raw_cell)
            if not parsed:
                _LOG.warning(
                    "config_signature_table_map_invalid_cell",
                    extra={"form_key": form_key, "cell_key": cell_key},
                )
                continue
            cells[cell_key] = parsed

        if not cells:
            _LOG.warning("config_signature_table_map_empty", extra={"form_key": form_key})
            continue

        out[form_key] = FormSignatureTableMap(cells=cells)
    return out


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


def _normalize_input_source(value: object) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"", "docuseal_fill_fields"}:
        return ""
    return mode


def _normalize_standalone_sequence_scope(value: object) -> str:
    scope = str(value or "none").strip().lower()
    if scope not in {"none", "yearly"}:
        return "none"
    return scope


def _parse_standalone_sharepoint_storage(
    raw_storage: object,
    *,
    form_label: str,
) -> StandaloneSharepointStorageConfig:
    storage = raw_storage if isinstance(raw_storage, dict) else {}
    root_folder = str(storage.get("root_folder", "")).strip() or None
    label_prefix = str(storage.get("label_prefix", "")).strip() or form_label
    return StandaloneSharepointStorageConfig(
        root_folder=root_folder,
        label_prefix=label_prefix,
        sequence_scope=_normalize_standalone_sequence_scope(storage.get("sequence_scope")),
        year_subfolders=bool(storage.get("year_subfolders", False)),
        upload_generated=bool(storage.get("upload_generated", True)),
        upload_signed=bool(storage.get("upload_signed", True)),
        upload_audit=bool(storage.get("upload_audit", True)),
    )


def _as_standalone_forms(value: object) -> dict[str, StandaloneFormConfig]:
    if not isinstance(value, dict):
        return {}

    out: dict[str, StandaloneFormConfig] = {}
    for raw_key, raw_form in value.items():
        key = str(raw_key or "").strip()
        if not key or not isinstance(raw_form, dict):
            continue

        template_path = str(raw_form.get("template_path", "")).strip()
        if not template_path:
            continue

        form_label = str(raw_form.get("form_label", "")).strip() or key
        sharepoint_folder_label = (
            str(raw_form.get("sharepoint_folder_label", "")).strip()
            or form_label
        )
        signer_count = max(1, int(raw_form.get("signer_count", 1)))
        default_signer_email = str(raw_form.get("default_signer_email", "")).strip()

        out[key] = StandaloneFormConfig(
            template_path=template_path,
            form_label=form_label,
            sharepoint_folder_label=sharepoint_folder_label,
            signer_count=signer_count,
            default_signer_email=default_signer_email,
            sharepoint_storage=_parse_standalone_sharepoint_storage(
                raw_form.get("sharepoint_storage"),
                form_label=form_label,
            ),
        )
    return out


def load_config(path: str) -> AppConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    docuseal_raw = raw.get("docuseal", {}) or {}
    graph_raw = raw.get("graph", {}) or {}
    email_raw = raw.get("email", {}) or {}
    grievance_raw = raw.get("grievance_id", {}) or {}
    intake_auth_raw = raw.get("intake_auth", {}) or {}
    rendering_raw = raw.get("rendering", {}) or {}
    officer_tracking_raw = raw.get("officer_tracking", {}) or {}
    document_policies_raw = raw.get("document_policies", {}) or {}
    standalone_forms_raw = raw.get("standalone_forms", {}) or {}

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
                staged_flow_enabled=bool(raw_policy.get("staged_flow_enabled", False)),
                auto_advance=bool(raw_policy.get("auto_advance", False)),
                store_all_stage_artifacts=bool(raw_policy.get("store_all_stage_artifacts", False)),
                input_source=_normalize_input_source(raw_policy.get("input_source")),
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
            failed_processes_folder=(
                str(graph_raw.get("failed_processes_folder", "config files/failed")).strip()
                or "config files/failed"
            ),
            standalone_parent_folder=(
                str(graph_raw.get("standalone_parent_folder", "Standalone Forms")).strip()
                or "Standalone Forms"
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
            strict_template_ids=bool(docuseal_raw.get("strict_template_ids", False)),
            submitters_order=_normalize_submitters_order(
                docuseal_raw.get("submitters_order", "preserved")
            ),
            submitters_order_by_form=_as_submitters_order_mapping(
                docuseal_raw.get("submitters_order_by_form")
            ),
            signature_layout_mode=_normalize_signature_layout_mode(
                docuseal_raw.get("signature_layout_mode", "table_preferred")
            ),
            signature_layout_mode_by_form=_as_signature_layout_mapping(
                docuseal_raw.get("signature_layout_mode_by_form")
            ),
            signature_table_trace_enabled=bool(docuseal_raw.get("signature_table_trace_enabled", True)),
            signature_table_trace_by_form=_as_bool_mapping(docuseal_raw.get("signature_table_trace_by_form")),
            signature_table_guard_enabled=bool(docuseal_raw.get("signature_table_guard_enabled", True)),
            signature_table_guard_tolerance=max(
                0.0,
                _as_float(docuseal_raw.get("signature_table_guard_tolerance", 0.015), 0.015),
            ),
            signature_table_guard_min_gap=max(
                0.0,
                _as_float(docuseal_raw.get("signature_table_guard_min_gap", 0.005), 0.005),
            ),
            signature_table_maps=_as_signature_table_maps(docuseal_raw.get("signature_table_maps")),
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
            test_mode_by_form=_as_bool_mapping(email_raw.get("test_mode_by_form")),
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
        officer_tracking=OfficerTrackingConfig(
            roster=_as_str_tuple(officer_tracking_raw.get("roster")),
        ),
        document_policies=parsed_document_policies,
        standalone_forms=_as_standalone_forms(standalone_forms_raw),
        docx_pdf_engine=_normalize_docx_pdf_engine(raw.get("docx_pdf_engine")),
        docx_pdf_graph_temp_folder=(
            str(raw.get("docx_pdf_graph_temp_folder", "_docx_pdf_convert")).strip()
            or "_docx_pdf_convert"
        ),
        wait_for_grievance_number_before_signature=bool(
            raw.get("wait_for_grievance_number_before_signature", True)
        ),
        require_approver_decision=bool(raw.get("require_approver_decision", True)),
        log_level=_normalize_log_level(raw.get("log_level")),
    )
