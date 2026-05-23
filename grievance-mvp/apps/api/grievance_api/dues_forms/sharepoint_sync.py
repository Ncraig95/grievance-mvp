from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import database

DEFAULT_SHAREPOINT_LIBRARY = "Grievances Library - Documents"
DEFAULT_SHAREPOINT_FOLDER = "New Member E-Cards"
FALLBACK_SHAREPOINT_LIBRARIES: tuple[str, ...] = ("Documents", "Shared Documents")


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _clean_filename(value: object) -> str:
    name = str(value or "").strip().replace("\\", "_").replace("/", "_")
    cleaned = "".join(ch if ch.isprintable() and ch not in "\0" else "_" for ch in name).strip()
    return cleaned or "sharepoint-form.pdf"


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"unable to find unique destination for {path}")


def _library_candidates(library: str) -> tuple[str, ...]:
    candidates: list[str] = []
    configured = str(library or "").strip()
    if configured:
        candidates.append(configured)
    for fallback in FALLBACK_SHAREPOINT_LIBRARIES:
        if fallback.casefold() not in {candidate.casefold() for candidate in candidates}:
            candidates.append(fallback)
    return tuple(candidates)


@dataclass(frozen=True)
class SharePointSyncSettings:
    enabled: bool = False
    config_path: str = ""
    site_hostname: str = ""
    site_path: str = ""
    library: str = DEFAULT_SHAREPOINT_LIBRARY
    folder_path: str = DEFAULT_SHAREPOINT_FOLDER
    recursive: bool = True

    @classmethod
    def from_env(
        cls,
        *,
        enabled: bool | None = None,
        config_path: str | None = None,
        site_hostname: str | None = None,
        site_path: str | None = None,
        library: str | None = None,
        folder_path: str | None = None,
        recursive: bool | None = None,
    ) -> "SharePointSyncSettings":
        default_config = database.project_root() / "config" / "config.yaml"
        resolved_enabled = (
            bool(enabled)
            if enabled is not None
            else _as_bool(os.getenv("DUES_FORMS_SHAREPOINT_ENABLED"), False)
        )
        return cls(
            enabled=resolved_enabled,
            config_path=str(
                config_path
                or os.getenv("DUES_FORMS_GRAPH_CONFIG_PATH")
                or os.getenv("GRIEVANCE_CONFIG_PATH")
                or default_config
            ),
            site_hostname=str(site_hostname or os.getenv("DUES_FORMS_SHAREPOINT_SITE_HOSTNAME") or "").strip(),
            site_path=str(site_path or os.getenv("DUES_FORMS_SHAREPOINT_SITE_PATH") or "").strip(),
            library=str(library or os.getenv("DUES_FORMS_SHAREPOINT_LIBRARY") or DEFAULT_SHAREPOINT_LIBRARY).strip(),
            folder_path=str(folder_path or os.getenv("DUES_FORMS_SHAREPOINT_FOLDER") or DEFAULT_SHAREPOINT_FOLDER).strip(),
            recursive=(
                bool(recursive)
                if recursive is not None
                else _as_bool(os.getenv("DUES_FORMS_SHAREPOINT_RECURSIVE"), True)
            ),
        )


def _graph_from_config(settings: SharePointSyncSettings):  # noqa: ANN001
    from ..core.config import load_config
    from ..services.sharepoint_graph import GraphUploader

    config_path = Path(settings.config_path).expanduser()
    if not config_path.exists():
        raise RuntimeError(f"Graph config not found: {config_path}")
    cfg = load_config(str(config_path))
    site_hostname = settings.site_hostname or cfg.graph.site_hostname
    site_path = settings.site_path or cfg.graph.site_path
    if not site_hostname or not site_path:
        raise RuntimeError("SharePoint site hostname/path is not configured for dues form sync")
    graph = GraphUploader(
        tenant_id=cfg.graph.tenant_id,
        client_id=cfg.graph.client_id,
        cert_thumbprint=cfg.graph.cert_thumbprint,
        cert_pem_path=cfg.graph.cert_pem_path,
        dry_run=False,
    )
    return graph, site_hostname, site_path


