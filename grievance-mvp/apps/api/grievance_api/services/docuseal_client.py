
from __future__ import annotations

import requests

class DocuSealClient:
    def __init__(self, base_url: str, api_token: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout

    def _headers(self, is_json: bool = True) -> dict:
        headers = {"X-Auth-Token": self.api_token}
        if is_json:
            headers["Content-Type"] = "application/json"
        return headers

    def create_submission(self, *, pdf_bytes: bytes, signers: list[str], title: str) -> dict:
        # 1. Create a template
        files = {"files[0]": ("document.pdf", pdf_bytes, "application/pdf")}
        response = requests.post(
            f"{self.base_url}/api/templates",
            headers=self._headers(is_json=False),
            files=files,
            timeout=self.timeout,
        )
        response.raise_for_status()
        template_id = response.json()["id"]

        # 2. Create a submission from the template
        payload = {
            "template_id": template_id,
            "signers": signers,
            "title": title,
            "send_email": False, # We will send emails ourselves
        }
        response = requests.post(
            f"{self.base_url}/api/submissions",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        submission = response.json()

        # 3. Get the signing link
        signing_link = None
        if len(submission["signers"]) > 0:
            signing_link = submission["signers"][0]["url"]

        return {
            "submission_id": submission["id"],
            "signing_link": signing_link,
        }

    def download_completed_artifacts(self, *, submission_id: str) -> dict:
        response = requests.get(
            f"{self.base_url}/api/submissions/{submission_id}/completed.zip",
            headers=self._headers(is_json=False),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return {"completed_zip_bytes": response.content}

