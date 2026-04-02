from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import msal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..core.config import AppConfig
from .admin_common import require_local_access

router = APIRouter()

_OIDC_SCOPES = ["openid", "profile", "email"]
_SESSION_USER_KEY = "officer_user"
_SESSION_FLOW_KEY = "officer_auth_flow"
_SESSION_NEXT_KEY = "officer_auth_next"
_ROLE_READ_ONLY = "read_only"
_ROLE_OFFICER = "officer"
_ROLE_CHIEF_STEWARD = "chief_steward"
_ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class OfficerUserContext:
    user_id: str | None
    email: str | None
    display_name: str | None
    role: str
    contract_scopes: tuple[str, ...]
    group_ids: tuple[str, ...]
    auth_enabled: bool
    can_create: bool
    can_edit: bool
    can_delete: bool
    can_bulk_edit: bool
    can_bulk_delete: bool
    can_view_audit: bool
    can_manage_chief_assignments: bool


def officer_auth_enabled(cfg: AppConfig) -> bool:
    return bool(getattr(cfg, "officer_auth", None) and cfg.officer_auth.enabled)


def _normalize_group_id(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_scope_key(value: object) -> str:
    text = str(value or "").strip().lower()
    chars: list[str] = []
    last_was_sep = False
    for ch in text:
        if ch.isalnum():
            chars.append(ch)
            last_was_sep = False
        elif not last_was_sep:
            chars.append("_")
            last_was_sep = True
    return "".join(chars).strip("_")


def _session(request: Request) -> dict[str, Any]:
    session = getattr(request, "session", None)
    return session if isinstance(session, dict) else {}


def _sanitize_next_path(value: object, *, default: str = "/officers") -> str:
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return default
    return text


def _login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/auth/login?next={quote(next_path, safe='/?=&')}", status_code=303)


def _local_read_only_user() -> OfficerUserContext:
    return OfficerUserContext(
        user_id=None,
        email=None,
        display_name="Local Read Only",
        role=_ROLE_READ_ONLY,
        contract_scopes=(),
        group_ids=(),
        auth_enabled=False,
        can_create=False,
        can_edit=False,
        can_delete=False,
        can_bulk_edit=False,
        can_bulk_delete=False,
        can_view_audit=False,
        can_manage_chief_assignments=False,
    )


def _local_ops_admin_user() -> OfficerUserContext:
    return OfficerUserContext(
        user_id=None,
        email=None,
        display_name="Local Admin",
        role=_ROLE_ADMIN,
        contract_scopes=(),
        group_ids=(),
        auth_enabled=False,
        can_create=False,
        can_edit=False,
        can_delete=False,
        can_bulk_edit=False,
        can_bulk_delete=False,
        can_view_audit=False,
        can_manage_chief_assignments=False,
    )


def _role_flags(role: str) -> tuple[bool, bool, bool, bool, bool]:
    if role == _ROLE_ADMIN:
        return True, True, True, True, True
    if role == _ROLE_CHIEF_STEWARD:
        return False, True, False, True, False
    return False, False, False, False, False


def _contract_scope_lookup(cfg: AppConfig) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for scope_key, scope_cfg in cfg.officer_auth.chief_steward_contract_scopes.items():
        lookup[normalize_scope_key(scope_key)] = scope_key
        for alias in scope_cfg.contract_aliases:
            normalized = normalize_scope_key(alias)
            if normalized:
                lookup[normalized] = scope_key
    return lookup


def resolve_contract_scope(cfg: AppConfig, contract_value: object) -> str | None:
    normalized = normalize_scope_key(contract_value)
    if not normalized:
        return None
    return _contract_scope_lookup(cfg).get(normalized)


async def _assigned_chief_scopes(db, *, user_id: str | None, email: str | None) -> tuple[str, ...]:  # noqa: ANN001
    clauses: list[str] = []
    params: list[object] = []
    if user_id:
        clauses.append("lower(COALESCE(principal_id, '')) = lower(?)")
        params.append(user_id)
    if email:
        clauses.append("lower(COALESCE(principal_email, '')) = lower(?)")
        params.append(email)
    if not clauses:
        return ()

    rows = await db.fetchall(
        f"""
        SELECT contract_scope
        FROM chief_steward_assignments
        WHERE {' OR '.join(clauses)}
        ORDER BY contract_scope
        """,
        tuple(params),
    )
    return tuple(
        sorted(
            {
                normalize_scope_key(row[0])
                for row in rows
                if normalize_scope_key(row[0])
            }
        )
    )


def _chief_scopes_from_groups(cfg: AppConfig, *, group_ids: set[str]) -> tuple[str, ...]:
    chief_scopes: set[str] = set()
    for scope_key, scope_cfg in cfg.officer_auth.chief_steward_contract_scopes.items():
        scope_group_ids = {
            _normalize_group_id(value) for value in scope_cfg.group_ids if _normalize_group_id(value)
        }
        if scope_group_ids.intersection(group_ids):
            chief_scopes.add(scope_key)
    return tuple(sorted(chief_scopes))


def _resolve_user_context(
    cfg: AppConfig,
    *,
    user_id: str | None,
    email: str | None,
    display_name: str | None,
    group_ids: set[str],
    assigned_chief_scopes: tuple[str, ...],
) -> OfficerUserContext:
    admin_group_ids = {_normalize_group_id(value) for value in cfg.officer_auth.admin_group_ids if _normalize_group_id(value)}
    officer_group_ids = {
        _normalize_group_id(value) for value in cfg.officer_auth.officer_group_ids if _normalize_group_id(value)
    }
    chief_scope_set = set(_chief_scopes_from_groups(cfg, group_ids=group_ids)).union(assigned_chief_scopes)

    if admin_group_ids.intersection(group_ids):
        role = _ROLE_ADMIN
        contract_scopes = tuple(sorted(cfg.officer_auth.chief_steward_contract_scopes))
    elif chief_scope_set:
        role = _ROLE_CHIEF_STEWARD
        contract_scopes = tuple(sorted(chief_scope_set))
    elif officer_group_ids.intersection(group_ids):
        role = _ROLE_OFFICER
        contract_scopes = ()
    else:
        raise HTTPException(status_code=403, detail="signed-in user is not authorized for officer access")

    can_create, can_edit, can_delete, can_bulk_edit, can_bulk_delete = _role_flags(role)
    return OfficerUserContext(
        user_id=user_id,
        email=email,
        display_name=display_name,
        role=role,
        contract_scopes=contract_scopes,
        group_ids=tuple(sorted(group_ids)),
        auth_enabled=True,
        can_create=can_create,
        can_edit=can_edit,
        can_delete=can_delete,
        can_bulk_edit=can_bulk_edit,
        can_bulk_delete=can_bulk_delete,
        can_view_audit=(role == _ROLE_ADMIN),
        can_manage_chief_assignments=(role == _ROLE_ADMIN),
    )


async def _resolve_user_context_from_claims(request: Request, *, claims: dict[str, Any]) -> OfficerUserContext:
    cfg: AppConfig = request.app.state.cfg
    groups = claims.get("groups")
    if not isinstance(groups, list):
        if claims.get("_claim_names") or claims.get("hasgroups"):
            raise HTTPException(
                status_code=403,
                detail="Microsoft Entra group overage is not supported for officer access; keep group claims in the ID token",
            )
        raise HTTPException(status_code=403, detail="Microsoft Entra ID token is missing required group claims")

    group_ids = {_normalize_group_id(value) for value in groups if _normalize_group_id(value)}
    user_id = str(claims.get("oid") or claims.get("sub") or "").strip() or None
    email = (
        str(claims.get("preferred_username") or "").strip()
        or str(claims.get("email") or "").strip()
        or str(claims.get("upn") or "").strip()
        or None
    )
    display_name = str(claims.get("name") or "").strip() or email
    assigned_chief_scopes = await _assigned_chief_scopes(request.app.state.db, user_id=user_id, email=email)
    return _resolve_user_context(
        cfg,
        user_id=user_id,
        email=email,
        display_name=display_name,
        group_ids=group_ids,
        assigned_chief_scopes=assigned_chief_scopes,
    )


def _session_user_payload(user: OfficerUserContext, *, exp: int | None) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "contract_scopes": list(user.contract_scopes),
        "group_ids": list(user.group_ids),
        "exp": int(exp or 0),
    }


