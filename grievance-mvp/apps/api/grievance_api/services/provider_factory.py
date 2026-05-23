from __future__ import annotations

from dataclasses import dataclass

from ..core.config import AppConfig
from .docuseal_client import DocuSealClient
from .graph_mail import GraphMailer
from .local_providers import LocalDocuSealClient, LocalGraphMailer, LocalSharePointUploader
from .sharepoint_graph import GraphUploader


@dataclass(frozen=True)
class RuntimeProviders:
    docuseal: object
    graph: object
    mailer: object | None
    outreach_mailer: object | None


def app_is_local(cfg: AppConfig) -> bool:
    return str(getattr(cfg, "app_mode", "production") or "production").strip().lower() == "local"


def _real_provider_selected(provider: object | None) -> bool:
    if provider is None:
        return False
    return isinstance(provider, (DocuSealClient, GraphUploader, GraphMailer))


def assert_local_safe_providers(cfg: AppConfig, providers: RuntimeProviders) -> None:
    if not app_is_local(cfg):
        return
    selected = {
        "docuseal": providers.docuseal,
        "graph": providers.graph,
        "mailer": providers.mailer,
        "outreach_mailer": providers.outreach_mailer,
    }
    real = [name for name, provider in selected.items() if _real_provider_selected(provider)]
    if real:
        raise RuntimeError(f"APP_MODE=local selected real outbound provider(s): {', '.join(real)}")

    intake_auth = getattr(cfg, "intake_auth", None)
    if intake_auth and (
        str(getattr(intake_auth, "cloudflare_access_client_id", "") or "").strip()
        or str(getattr(intake_auth, "cloudflare_access_client_secret", "") or "").strip()
    ):
        raise RuntimeError("APP_MODE=local cannot enable Cloudflare Access intake auth")


def build_runtime_providers(cfg: AppConfig) -> RuntimeProviders:
    if app_is_local(cfg):
        mailer = None
        if cfg.email.enabled:
            mailer = LocalGraphMailer(data_root=cfg.data_root, sender_user_id=cfg.email.sender_user_id)
        outreach_mailer = None
        if cfg.outreach.enabled:
            outreach_mailer = LocalGraphMailer(data_root=cfg.data_root, sender_user_id=cfg.outreach.sender_user_id)
        providers = RuntimeProviders(
            docuseal=LocalDocuSealClient(data_root=cfg.data_root, public_base_url=cfg.docuseal.public_base_url),
            graph=LocalSharePointUploader(data_root=cfg.data_root),
            mailer=mailer,
            outreach_mailer=outreach_mailer,
        )
        assert_local_safe_providers(cfg, providers)
        return providers

    graph = GraphUploader(
        tenant_id=cfg.graph.tenant_id,
        client_id=cfg.graph.client_id,
        cert_thumbprint=cfg.graph.cert_thumbprint,
        cert_pem_path=cfg.graph.cert_pem_path,
        dry_run=cfg.email.dry_run,
    )
    mailer = None
    if cfg.email.enabled:
        mailer = GraphMailer(
            tenant_id=cfg.graph.tenant_id,
            client_id=cfg.graph.client_id,
            cert_thumbprint=cfg.graph.cert_thumbprint,
            cert_pem_path=cfg.graph.cert_pem_path,
            sender_user_id=cfg.email.sender_user_id,
            dry_run=cfg.email.dry_run,
        )
    outreach_mailer = None
    if cfg.outreach.enabled:
        outreach_mailer = GraphMailer(
            tenant_id=cfg.graph.tenant_id,
            client_id=cfg.graph.client_id,
            cert_thumbprint=cfg.graph.cert_thumbprint,
            cert_pem_path=cfg.graph.cert_pem_path,
            sender_user_id=cfg.outreach.sender_user_id,
            dry_run=cfg.email.dry_run,
        )

    return RuntimeProviders(
        docuseal=DocuSealClient(
            cfg.docuseal.base_url,
            cfg.docuseal.api_token,
            public_base_url=cfg.docuseal.public_base_url,
            web_base_url=cfg.docuseal.web_base_url,
            web_email=cfg.docuseal.web_email,
            web_password=cfg.docuseal.web_password,
            submitters_order=cfg.docuseal.submitters_order,
            submitters_order_by_form=cfg.docuseal.submitters_order_by_form,
            signature_layout_mode=cfg.docuseal.signature_layout_mode,
            signature_layout_mode_by_form=cfg.docuseal.signature_layout_mode_by_form,
            signature_table_trace_enabled=cfg.docuseal.signature_table_trace_enabled,
            signature_table_trace_by_form=cfg.docuseal.signature_table_trace_by_form,
            signature_table_guard_enabled=cfg.docuseal.signature_table_guard_enabled,
            signature_table_guard_tolerance=cfg.docuseal.signature_table_guard_tolerance,
            signature_table_guard_min_gap=cfg.docuseal.signature_table_guard_min_gap,
            signature_table_maps={
                form_key: {
                    cell_key: {
                        "page": int(cell.page),
                        "x": float(cell.x),
                        "y": float(cell.y),
                        "w": float(cell.w),
                        "h": float(cell.h),
                    }
                    for cell_key, cell in table_map.cells.items()
                }
                for form_key, table_map in cfg.docuseal.signature_table_maps.items()
            },
        ),
        graph=graph,
        mailer=mailer,
        outreach_mailer=outreach_mailer,
    )
