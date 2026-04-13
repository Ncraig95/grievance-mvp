from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import msal
import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..core.config import AppConfig
from ..db.db import utcnow
from .admin_common import require_local_access

router = APIRouter()

# MSAL adds the required OIDC scopes automatically for the auth-code flow.
_OIDC_SCOPES: list[str] = []
_SESSION_USER_KEY = "officer_user"
_SESSION_FLOW_KEY = "officer_auth_flow"
_SESSION_NEXT_KEY = "officer_auth_next"
_EXTERNAL_SESSION_USER_KEY = "external_steward_user"
_EXTERNAL_SESSION_FLOW_KEY = "external_steward_auth_flow"
_EXTERNAL_SESSION_NEXT_KEY = "external_steward_auth_next"
_ROLE_READ_ONLY = "read_only"
_ROLE_OFFICER = "officer"
_ROLE_CHIEF_STEWARD = "chief_steward"
_ROLE_ADMIN = "admin"
_ROLE_EXTERNAL_STEWARD = "external_steward"
_EXTERNAL_AUTH_SOURCE = "microsoft_oidc"
_EXTERNAL_STEWARD_STATUS_ACTIVE = "active"
_EXTERNAL_STEWARD_STATUS_DISABLED = "disabled"

_EXTERNAL_OIDC_TIMEOUT_SECONDS = 15
_DEFAULT_CONTRACT_SCOPE_DEFINITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("coj", "City of Jacksonville (COJ)", ("City of Jacksonville", "City of Jacksonville (COJ)", "COJ")),
    ("wire_tech", "Wire Tech (WT)", ("Wire Tech", "Wire Tech (WT)", "WT")),
    ("core_southeastern", "Core Southeastern", ("Core Southeastern", "Core Southeast", "Core Southest")),
    ("construction", "Construction", ("Construction",)),
    ("yellow_pages_thrive", "Yellow Pages / Thrive", ("Yellow Pages / Thrive", "Yellow Pages", "Thrive")),
    ("mobility_ihx", "Mobility / IHX", ("Mobility / IHX",)),
    ("mobility", "Mobility", ("Mobility", "AT&T Mobility", "ATT Mobility")),
    ("ihx", "IHX", ("IHX",)),
    ("utilities", "Utilities", ("Utilities", "Utility")),
)
_FIXED_SCOPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("coj", "City of Jacksonville (COJ)"),
    ("wire_tech", "Wire Tech (WT)"),
    ("core_southeastern", "Core Southeastern"),
    ("construction", "Construction"),
    ("yellow_pages_thrive", "Yellow Pages / Thrive"),
    ("mobility_ihx", "Mobility / IHX"),
)
_CASE_SCOPE_COMPATIBILITY: dict[str, tuple[str, ...]] = {
    "core_southeastern": ("core_southeastern", "utilities"),
    "utilities": ("core_southeastern", "utilities"),
    "mobility_ihx": ("mobility_ihx", "mobility", "ihx"),
    "mobility": ("mobility_ihx", "mobility", "ihx"),
    "ihx": ("mobility_ihx", "mobility", "ihx"),
}


@dataclass(frozen=True)
class OfficerUserContext:
    user_id: str | None
    email: str | None
    display_name: str | None
    officer_title: str | None
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


@dataclass(frozen=True)
class ExternalStewardUserContext:
    external_user_id: int
    email: str
    display_name: str | None
    auth_source: str
    issuer: str
    provider_subject: str
    verified_email: bool
    auth_enabled: bool


def officer_auth_enabled(cfg: AppConfig) -> bool:
    return bool(getattr(cfg, "officer_auth", None) and cfg.officer_auth.enabled)


def external_steward_auth_enabled(cfg: AppConfig) -> bool:
    return bool(getattr(cfg, "external_steward_auth", None) and cfg.external_steward_auth.enabled)


def _normalize_group_id(value: object) -> str:
    return str(value or "").strip().lower()


