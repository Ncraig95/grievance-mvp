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


def validate_external_steward_auth_config(cfg: ExternalStewardAuthConfig) -> None:
    if not cfg.enabled:
        return

    required = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri,
        "post_logout_redirect_uri": cfg.post_logout_redirect_uri,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"external_steward_auth enabled but missing required values: {', '.join(missing)}")
    if not str(cfg.authority or "").strip() and not str(cfg.discovery_url or "").strip():
        raise RuntimeError("external_steward_auth enabled but both authority and discovery_url are empty")