async def current_officer_user(request: Request) -> OfficerUserContext | None:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        return None

    session = _session(request)
    payload = session.get(_SESSION_USER_KEY)
    if not isinstance(payload, dict):
        return None

    exp = int(payload.get("exp") or 0)
    if exp and exp <= int(time.time()):
        session.pop(_SESSION_USER_KEY, None)
        return None

    email = str(payload.get("email") or "").strip() or None
    display_name = str(payload.get("display_name") or "").strip() or None
    user_id = str(payload.get("user_id") or "").strip() or None
    session_group_ids = {
        _normalize_group_id(value)
        for value in payload.get("group_ids", [])
        if _normalize_group_id(value)
    }
    if session_group_ids:
        assigned_chief_scopes = await _assigned_chief_scopes(request.app.state.db, user_id=user_id, email=email)
        return _resolve_user_context(
            request.app.state.cfg,
            user_id=user_id,
            email=email,
            display_name=display_name,
            group_ids=session_group_ids,
            assigned_chief_scopes=assigned_chief_scopes,
        )

    role = str(payload.get("role") or "").strip().lower()
    if role not in {_ROLE_OFFICER, _ROLE_CHIEF_STEWARD, _ROLE_ADMIN}:
        session.pop(_SESSION_USER_KEY, None)
        return None
    can_create, can_edit, can_delete, can_bulk_edit, can_bulk_delete = _role_flags(role)
    scopes = tuple(
        sorted(
            normalize_scope_key(value)
            for value in payload.get("contract_scopes", [])
            if normalize_scope_key(value)
        )
    )
    return OfficerUserContext(
        user_id=user_id,
        email=email,
        display_name=display_name,
        role=role,
        contract_scopes=scopes,
        group_ids=tuple(sorted(session_group_ids)),
        auth_enabled=True,
        can_create=can_create,
        can_edit=can_edit,
        can_delete=can_delete,
        can_bulk_edit=can_bulk_edit,
        can_bulk_delete=can_bulk_delete,
        can_view_audit=(role == _ROLE_ADMIN),
        can_manage_chief_assignments=(role == _ROLE_ADMIN),
    )