def _normalize_email(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text or "@" not in text:
        return None
    return text


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


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


def manual_contract_choices() -> tuple[tuple[str, str], ...]:
    return (
        ("City of Jacksonville", "City of Jacksonville (COJ)"),
        ("Wire Tech", "Wire Tech (WT)"),
        ("Core Southeastern", "Core Southeastern"),
        ("Construction", "Construction"),
        ("Yellow Pages / Thrive", "Yellow Pages / Thrive"),
        ("Mobility / IHX", "Mobility / IHX"),
    )


def selectable_contract_scopes() -> tuple[tuple[str, str], ...]:
    return _FIXED_SCOPE_CHOICES


def scope_display_label(value: object) -> str:
    normalized = normalize_scope_key(value)
    for scope_key, label in _FIXED_SCOPE_CHOICES:
        if normalized == scope_key:
            return label
    compatibility_label_map = {
        "utilities": "Core Southeastern",
        "mobility": "Mobility / IHX",
        "ihx": "Mobility / IHX",
    }
    if normalized in compatibility_label_map:
        return compatibility_label_map[normalized]
    for scope_key, label, _aliases in _DEFAULT_CONTRACT_SCOPE_DEFINITIONS:
        if normalized == scope_key:
            return label
    return str(value or "").replace("_", " ").title()


def known_contract_scopes(cfg: AppConfig) -> tuple[str, ...]:
    scopes = {scope_key for scope_key, _label, _aliases in _DEFAULT_CONTRACT_SCOPE_DEFINITIONS}
    scopes.update(
        normalize_scope_key(scope_key)
        for scope_key in cfg.officer_auth.chief_steward_contract_scopes
        if normalize_scope_key(scope_key)
    )
    return tuple(sorted(scopes))


def case_scope_matches_user_scopes(contract_scope: str | None, user_scopes: tuple[str, ...]) -> bool:
    normalized_scope = normalize_scope_key(contract_scope)
    if not normalized_scope:
        return False
    case_scopes = set(_CASE_SCOPE_COMPATIBILITY.get(normalized_scope, (normalized_scope,)))
    for user_scope in user_scopes:
        normalized_user_scope = normalize_scope_key(user_scope)
        if not normalized_user_scope:
            continue
        allowed_user_scopes = _CASE_SCOPE_COMPATIBILITY.get(normalized_user_scope, (normalized_user_scope,))
        if case_scopes.intersection(allowed_user_scopes):
            return True
    return False


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


def _external_login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/auth/steward/login?next={quote(next_path, safe='/?=&')}", status_code=303)


def _auth_origin_from_redirect_uri(redirect_uri: str) -> str | None:
    parsed = urlsplit(str(redirect_uri or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _auth_host_from_redirect_uri(redirect_uri: str) -> str | None:
    parsed = urlsplit(str(redirect_uri or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    return host or None


def _external_steward_tenant_id(cfg: AppConfig) -> str:
    tenant_id = str(cfg.external_steward_auth.tenant_id or "").strip()
    if tenant_id:
        return tenant_id
    if cfg.external_steward_auth.reuse_officer_auth_app:
        return str(cfg.officer_auth.tenant_id or "").strip()
    return ""


def _external_steward_client_id(cfg: AppConfig) -> str:
    client_id = str(cfg.external_steward_auth.client_id or "").strip()
    if client_id:
        return client_id
    if cfg.external_steward_auth.reuse_officer_auth_app:
        return str(cfg.officer_auth.client_id or "").strip()
    return ""


def _external_steward_client_secret(cfg: AppConfig) -> str:
    client_secret = str(cfg.external_steward_auth.client_secret or "").strip()
    if client_secret:
        return client_secret
    if cfg.external_steward_auth.reuse_officer_auth_app:
        return str(cfg.officer_auth.client_secret or "").strip()
    return ""


def _external_steward_redirect_uri(cfg: AppConfig) -> str:
    redirect_uri = str(cfg.external_steward_auth.redirect_uri or "").strip()
    if redirect_uri:
        return redirect_uri
    if cfg.external_steward_auth.reuse_officer_auth_app:
        return str(cfg.officer_auth.redirect_uri or "").strip()
    return ""


def _external_steward_post_logout_redirect_uri(cfg: AppConfig) -> str:
    redirect_uri = str(cfg.external_steward_auth.post_logout_redirect_uri or "").strip()
    if redirect_uri:
        return redirect_uri
    if cfg.external_steward_auth.reuse_officer_auth_app:
        return str(cfg.officer_auth.post_logout_redirect_uri or "").strip()
    return ""


def _request_host(request: Request) -> str | None:
    headers = getattr(request, "headers", None)
    if headers is not None:
        forwarded_host = str(headers.get("x-forwarded-host") or "").strip()
        if forwarded_host:
            return forwarded_host.split(",", 1)[0].strip().split(":", 1)[0].lower() or None
        host = str(headers.get("host") or "").strip()
        if host:
            return host.split(":", 1)[0].lower() or None
    request_url = getattr(request, "url", None)
    host = str(getattr(request_url, "hostname", "") or "").strip().lower()
    return host or None


def _http_json(method: str, url: str, *, data: dict[str, object] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.request(
        method=method,
        url=url,
        data=data,
        headers=headers,
        timeout=_EXTERNAL_OIDC_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.text[:500]}")
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _decode_jwt_claims_unverified(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _external_email_from_claims(*sources: dict[str, Any]) -> str | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("email", "preferred_username", "upn"):
            normalized = _normalize_email(source.get(key))
            if normalized:
                return normalized
        emails = source.get("emails")
        if isinstance(emails, list):
            for value in emails:
                normalized = _normalize_email(value)
                if normalized:
                    return normalized
    return None


def _external_display_name_from_claims(*sources: dict[str, Any]) -> str | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("name", "displayName"):
            text = str(source.get(key) or "").strip()
            if text:
                return text
        given = str(source.get("given_name") or "").strip()
        family = str(source.get("family_name") or "").strip()
        joined = " ".join(part for part in (given, family) if part)
        if joined:
            return joined
    return None


def _external_email_verified(*sources: dict[str, Any]) -> bool:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("email_verified", "verified_email", "verifiedEmail", "verified_primary_email"):
            if key in source:
                return _coerce_bool(source.get(key))
    return False


def _local_read_only_user() -> OfficerUserContext:
    return OfficerUserContext(
        user_id=None,
        email=None,
        display_name="Local Read Only",
        officer_title=None,
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
        officer_title=None,
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


async def _officer_profile_title(db, *, user_id: str | None, email: str | None) -> str | None:  # noqa: ANN001
    normalized_email = _normalize_email(email)
    clauses: list[str] = []
    params: list[object] = []
    if normalized_email:
        clauses.append("lower(COALESCE(principal_email, '')) = lower(?)")
        params.append(normalized_email)
    if user_id:
        clauses.append("lower(COALESCE(principal_id, '')) = lower(?)")
        params.append(user_id)
    if not clauses:
        return None

    row = await db.fetchone(
        f"""
        SELECT officer_title
        FROM officer_profiles
        WHERE {' OR '.join(clauses)}
        ORDER BY CASE
          WHEN lower(COALESCE(principal_email, '')) = lower(?) THEN 0
          ELSE 1
        END,
        id DESC
        LIMIT 1
        """,
        tuple(params + [normalized_email or ""]),
    )
    title = str(row[0] or "").strip() if row else ""
    return title or None


def _contract_scope_lookup(cfg: AppConfig) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for scope_key, _label, aliases in _DEFAULT_CONTRACT_SCOPE_DEFINITIONS:
        lookup[scope_key] = scope_key
        for alias in aliases:
            normalized = normalize_scope_key(alias)
            if normalized:
                lookup[normalized] = scope_key
    for scope_key, scope_cfg in cfg.officer_auth.chief_steward_contract_scopes.items():
        normalized_scope_key = normalize_scope_key(scope_key)
        if not normalized_scope_key:
            continue
        lookup[normalized_scope_key] = normalized_scope_key
        for alias in scope_cfg.contract_aliases:
            normalized = normalize_scope_key(alias)
            if normalized:
                lookup[normalized] = normalized_scope_key
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
        normalized_scope_key = normalize_scope_key(scope_key)
        if not normalized_scope_key:
            continue
        scope_group_ids = {
            _normalize_group_id(value) for value in scope_cfg.group_ids if _normalize_group_id(value)
        }
        if scope_group_ids.intersection(group_ids):
            chief_scopes.add(normalized_scope_key)
    return tuple(sorted(chief_scopes))


def _resolve_user_context(
    cfg: AppConfig,
    *,
    user_id: str | None,
    email: str | None,
    display_name: str | None,
    officer_title: str | None,
    group_ids: set[str],
    assigned_chief_scopes: tuple[str, ...],
) -> OfficerUserContext:
    admin_group_ids = {_normalize_group_id(value) for value in cfg.officer_auth.admin_group_ids if _normalize_group_id(value)}
    officer_group_ids = {
        _normalize_group_id(value) for value in cfg.officer_auth.officer_group_ids if _normalize_group_id(value)
    }
    chief_steward_group_ids = {
        _normalize_group_id(value)
        for value in cfg.officer_auth.chief_steward_group_ids
        if _normalize_group_id(value)
    }
    has_officer_group = bool(officer_group_ids.intersection(group_ids))
    has_chief_steward_group = bool(chief_steward_group_ids.intersection(group_ids))
    chief_scope_set = set(_chief_scopes_from_groups(cfg, group_ids=group_ids)).union(assigned_chief_scopes)

    if admin_group_ids.intersection(group_ids):
        role = _ROLE_ADMIN
        contract_scopes = tuple(scope_key for scope_key, _label in selectable_contract_scopes())
    elif chief_scope_set and (has_chief_steward_group or has_officer_group or not chief_steward_group_ids):
        role = _ROLE_CHIEF_STEWARD
        contract_scopes = tuple(sorted(chief_scope_set))
    elif has_officer_group:
        role = _ROLE_OFFICER
        contract_scopes = ()
    else:
        raise HTTPException(status_code=403, detail="signed-in user is not authorized for officer access")

    can_create, can_edit, can_delete, can_bulk_edit, can_bulk_delete = _role_flags(role)
    return OfficerUserContext(
        user_id=user_id,
        email=email,
        display_name=display_name,
        officer_title=officer_title,
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
    officer_title = await _officer_profile_title(request.app.state.db, user_id=user_id, email=email)
    return _resolve_user_context(
        cfg,
        user_id=user_id,
        email=email,
        display_name=display_name,
        officer_title=officer_title,
        group_ids=group_ids,
        assigned_chief_scopes=assigned_chief_scopes,
    )


def _session_user_payload(user: OfficerUserContext, *, exp: int | None) -> dict[str, Any]:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "officer_title": user.officer_title,
        "role": user.role,
        "contract_scopes": list(user.contract_scopes),
        "group_ids": list(user.group_ids),
        "exp": int(exp or 0),
    }


def _external_session_user_payload(user: ExternalStewardUserContext, *, exp: int | None) -> dict[str, Any]:
    return {
        "external_user_id": int(user.external_user_id),
        "email": user.email,
        "display_name": user.display_name,
        "role": _ROLE_EXTERNAL_STEWARD,
        "auth_source": user.auth_source,
        "issuer": user.issuer,
        "provider_subject": user.provider_subject,
        "verified_email": bool(user.verified_email),
        "exp": int(exp or 0),
    }


async def _bind_external_steward_login(
    request: Request,
    *,
    email: str,
    display_name: str | None,
    issuer: str,
    provider_subject: str,
) -> ExternalStewardUserContext:
    db = request.app.state.db
    normalized_email = _normalize_email(email)
    if not normalized_email:
        raise HTTPException(status_code=403, detail="external steward login is missing a valid verified email")
    row = await db.fetchone(
        """
        SELECT id, email, display_name, status, auth_source, auth_issuer, auth_subject
        FROM external_steward_users
        WHERE email=?
        """,
        (normalized_email,),
    )
    if not row:
        raise HTTPException(status_code=403, detail="external steward email is not allowlisted")

    status = str(row[3] or "").strip().lower()
    if status != _EXTERNAL_STEWARD_STATUS_ACTIVE:
        raise HTTPException(status_code=403, detail="external steward access is disabled")

    existing_issuer = str(row[5] or "").strip()
    existing_subject = str(row[6] or "").strip()
    if existing_issuer and existing_subject:
        if existing_issuer != issuer or existing_subject != provider_subject:
            raise HTTPException(status_code=403, detail="external steward account is already bound to a different identity")
    else:
        now = utcnow()
        await db.exec(
            """
            UPDATE external_steward_users
            SET auth_source=?, auth_issuer=?, auth_subject=?, updated_at_utc=?
            WHERE id=?
            """,
            (_EXTERNAL_AUTH_SOURCE, issuer, provider_subject, now, int(row[0])),
        )

    last_login_at_utc = utcnow()
    if display_name and not str(row[2] or "").strip():
        await db.exec(
            "UPDATE external_steward_users SET display_name=?, last_login_at_utc=?, updated_at_utc=? WHERE id=?",
            (display_name, last_login_at_utc, last_login_at_utc, int(row[0])),
        )
    else:
        await db.exec(
            "UPDATE external_steward_users SET last_login_at_utc=?, updated_at_utc=? WHERE id=?",
            (last_login_at_utc, last_login_at_utc, int(row[0])),
        )

    return ExternalStewardUserContext(
        external_user_id=int(row[0]),
        email=str(row[1] or normalized_email),
        display_name=str(row[2] or "").strip() or display_name or normalized_email,
        auth_source=_EXTERNAL_AUTH_SOURCE,
        issuer=issuer,
        provider_subject=provider_subject,
        verified_email=True,
        auth_enabled=True,
    )


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
    officer_title = await _officer_profile_title(request.app.state.db, user_id=user_id, email=email)
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
            officer_title=officer_title,
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
        officer_title=officer_title or str(payload.get("officer_title") or "").strip() or None,
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


async def current_external_steward_user(request: Request) -> ExternalStewardUserContext | None:
    cfg: AppConfig = request.app.state.cfg
    if not external_steward_auth_enabled(cfg):
        return None

    session = _session(request)
    payload = session.get(_EXTERNAL_SESSION_USER_KEY)
    if not isinstance(payload, dict):
        return None

    exp = int(payload.get("exp") or 0)
    if exp and exp <= int(time.time()):
        session.pop(_EXTERNAL_SESSION_USER_KEY, None)
        return None

    if str(payload.get("role") or "").strip().lower() != _ROLE_EXTERNAL_STEWARD:
        session.pop(_EXTERNAL_SESSION_USER_KEY, None)
        return None

    external_user_id = int(payload.get("external_user_id") or 0)
    email = _normalize_email(payload.get("email"))
    issuer = str(payload.get("issuer") or "").strip()
    provider_subject = str(payload.get("provider_subject") or "").strip()
    if external_user_id <= 0 or not email or not issuer or not provider_subject:
        session.pop(_EXTERNAL_SESSION_USER_KEY, None)
        return None

    row = await request.app.state.db.fetchone(
        """
        SELECT id, email, display_name, status, auth_source, auth_issuer, auth_subject
        FROM external_steward_users
        WHERE id=?
        """,
        (external_user_id,),
    )
    if not row:
        session.pop(_EXTERNAL_SESSION_USER_KEY, None)
        return None

    status = str(row[3] or "").strip().lower()
    if status != _EXTERNAL_STEWARD_STATUS_ACTIVE:
        session.pop(_EXTERNAL_SESSION_USER_KEY, None)
        return None

    if str(row[5] or "").strip() != issuer or str(row[6] or "").strip() != provider_subject:
        session.pop(_EXTERNAL_SESSION_USER_KEY, None)
        return None

    return ExternalStewardUserContext(
        external_user_id=int(row[0]),
        email=str(row[1] or email),
        display_name=str(row[2] or "").strip() or str(payload.get("display_name") or "").strip() or email,
        auth_source=str(row[4] or _EXTERNAL_AUTH_SOURCE),
        issuer=issuer,
        provider_subject=provider_subject,
        verified_email=bool(payload.get("verified_email")),
        auth_enabled=True,
    )


async def require_authenticated_external_steward(request: Request) -> ExternalStewardUserContext:
    cfg: AppConfig = request.app.state.cfg
    if not external_steward_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="external steward auth is disabled")
    user = await current_external_steward_user(request)
    if not user:
        if await current_officer_user(request):
            raise HTTPException(status_code=403, detail="staff users must use the officer portal")
        raise HTTPException(status_code=401, detail="external steward login required")
    return user


async def require_external_steward_page_access(
    request: Request,
    *,
    next_path: str,
) -> ExternalStewardUserContext | RedirectResponse:
    cfg: AppConfig = request.app.state.cfg
    if not external_steward_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="external steward auth is disabled")
    user = await current_external_steward_user(request)
    if user:
        return user
    if await current_officer_user(request):
        raise HTTPException(status_code=403, detail="staff users must use the officer portal")
    return _external_login_redirect(_sanitize_next_path(next_path, default="/steward"))


async def require_authenticated_officer(request: Request) -> OfficerUserContext:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        require_local_access(request)
        return _local_read_only_user()

    user = await current_officer_user(request)
    if not user:
        if await current_external_steward_user(request):
            raise HTTPException(status_code=403, detail="external steward access is limited to the steward portal")
        raise HTTPException(status_code=401, detail="login required")
    return user


async def require_officer_page_access(request: Request, *, next_path: str) -> OfficerUserContext | RedirectResponse:
    cfg: AppConfig = request.app.state.cfg
    if not officer_auth_enabled(cfg):
        require_local_access(request)
        return _local_read_only_user()

    user = await current_officer_user(request)
    if not user:
        if await current_external_steward_user(request):
            raise HTTPException(status_code=403, detail="external steward access is limited to the steward portal")
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
        if await current_external_steward_user(request):
            raise HTTPException(status_code=403, detail="external steward access is limited to the steward portal")
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
        if await current_external_steward_user(request):
            raise HTTPException(status_code=403, detail="external steward access is limited to the steward portal")
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
        if await current_external_steward_user(request):
            raise HTTPException(status_code=403, detail="external stewards cannot edit officer case metadata")
        raise HTTPException(status_code=401, detail="login required")
    if user.role == _ROLE_ADMIN:
        return user
    if user.role != _ROLE_CHIEF_STEWARD:
        raise HTTPException(status_code=403, detail="chief steward or admin role required")
    if not case_scope_matches_user_scopes(contract_scope, user.contract_scopes):
        raise HTTPException(status_code=403, detail="case is outside this chief steward contract scope")
    return user


def user_can_view_case(user: OfficerUserContext, *, contract_scope: str | None) -> bool:
    if user.role in {_ROLE_ADMIN, _ROLE_OFFICER, _ROLE_READ_ONLY}:
        return True
    return case_scope_matches_user_scopes(contract_scope, user.contract_scopes)


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
        "actor_officer_title": user.officer_title,
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


def _build_external_steward_msal_client(cfg: AppConfig) -> msal.ConfidentialClientApplication:
    tenant_id = _external_steward_tenant_id(cfg)
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    return msal.ConfidentialClientApplication(
        client_id=_external_steward_client_id(cfg),
        authority=authority,
        client_credential=_external_steward_client_secret(cfg),
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

    auth_origin = _auth_origin_from_redirect_uri(cfg.officer_auth.redirect_uri)
    auth_host = _auth_host_from_redirect_uri(cfg.officer_auth.redirect_uri)
    request_host = _request_host(request)
    if auth_origin and auth_host and request_host and auth_host != request_host:
        return RedirectResponse(
            url=f"{auth_origin}/auth/login?next={quote(next_path, safe='/?=&')}",
            status_code=303,
        )

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
    external_flow = _session(request).get(_EXTERNAL_SESSION_FLOW_KEY)
    callback_state = str(request.query_params.get("state") or "").strip()
    if (
        external_steward_auth_enabled(cfg)
        and isinstance(external_flow, dict)
        and str(external_flow.get("state") or "").strip()
        and str(external_flow.get("state") or "").strip() == callback_state
    ):
        return await _complete_external_steward_callback(request)

    if not officer_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="officer auth is disabled")

    session = _session(request)
    flow = session.get(_SESSION_FLOW_KEY)
    if not isinstance(flow, dict):
        session.pop(_SESSION_NEXT_KEY, None)
        return _login_redirect("/officers")

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


@router.get("/auth/steward/login")
async def external_steward_login(request: Request, next: str | None = None):
    cfg: AppConfig = request.app.state.cfg
    if not external_steward_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="external steward auth is disabled")

    existing = await current_external_steward_user(request)
    next_path = _sanitize_next_path(next, default="/steward")
    if existing:
        return RedirectResponse(url=next_path, status_code=303)

    redirect_uri = _external_steward_redirect_uri(cfg)
    auth_origin = _auth_origin_from_redirect_uri(redirect_uri)
    auth_host = _auth_host_from_redirect_uri(redirect_uri)
    request_host = _request_host(request)
    if auth_origin and auth_host and request_host and auth_host != request_host:
        return RedirectResponse(
            url=f"{auth_origin}/auth/steward/login?next={quote(next_path, safe='/?=&')}",
            status_code=303,
        )

    flow = _build_external_steward_msal_client(cfg).initiate_auth_code_flow(scopes=[], redirect_uri=redirect_uri)
    session = _session(request)
    session[_EXTERNAL_SESSION_FLOW_KEY] = flow
    session[_EXTERNAL_SESSION_NEXT_KEY] = next_path
    return RedirectResponse(url=str(flow["auth_uri"]), status_code=302)


@router.get("/auth/steward/callback")
async def external_steward_callback(request: Request):
    return await _complete_external_steward_callback(request)


async def _complete_external_steward_callback(request: Request):
    cfg: AppConfig = request.app.state.cfg
    if not external_steward_auth_enabled(cfg):
        raise HTTPException(status_code=503, detail="external steward auth is disabled")

    session = _session(request)
    flow = session.get(_EXTERNAL_SESSION_FLOW_KEY)
    if not isinstance(flow, dict):
        session.pop(_EXTERNAL_SESSION_NEXT_KEY, None)
        return _external_login_redirect("/steward")

    try:
        token_result = _build_external_steward_msal_client(cfg).acquire_token_by_auth_code_flow(
            flow,
            dict(request.query_params),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid external steward auth callback: {exc}") from exc
    finally:
        session.pop(_EXTERNAL_SESSION_FLOW_KEY, None)

    if not isinstance(token_result, dict):
        raise HTTPException(status_code=401, detail="external steward callback returned an invalid token response")
    if token_result.get("error"):
        description = str(token_result.get("error_description") or token_result.get("error") or "login failed").strip()
        raise HTTPException(status_code=401, detail=description)

    id_claims = token_result.get("id_token_claims")
    if not isinstance(id_claims, dict):
        id_token = str(token_result.get("id_token") or "").strip()
        id_claims = _decode_jwt_claims_unverified(id_token)
    if not isinstance(id_claims, dict):
        raise HTTPException(status_code=401, detail="missing id_token_claims in external steward response")

    issuer = str(id_claims.get("iss") or "").strip()
    provider_subject = str(id_claims.get("oid") or id_claims.get("sub") or "").strip()
    email = _external_email_from_claims(id_claims)
    display_name = _external_display_name_from_claims(id_claims)
    verified_email = bool(email)
    if not issuer or not provider_subject:
        raise HTTPException(status_code=401, detail="external steward identity response is missing issuer or subject")
    if not email:
        raise HTTPException(status_code=403, detail="external steward login is missing a usable email")

    user = await _bind_external_steward_login(
        request,
        email=email,
        display_name=display_name,
        issuer=issuer,
        provider_subject=provider_subject,
    )
    exp = int(id_claims.get("exp") or token_result.get("expires_on") or 0)
    if exp <= int(time.time()):
        exp = int(time.time()) + 3600
    session[_EXTERNAL_SESSION_USER_KEY] = _external_session_user_payload(user, exp=exp)
    next_path = _sanitize_next_path(session.pop(_EXTERNAL_SESSION_NEXT_KEY, None), default="/steward")
    return RedirectResponse(url=next_path, status_code=303)


@router.post("/auth/logout")
async def officer_logout(request: Request):
    cfg: AppConfig = request.app.state.cfg
    session = _session(request)
    had_external_session = isinstance(session.get(_EXTERNAL_SESSION_USER_KEY), dict)
    had_staff_session = isinstance(session.get(_SESSION_USER_KEY), dict)
    session.clear()

    if had_external_session and external_steward_auth_enabled(cfg):
        query = urlencode({"post_logout_redirect_uri": _external_steward_post_logout_redirect_uri(cfg)})
        logout_url = (
            f"https://login.microsoftonline.com/{_external_steward_tenant_id(cfg)}/oauth2/v2.0/logout?{query}"
        )
        return RedirectResponse(url=logout_url, status_code=303)

    if had_staff_session and officer_auth_enabled(cfg):
        query = urlencode({"post_logout_redirect_uri": cfg.officer_auth.post_logout_redirect_uri})
        logout_url = (
            f"https://login.microsoftonline.com/{cfg.officer_auth.tenant_id}/oauth2/v2.0/logout?{query}"
        )
        return RedirectResponse(url=logout_url, status_code=303)

    return RedirectResponse(url="/", status_code=303)
