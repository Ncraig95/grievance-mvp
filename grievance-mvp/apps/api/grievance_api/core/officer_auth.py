from __future__ import annotations

from .config import ExternalStewardAuthConfig, OfficerAuthConfig


def validate_officer_auth_config(cfg: OfficerAuthConfig) -> None:
    if not cfg.enabled:
        return

    required = {
        "tenant_id": cfg.tenant_id,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri,
        "post_logout_redirect_uri": cfg.post_logout_redirect_uri,
        "session_secret": cfg.session_secret,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"officer_auth enabled but missing required values: {', '.join(missing)}")

    if not cfg.officer_group_ids:
        raise RuntimeError("officer_auth enabled but officer_group_ids is empty")
    if not cfg.admin_group_ids:
        raise RuntimeError("officer_auth enabled but admin_group_ids is empty")

    for scope_key, scope_cfg in cfg.chief_steward_contract_scopes.items():
        if not scope_cfg.contract_aliases:
            raise RuntimeError(
                f"officer_auth chief_steward_contract_scopes.{scope_key}.contract_aliases is empty"
            )


def validate_external_steward_auth_config(
    cfg: ExternalStewardAuthConfig,
    officer_cfg: OfficerAuthConfig | None = None,
) -> None:
    if not cfg.enabled:
        return

    officer_cfg = officer_cfg or OfficerAuthConfig()
    tenant_id = str(cfg.tenant_id or "").strip()
    client_id = str(cfg.client_id or "").strip()
    client_secret = str(cfg.client_secret or "").strip()
    redirect_uri = str(cfg.redirect_uri or "").strip()
    post_logout_redirect_uri = str(cfg.post_logout_redirect_uri or "").strip()
    if cfg.reuse_officer_auth_app:
        tenant_id = tenant_id or str(officer_cfg.tenant_id or "").strip()
        client_id = client_id or str(officer_cfg.client_id or "").strip()
        client_secret = client_secret or str(officer_cfg.client_secret or "").strip()
        redirect_uri = redirect_uri or str(officer_cfg.redirect_uri or "").strip()
        post_logout_redirect_uri = post_logout_redirect_uri or str(officer_cfg.post_logout_redirect_uri or "").strip()

    required = {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"external_steward_auth enabled but missing required values: {', '.join(missing)}")
