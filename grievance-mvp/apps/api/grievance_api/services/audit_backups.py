from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .sharepoint_graph import GraphUploader


@dataclass(frozen=True)
class SharePointAuditCopy:
    subfolder: str
    path: str
    web_url: str | None


@dataclass(frozen=True)
class AuditBackupFailure:
    destination: str
    error: str


@dataclass(frozen=True)
class AuditBackupOutcome:
    primary_web_url: str | None
    sharepoint_copies: tuple[SharePointAuditCopy, ...]
    local_paths: tuple[str, ...]
    failures: tuple[AuditBackupFailure, ...]


def _dedupe_values(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def _parse_existing_locations(raw_json: str | None) -> dict:
    if not raw_json:
        return {"sharepoint": [], "local": []}
    try:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            return {"sharepoint": [], "local": []}
        sharepoint = parsed.get("sharepoint")
        local = parsed.get("local")
        return {
            "sharepoint": sharepoint if isinstance(sharepoint, list) else [],
            "local": local if isinstance(local, list) else [],
        }
    except Exception:
        return {"sharepoint": [], "local": []}


def _local_backup_target(
    *,
    backup_root: str,
    case_parent_folder: str,
    case_folder_name: str,
    subfolder: str,
    filename: str,
) -> Path:
    parent_parts = [p for p in case_parent_folder.replace("\\", "/").split("/") if p.strip()]
    base = Path(backup_root).expanduser()
    folder = base.joinpath(*parent_parts, case_folder_name, subfolder)
    return folder / filename


def fanout_audit_backups(
    *,
    graph: GraphUploader,
    site_hostname: str,
    site_path: str,
    library: str,
    case_parent_folder: str,
    case_folder_name: str,
    primary_subfolder: str,
    extra_subfolders: tuple[str, ...],
    local_backup_roots: tuple[str, ...],
    filename: str,
    file_bytes: bytes,
) -> AuditBackupOutcome:
    subfolders = _dedupe_values([primary_subfolder, *extra_subfolders])
    roots = _dedupe_values(list(local_backup_roots))

    primary_web_url: str | None = None
    sharepoint_copies: list[SharePointAuditCopy] = []
    local_paths: list[str] = []
    failures: list[AuditBackupFailure] = []

    for index, subfolder in enumerate(subfolders):
        try:
            uploaded = graph.upload_to_case_subfolder(
                site_hostname=site_hostname,
                site_path=site_path,
                library=library,
                case_folder_name=case_folder_name,
                case_parent_folder=case_parent_folder,
                subfolder=subfolder,
                filename=filename,
                file_bytes=file_bytes,
            )
            copy_ref = SharePointAuditCopy(
                subfolder=subfolder,
                path=uploaded.path,
                web_url=uploaded.web_url,
            )
            sharepoint_copies.append(copy_ref)
            if index == 0 and uploaded.web_url:
                primary_web_url = uploaded.web_url
            elif primary_web_url is None and uploaded.web_url:
                # Fallback if the primary destination failed but another copy succeeded.
                primary_web_url = uploaded.web_url
        except Exception as exc:
            failures.append(
                AuditBackupFailure(
                    destination=f"sharepoint:{subfolder}",
                    error=str(exc),
                )
            )

    for root in roots:
        try:
            target = _local_backup_target(
                backup_root=root,
                case_parent_folder=case_parent_folder,
                case_folder_name=case_folder_name,
                subfolder=primary_subfolder,
                filename=filename,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file_bytes)
            local_paths.append(str(target))
        except Exception as exc:
            failures.append(
                AuditBackupFailure(
                    destination=f"local:{root}",
                    error=str(exc),
                )
            )

    return AuditBackupOutcome(
        primary_web_url=primary_web_url,
        sharepoint_copies=tuple(sharepoint_copies),
        local_paths=tuple(local_paths),
        failures=tuple(failures),
    )


def merge_backup_locations_json(existing_json: str | None, outcome: AuditBackupOutcome) -> str | None:
    merged = _parse_existing_locations(existing_json)
    sharepoint_existing = merged["sharepoint"]
    local_existing = merged["local"]

    existing_paths = {
        str(item.get("path", "")).strip().lower()
        for item in sharepoint_existing
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    }
    for copy_ref in outcome.sharepoint_copies:
        key = copy_ref.path.strip().lower()
        if not key or key in existing_paths:
            continue
        existing_paths.add(key)
        sharepoint_existing.append(
            {
                "subfolder": copy_ref.subfolder,
                "path": copy_ref.path,
                "web_url": copy_ref.web_url,
            }
        )

    existing_local = {str(path).strip() for path in local_existing if str(path).strip()}
    for local_path in outcome.local_paths:
        normalized = local_path.strip()
        if not normalized or normalized in existing_local:
            continue
        existing_local.add(normalized)
        local_existing.append(normalized)

    if not sharepoint_existing and not local_existing:
        return None
    return json.dumps({"sharepoint": sharepoint_existing, "local": local_existing}, ensure_ascii=False)