def sync_sharepoint_pdfs_to_inbox(
    *,
    data_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    settings: SharePointSyncSettings | None = None,
    graph: Any | None = None,
    logger: Any | None = None,
) -> dict[str, int]:
    active_settings = settings or SharePointSyncSettings.from_env()
    result = {
        "remote_files": 0,
        "downloaded": 0,
        "already_downloaded": 0,
        "duplicate_hashes": 0,
        "skipped_non_pdf": 0,
        "download_errors": 0,
    }
    if not active_settings.enabled:
        return result

    resolved_data_dir = database.ensure_directories(data_dir=data_dir, db_path=db_path)
    resolved_db_path = database.resolve_db_path(db_path)
    inbox = resolved_data_dir / "inbox"

    if graph is None:
        graph, site_hostname, site_path = _graph_from_config(active_settings)
    else:
        site_hostname = active_settings.site_hostname
        site_path = active_settings.site_path
    if not site_hostname or not site_path:
        raise RuntimeError("SharePoint site hostname/path is required for dues form sync")

    files = []
    last_error: Exception | None = None
    for library in _library_candidates(active_settings.library):
        try:
            files = graph.list_files_in_folder_path(
                site_hostname=site_hostname,
                site_path=site_path,
                library=library,
                folder_path=active_settings.folder_path,
                recursive=active_settings.recursive,
            )
            if logger and library != active_settings.library:
                logger.info("SharePoint dues form sync using document library fallback: %s", library)
            break
        except RuntimeError as exc:
            message = str(exc)
            if "Could not find document library drive named" not in message:
                raise
            last_error = exc
            if logger:
                logger.info("SharePoint document library not found for dues sync: %s", library)
    else:
        if last_error is not None:
            raise RuntimeError(
                f"Could not find SharePoint document library. Tried: {', '.join(_library_candidates(active_settings.library))}"
            ) from last_error

    result["remote_files"] = len(files)

    for item in files:
        name = _clean_filename(getattr(item, "name", ""))
        if not name.lower().endswith(".pdf"):
            result["skipped_non_pdf"] += 1
            continue
        drive_id = str(getattr(item, "drive_id", "") or "").strip()
        item_id = str(getattr(item, "item_id", "") or "").strip()
        if not drive_id or not item_id:
            continue
        if database.sharepoint_item_seen(drive_id=drive_id, item_id=item_id, db_path=resolved_db_path):
            result["already_downloaded"] += 1
            continue

        dest = _unique_destination(inbox / name)
        tmp_dest = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
        try:
            content = graph.download_item_bytes(drive_id=drive_id, item_id=item_id)
            if not content:
                continue
            inbox.mkdir(parents=True, exist_ok=True)
            tmp_dest.write_bytes(content)
            tmp_dest.replace(dest)
            source_sha256 = scanner_sha256_file(dest)
            if database.record_exists(source_sha256, db_path=resolved_db_path):
                dest.unlink(missing_ok=True)
                result["duplicate_hashes"] += 1
                local_path = ""
            else:
                result["downloaded"] += 1
                local_path = str(dest)
            database.mark_sharepoint_item_downloaded(
                drive_id=drive_id,
                item_id=item_id,
                name=name,
                path=str(getattr(item, "path", "") or ""),
                web_url=str(getattr(item, "web_url", "") or ""),
                source_sha256=source_sha256,
                local_path=local_path,
                db_path=resolved_db_path,
            )
            if logger:
                logger.info("SharePoint dues form synced: %s", name)
        except Exception as exc:
            tmp_dest.unlink(missing_ok=True)
            result["download_errors"] += 1
            if logger:
                logger.exception("SharePoint dues form download failed for %s: %s", name, exc)
            continue

    return result


def scanner_sha256_file(path: Path) -> str:
    # Local helper avoids importing scanner.py and creating a cycle.
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
