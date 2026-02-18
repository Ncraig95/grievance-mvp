from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import msal
import requests


@dataclass(frozen=True)
class CaseFolderRef:
    drive_id: str
    folder_id: str
    folder_name: str
    web_url: str | None


@dataclass(frozen=True)
class UploadedFileRef:
    drive_id: str
    item_id: str
    web_url: str | None
    path: str


class GraphUploader:
    """Graph app-only client with SharePoint case-folder helpers."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        cert_thumbprint: str,
        cert_pem_path: str,
        timeout_seconds: int = 30,
        dry_run: bool = False,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.cert_thumbprint = cert_thumbprint
        self.cert_pem_path = cert_pem_path
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._app: msal.ConfidentialClientApplication | None = None
        self._site_cache: dict[str, str] = {}
        self._drive_cache: dict[tuple[str, str, str], str] = {}

    def _load_cert_credential(self) -> dict:
        pem = Path(self.cert_pem_path).read_text(encoding="utf-8")
        return {"private_key": pem, "thumbprint": self.cert_thumbprint}

    def token(self) -> str:
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                authority=self._authority,
                client_credential=self._load_cert_credential(),
            )

        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            err = result.get("error")
            desc = result.get("error_description")
            raise RuntimeError(f"Graph token failure: {err} {desc}")
        return str(result["access_token"])

    def _request(self, method: str, endpoint: str, *, payload: dict | None = None, data: bytes | None = None) -> dict:
        if self.dry_run:
            return {}
        url = endpoint
        if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
            url = f"https://graph.microsoft.com/v1.0{endpoint}"
        r = requests.request(
            method=method,
            url=url,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": "application/json" if data is None else "application/octet-stream",
            },
            json=payload if data is None else None,
            data=data,
            timeout=self.timeout_seconds,
        )
        if 200 <= r.status_code < 300:
            if r.content:
                return r.json()
            return {}
        raise RuntimeError(f"Graph request failed ({method} {endpoint}): {r.status_code} {r.text[:500]}")

    @staticmethod
    def _encode_path(path: str) -> str:
        return "/".join(quote(part, safe="") for part in path.strip("/").split("/") if part)

    def _site_id(self, site_hostname: str, site_path: str) -> str:
        cache_key = f"{site_hostname}:{site_path}"
        if cache_key in self._site_cache:
            return self._site_cache[cache_key]
        if self.dry_run:
            site_id = "dryrun-site"
        else:
            site = self._request("GET", f"/sites/{site_hostname}:{site_path}")
            site_id = str(site["id"])
        self._site_cache[cache_key] = site_id
        return site_id

    def _drive_id(self, site_hostname: str, site_path: str, library: str) -> str:
        cache_key = (site_hostname, site_path, library)
        if cache_key in self._drive_cache:
            return self._drive_cache[cache_key]
        if self.dry_run:
            drive_id = "dryrun-drive"
        else:
            site_id = self._site_id(site_hostname, site_path)
            drives = self._request("GET", f"/sites/{site_id}/drives")
            drive_id = ""
            for d in drives.get("value", []):
                if d.get("name") == library:
                    drive_id = str(d["id"])
                    break
            if not drive_id:
                raise RuntimeError(f"Could not find document library drive named '{library}'")
        self._drive_cache[cache_key] = drive_id
        return drive_id

    def _root_folder_id(self, drive_id: str) -> str:
        if self.dry_run:
            return "dryrun-root"
        root = self._request("GET", f"/drives/{drive_id}/root")
        return str(root["id"])

    def _list_children(self, drive_id: str, folder_id: str) -> list[dict]:
        if self.dry_run:
            return []
        out: list[dict] = []
        endpoint = f"/drives/{drive_id}/items/{folder_id}/children?$top=200"
        while endpoint:
            page = self._request("GET", endpoint)
            out.extend(page.get("value", []))
            endpoint = str(page.get("@odata.nextLink", "")).strip()
        return out

    def _find_child_folder(self, drive_id: str, folder_id: str, name: str) -> dict | None:
        wanted = name.strip().lower()
        for child in self._list_children(drive_id, folder_id):
            if not isinstance(child, dict):
                continue
            if "folder" not in child:
                continue
            if str(child.get("name", "")).strip().lower() == wanted:
                return child
        return None

    def _create_child_folder(self, drive_id: str, folder_id: str, name: str) -> dict:
        if self.dry_run:
            return {"id": f"dryrun-{name}", "name": name, "webUrl": f"https://dryrun/{name}"}
        return self._request(
            "POST",
            f"/drives/{drive_id}/items/{folder_id}/children",
            payload={
                "name": name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "rename",
            },
        )

    def _ensure_folder_chain(self, drive_id: str, folder_path: str) -> tuple[str, str]:
        folder_id = self._root_folder_id(drive_id)
        current_path_parts: list[str] = []
        for part in [p for p in folder_path.strip("/").split("/") if p]:
            current_path_parts.append(part)
            existing = self._find_child_folder(drive_id, folder_id, part)
            if existing is None:
                existing = self._create_child_folder(drive_id, folder_id, part)
            folder_id = str(existing["id"])
        return folder_id, "/".join(current_path_parts)

    def ensure_case_folder(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        case_parent_folder: str,
        grievance_id: str,
        member_name: str,
    ) -> CaseFolderRef:
        drive_id = self._drive_id(site_hostname, site_path, library)
        parent_id, parent_path = self._ensure_folder_chain(drive_id, case_parent_folder)

        desired_name = f"{grievance_id} {member_name}".strip()
        desired_lower = desired_name.lower()
        wanted_token = grievance_id.lower().strip()
        exact_match: dict | None = None
        prefix_match: dict | None = None
        contains_match: dict | None = None
        for child in self._list_children(drive_id, parent_id):
            if "folder" not in child:
                continue
            name = str(child.get("name", ""))
            lowered = name.lower()
            if lowered == desired_lower:
                exact_match = child
                break
            if wanted_token and (lowered == wanted_token or lowered.startswith(f"{wanted_token} ")):
                if prefix_match is None:
                    prefix_match = child
                continue
            if wanted_token and wanted_token in lowered and contains_match is None:
                contains_match = child

        selected = exact_match or prefix_match or contains_match
        if selected is None:
            selected = self._create_child_folder(drive_id, parent_id, desired_name)

        folder_name = str(selected.get("name", ""))
        folder_path = "/".join(part for part in [parent_path, folder_name] if part)
        return CaseFolderRef(
            drive_id=drive_id,
            folder_id=str(selected["id"]),
            folder_name=folder_name,
            web_url=selected.get("webUrl"),
        )

    def list_case_folder_names(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        case_parent_folder: str,
    ) -> list[str]:
        drive_id = self._drive_id(site_hostname, site_path, library)
        parent_id, _ = self._ensure_folder_chain(drive_id, case_parent_folder)
        names: list[str] = []
        for child in self._list_children(drive_id, parent_id):
            if "folder" not in child:
                continue
            name = str(child.get("name", "")).strip()
            if name:
                names.append(name)
        return names

    def upload_to_case_subfolder(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        case_folder_name: str,
        case_parent_folder: str,
        subfolder: str,
        filename: str,
        file_bytes: bytes,
    ) -> UploadedFileRef:
        drive_id = self._drive_id(site_hostname, site_path, library)
        full_folder = "/".join(
            part.strip("/")
            for part in (case_parent_folder, case_folder_name, subfolder)
            if part and part.strip("/")
        )
        _, normalized_folder = self._ensure_folder_chain(drive_id, full_folder)

        normalized_path = "/".join(
            part for part in [self._encode_path(normalized_folder), quote(filename, safe="")] if part
        )

        if self.dry_run:
            return UploadedFileRef(
                drive_id=drive_id,
                item_id=f"dryrun-{filename}",
                web_url=f"https://dryrun/{normalized_path}",
                path=normalized_path,
            )

        put = self._request(
            "PUT",
            f"/drives/{drive_id}/root:/{normalized_path}:/content",
            data=file_bytes,
        )
        return UploadedFileRef(
            drive_id=drive_id,
            item_id=str(put.get("id", "")),
            web_url=put.get("webUrl"),
            path=normalized_path,
        )