async def require_authenticated_officer(request: Request) -> OfficerUserContext:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        require_local_access(request)
        return _local_read_only_user()

    user = await current_officer_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


async def require_officer_page_access(request: Request, *, next_path: str) -> OfficerUserContext | RedirectResponse:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        require_local_access(request)
        return _local_read_only_user()

    user = await current_officer_user(request)
    if not user:
        return _login_redirect(_sanitize_next_path(next_path))
    return user


async def require_admin_user(request: Request, *, allow_local_fallback: bool = False) -> OfficerUserContext:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        if allow_local_fallback:
            require_local_access(request)
            return _local_ops_admin_user()
        raise HTTPException(status_code=423, detail="officer changes are disabled until officer auth is enabled")

    user = await current_officer_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if user.role != _ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="admin role required")
    return user


async def require_ops_page_access(request: Request, *, next_path: str) -> OfficerUserContext | RedirectResponse:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        require_local_access(request)
        return _local_ops_admin_user()

    user = await current_officer_user(request)
    if not user:
        return _login_redirect(_sanitize_next_path(next_path, default="/ops"))
    if user.role != _ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="admin role required")
    return user


async def require_case_edit_access(request: Request, *, contract_scope: str | None) -> OfficerUserContext:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        raise HTTPException(status_code=423, detail="officer changes are disabled until officer auth is enabled")

    user = await current_officer_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if user.role == _ROLE_ADMIN:
        return user
    if user.role != _ROLE_CHIEF_STEWARD:
        raise HTTPException(status_code=403, detail="chief steward or admin role required")
    if not contract_scope or contract_scope not in user.contract_scopes:
        raise HTTPException(status_code=403, detail="case is outside this chief steward contract scope")
    return user


