from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import msal
import requests


@dataclass(frozen=True)
class UploadedFileRef:
    drive_id: str
    item_id: str
    web_url: str | None


class GraphUploader:
    """Minimal Graph app-only auth using a certificate PEM."""

    def __init__(self, tenant_id: str, client_id: str, cert_thumbprint: str, cert_pem_path: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.cert_thumbprint = cert_thumbprint
        self.cert_pem_path = cert_pem_path
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"
        self._app: msal.ConfidentialClientApplication | None = None

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
        return result["access_token"]

    def _put_bytes(self, url: str, token: str, data: bytes) -> dict:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
        last_err = None
        for _ in range(3):
            r = requests.put(url, headers=headers, data=data, timeout=60)
            if 200 <= r.status_code < 300:
                return r.json()
            last_err = f"{r.status_code} {r.text}"
        raise RuntimeError(f"Graph upload failed after retries: {last_err}")

    def upload_to_sharepoint_path(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        folder_path: str,
        filename: str,
        file_bytes: bytes,
    ) -> UploadedFileRef:
        token = self.token()

        site_url = f"https://graph.microsoft.com/v1.0/sites/{site_hostname}:{site_path}"
        site = requests.get(site_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        site.raise_for_status()
        site_id = site.json()["id"]

        drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        drives = requests.get(drives_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        drives.raise_for_status()

        drive_id = None
        for d in drives.json().get("value", []):
            if d.get("name") == library:
                drive_id = d["id"]
                break
        if not drive_id:
            raise RuntimeError("Could not find document library drive by name")

        safe_folder = folder_path.strip("/")
        put_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{safe_folder}/{filename}:/content"
        put_result = self._put_bytes(put_url, token, file_bytes)
        return UploadedFileRef(
            drive_id=drive_id,
            item_id=str(put_result.get("id", "")),
            web_url=put_result.get("webUrl"),
        )
