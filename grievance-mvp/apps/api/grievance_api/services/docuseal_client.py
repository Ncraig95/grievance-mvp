from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class DocuSealSubmission:
    submission_id: str
    signing_link: str | None
    template_id: str | None
    raw: dict


class DocuSealClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout: int = 30,
        public_base_url: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.public_base_url = (public_base_url or "").rstrip("/") or None

    def _headers(self, *, is_json: bool = True) -> dict:
        headers = {"X-Auth-Token": self.api_token}
        if is_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _rewrite_public_url(self, value: str | None) -> str | None:
        if not value:
            return value
        if not self.public_base_url:
            return value
        parsed = urlparse(value)
        if parsed.hostname not in {"127.0.0.1", "localhost", "docuseal"}:
            return value
        pub = urlparse(self.public_base_url)
        rebuilt = parsed._replace(scheme=pub.scheme, netloc=pub.netloc)
        return rebuilt.geturl()

    def _extract_signing_link(self, submission: dict) -> str | None:
        for key in ("submitters", "signers"):
            raw = submission.get(key)
            if not isinstance(raw, list):
                continue
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                for link_key in ("url", "signing_url", "link"):
                    val = entry.get(link_key)
                    if isinstance(val, str) and val.strip():
                        return self._rewrite_public_url(val.strip())
        for key in ("url", "signing_url", "submitter_url"):
            val = submission.get(key)
            if isinstance(val, str) and val.strip():
                return self._rewrite_public_url(val.strip())
        return None

    def create_submission(
        self,
        *,
        pdf_bytes: bytes,
        signers: list[str],
        title: str,
        metadata: dict[str, str] | None = None,
        template_id: int | None = None,
    ) -> DocuSealSubmission:
        if not signers:
            raise RuntimeError("DocuSeal submission requires at least one signer")

        selected_template_id: str | None = str(template_id) if template_id is not None else None
        if not selected_template_id:
            files = {"files[0]": ("document.pdf", pdf_bytes, "application/pdf")}
            create_template = requests.post(
                f"{self.base_url}/api/templates",
                headers=self._headers(is_json=False),
                files=files,
                timeout=self.timeout,
            )
            if 200 <= create_template.status_code < 300:
                template_obj = create_template.json()
                selected_template_id = str(template_obj.get("id") or template_obj.get("template_id") or "")
            elif create_template.status_code == 404:
                # API template creation may be disabled in some deployments; use an existing template id instead.
                templates = requests.get(
                    f"{self.base_url}/api/templates",
                    headers=self._headers(is_json=False),
                    timeout=self.timeout,
                )
                templates.raise_for_status()
                data = templates.json().get("data", [])
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    fields = item.get("fields")
                    if isinstance(fields, list) and fields:
                        selected_template_id = str(item.get("id") or "")
                        break
            else:
                create_template.raise_for_status()

        if not selected_template_id:
            raise RuntimeError(
                "DocuSeal template resolution failed. Configure docuseal.default_template_id/template_ids with a template containing signature fields."
            )

        signer_objs = [{"email": s} for s in signers]
        payload_variants = [
            {
                "template_id": selected_template_id,
                "submitters": signer_objs,
                "name": title,
                "send_email": False,
                "metadata": metadata or {},
            },
            {
                "template_id": selected_template_id,
                "signers": signers,
                "title": title,
                "send_email": False,
                "metadata": metadata or {},
            },
        ]

        last_err: str | None = None
        submission: dict | None = None
        for payload in payload_variants:
            resp = requests.post(
                f"{self.base_url}/api/submissions",
                headers=self._headers(is_json=True),
                json=payload,
                timeout=self.timeout,
            )
            if 200 <= resp.status_code < 300:
                submission = resp.json()
                break
            last_err = f"{resp.status_code} {resp.text[:400]}"

        if submission is None:
            raise RuntimeError(f"DocuSeal submission create failed: {last_err}")

        submission_id = str(
            submission.get("id")
            or submission.get("submission_id")
            or submission.get("submissionId")
            or ""
        )
        if not submission_id:
            raise RuntimeError("DocuSeal submission response missing id")

        return DocuSealSubmission(
            submission_id=submission_id,
            signing_link=self._extract_signing_link(submission),
            template_id=selected_template_id,
            raw=submission,
        )

    def download_completed_artifacts(self, *, submission_id: str) -> dict:
        zip_bytes: bytes | None = None
        last_err: str | None = None
        for path in (
            f"/api/submissions/{submission_id}/completed.zip",
            f"/api/submissions/{submission_id}/download",
        ):
            resp = requests.get(
                f"{self.base_url}{path}",
                headers=self._headers(is_json=False),
                timeout=self.timeout,
            )
            if 200 <= resp.status_code < 300 and resp.content:
                zip_bytes = resp.content
                break
            last_err = f"{resp.status_code} {resp.text[:400]}"

        details: dict | None = None
        info = requests.get(
            f"{self.base_url}/api/submissions/{submission_id}",
            headers=self._headers(is_json=False),
            timeout=self.timeout,
        )
        if 200 <= info.status_code < 300:
            try:
                details = info.json()
            except Exception:
                details = None

        if zip_bytes is None and details is None:
            raise RuntimeError(f"DocuSeal artifact download failed: {last_err}")

        return {
            "completed_zip_bytes": zip_bytes,
            "submission": details,
        }