def user_can_view_case(user: OfficerUserContext, *, contract_scope: str | None) -> bool:
    if user.role in {_ROLE_ADMIN, _ROLE_OFFICER, _ROLE_READ_ONLY}:
        return True
    return bool(contract_scope and contract_scope in user.contract_scopes)


def actor_identity(user: OfficerUserContext, *, fallback: str | None = None) -> str:
    text = str(user.display_name or "").strip() or str(user.email or "").strip()
    if text:
        return text
    return str(fallback or "officer-ui")


def audit_actor_details(
    user: OfficerUserContext,
    *,
    case_contract_scope: str | None = None,
    bulk: bool = False,
) -> dict[str, Any]:
    return {
        "actor_email": user.email,
        "actor_display_name": user.display_name,
        "actor_role": user.role,
        "actor_contract_scopes": list(user.contract_scopes),
        "case_contract_scope": case_contract_scope,
        "bulk_update": bulk,
    }


def _build_msal_client(cfg: AppConfig) -> msal.ConfidentialClientApplication:
    authority = f"https://login.microsoftonline.com/{cfg.officer_auth.tenant_id}"
    return msal.ConfidentialClientApplication(
        client_id=cfg.officer_auth.client_id,
        authority=authority,
        client_credential=cfg.officer_auth.client_secret,
    )


@router.get("/auth/login")
async def officer_login(request: Request, next: str | None = None):
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="officer auth is disabled")

    user = await current_officer_user(request)
    next_path = _sanitize_next_path(next, default="/officers")
    if user:
        return RedirectResponse(url=next_path, status_code=303)

    flow = _build_msal_client(cfg).initiate_auth_code_flow(
        scopes=_OIDC_SCOPES,
        redirect_uri=cfg.officer_auth.redirect_uri,
    )
    session = _session(request)
    session[_SESSION_FLOW_KEY] = flow
    session[_SESSION_NEXT_KEY] = next_path
    return RedirectResponse(url=str(flow["auth_uri"]), status_code=302)


@router.get("/auth/callback")
async def officer_callback(request: Request):
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="officer auth is disabled")

    session = _session(request)
    flow = session.get(_SESSION_FLOW_KEY)
    if not isinstance(flow, dict):
        raise HTTPException(status_code=400, detail="missing auth flow in session")

    try:
        result = _build_msal_client(cfg).acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid auth callback: {exc}") from exc
    finally:
        session.pop(_SESSION_FLOW_KEY, None)

    if not isinstance(result, dict):
        raise HTTPException(status_code=401, detail="Microsoft Entra callback returned an invalid token response")
    if result.get("error"):
        description = str(result.get("error_description") or result.get("error") or "login failed").strip()
        raise HTTPException(status_code=401, detail=description)

    claims = result.get("id_token_claims")
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="missing id_token_claims in Microsoft Entra response")

    user = await _resolve_user_context_from_claims(request, claims=claims)
    session[_SESSION_USER_KEY] = _session_user_payload(user, exp=int(claims.get("exp") or 0))
    next_path = _sanitize_next_path(session.pop(_SESSION_NEXT_KEY, None), default="/officers")
    return RedirectResponse(url=next_path, status_code=303)


@router.post("/auth/logout")
async def officer_logout(request: Request):
    cfg: AppConfig = request.app.state.cfg
    session = _session(request)
    session.clear()

    if not officer_auth_enabled(cfg):
        return RedirectResponse(url="/", status_code=303)

    query = urlencode({"post_logout_redirect_uri": cfg.officer_auth.post_logout_redirect_uri})
    logout_url = (
        f"https://login.microsoftonline.com/{cfg.officer_auth.tenant_id}/oauth2/v2.0/logout?{query}"
    )
    return RedirectResponse(url=logout_url, status_code=303)
