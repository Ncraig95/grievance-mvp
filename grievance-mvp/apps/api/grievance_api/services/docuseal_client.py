from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse
from uuid import uuid4

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
        web_base_url: str | None = None,
        web_email: str | None = None,
        web_password: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.web_base_url = (web_base_url or "").rstrip("/") or None
        self.web_email = (web_email or "").strip() or None
        self.web_password = (web_password or "").strip() or None

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()).strip("_.")
        return f"{cleaned or 'document'}.pdf"

    @staticmethod
    def _placeholder_patterns() -> dict[str, re.Pattern[str]]:
        return {
            # pdftotext -bbox often strips surrounding braces from placeholders, so
            # we match the inner marker token and normalize optional braces separately.
            "signature": re.compile(r"^Sig_es_:signer(\d+):signature$", re.IGNORECASE),
            "date": re.compile(r"^Dte_es_:signer(\d+):date$", re.IGNORECASE),
            "email": re.compile(r"^Eml_es_:signer(\d+):email$", re.IGNORECASE),
        }

    @staticmethod
    def _normalize_placeholder_token(token: str) -> str:
        cleaned = (token or "").strip()
        if cleaned.startswith("{{") and cleaned.endswith("}}"):
            cleaned = cleaned[2:-2]
        return cleaned.strip()

    def _headers(self, *, is_json: bool = True) -> dict:
        headers = {"X-Auth-Token": self.api_token}
        if is_json:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _extract_csrf_token(html_text: str) -> str:
        # Prefer form token first; fall back to meta token if form is not present.
        for pattern in (
            r'name="authenticity_token"\s+value="([^"]+)"',
            r'name="csrf-token"\s+content="([^"]+)"',
        ):
            match = re.search(pattern, html_text)
            if match:
                return unescape(match.group(1))
        raise RuntimeError("DocuSeal CSRF token not found in HTML response")

    @staticmethod
    def _json_object(resp: requests.Response) -> dict:
        payload = resp.json()
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    return item
        return {}

    def _clone_and_replace_template(
        self,
        *,
        base_template_id: str,
        upload_pdf_bytes: bytes,
        alignment_pdf_bytes: bytes | None,
        title: str,
    ) -> str:
        web_base = self.web_base_url or self.public_base_url
        if not web_base:
            raise RuntimeError(
                "DocuSeal clone-and-replace requires docuseal.public_base_url or docuseal.web_base_url"
            )
        if not self.web_email or not self.web_password:
            raise RuntimeError(
                "DocuSeal clone-and-replace requires web credentials. Set DOCUSEAL_WEB_EMAIL and DOCUSEAL_WEB_PASSWORD."
            )

        sess = requests.Session()

        sign_in_page = sess.get(f"{web_base}/sign_in", timeout=self.timeout)
        sign_in_page.raise_for_status()
        sign_in_token = self._extract_csrf_token(sign_in_page.text)

        sign_in_resp = sess.post(
            f"{web_base}/sign_in",
            data={
                "authenticity_token": sign_in_token,
                "user[email]": self.web_email,
                "user[password]": self.web_password,
            },
            allow_redirects=False,
            timeout=self.timeout,
        )
        if sign_in_resp.status_code not in {200, 302, 303}:
            raise RuntimeError(f"DocuSeal web sign-in failed: {sign_in_resp.status_code} {sign_in_resp.text[:400]}")
        if sign_in_resp.status_code == 200 and "Invalid Email or password" in sign_in_resp.text:
            raise RuntimeError("DocuSeal web sign-in failed: invalid credentials")

        edit_resp = sess.get(f"{web_base}/templates/{base_template_id}/edit", timeout=self.timeout)
        edit_resp.raise_for_status()
        csrf_token = self._extract_csrf_token(edit_resp.text)

        files = {
            "files[]": (
                self._safe_filename(title),
                upload_pdf_bytes,
                "application/pdf",
            )
        }
        clone_resp = sess.post(
            f"{web_base}/templates/{base_template_id}/clone_and_replace",
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf_token,
            },
            files=files,
            timeout=self.timeout,
        )
        if not (200 <= clone_resp.status_code < 300):
            raise RuntimeError(f"DocuSeal clone-and-replace failed: {clone_resp.status_code} {clone_resp.text[:400]}")

        obj = self._json_object(clone_resp)
        template_id = str(obj.get("id") or "").strip()
        if not template_id:
            raise RuntimeError("DocuSeal clone-and-replace response missing template id")
        self._apply_placeholder_field_alignment(
            template_id=template_id,
            pdf_bytes=alignment_pdf_bytes or upload_pdf_bytes,
        )
        return template_id

    def _extract_placeholder_areas(self, *, pdf_bytes: bytes) -> dict[tuple[int, str], list[dict]]:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as src:
            src.write(pdf_bytes)
            src.flush()
            proc = subprocess.run(
                ["pdftotext", "-bbox", src.name, "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        if proc.returncode != 0:
            return {}

        patterns = self._placeholder_patterns()
        page_re = re.compile(r'<page[^>]*width="([0-9.]+)"[^>]*height="([0-9.]+)"', re.IGNORECASE)
        word_re = re.compile(
            r'<word[^>]*xMin="([0-9.]+)"[^>]*yMin="([0-9.]+)"[^>]*xMax="([0-9.]+)"[^>]*yMax="([0-9.]+)"[^>]*>(.*?)</word>',
            re.IGNORECASE,
        )

        areas: dict[tuple[int, str], list[dict]] = {}
        page_index = -1
        page_w = 0.0
        page_h = 0.0

        for line in proc.stdout.splitlines():
            page_match = page_re.search(line)
            if page_match:
                page_index += 1
                page_w = float(page_match.group(1))
                page_h = float(page_match.group(2))
                continue

            word_match = word_re.search(line)
            if not word_match or page_index < 0:
                continue

            token = self._normalize_placeholder_token(unescape(word_match.group(5)))
            if not token:
                continue
            signer_index: int | None = None
            field_type: str | None = None
            for candidate_type, pattern in patterns.items():
                match = pattern.match(token)
                if match:
                    signer_index = int(match.group(1))
                    field_type = candidate_type
                    break
            if signer_index is None or field_type is None:
                continue

            areas.setdefault((signer_index, field_type), []).append(
                {
                    "x_min": float(word_match.group(1)),
                    "y_min": float(word_match.group(2)),
                    "x_max": float(word_match.group(3)),
                    "y_max": float(word_match.group(4)),
                    "page": page_index,
                    "page_w": page_w,
                    "page_h": page_h,
                }
            )

        return areas

    @staticmethod
    def _normalize_area(*, raw: dict, field_type: str, attachment_uuid: str) -> dict:
        word_w = max(1.0, raw["x_max"] - raw["x_min"])
        word_h = max(1.0, raw["y_max"] - raw["y_min"])

        if field_type == "signature":
            min_w, min_h, pad_w, pad_h = 140.0, 28.0, 8.0, 4.0
            y_lift = 14.0
        elif field_type == "email":
            min_w, min_h, pad_w, pad_h = 180.0, 18.0, 6.0, 2.0
            y_lift = 5.0
        else:
            min_w, min_h, pad_w, pad_h = 90.0, 18.0, 4.0, 2.0
            y_lift = 6.0

        x = max(0.0, raw["x_min"] - 2.0)
        y = max(0.0, raw["y_min"] - y_lift)
        w = max(min_w, word_w + 2 * pad_w)
        h = max(min_h, word_h + 2 * pad_h)

        if x + w > raw["page_w"]:
            w = max(1.0, raw["page_w"] - x)
        if y + h > raw["page_h"]:
            h = max(1.0, raw["page_h"] - y)

        return {
            "x": x / raw["page_w"],
            "y": y / raw["page_h"],
            "w": w / raw["page_w"],
            "h": h / raw["page_h"],
            "attachment_uuid": attachment_uuid,
            "page": int(raw["page"]),
        }

    def _apply_placeholder_field_alignment(self, *, template_id: str, pdf_bytes: bytes) -> None:
        placeholder_areas = self._extract_placeholder_areas(pdf_bytes=pdf_bytes)
        if not placeholder_areas:
            return

        template_resp = requests.get(
            f"{self.base_url}/api/templates/{template_id}",
            headers=self._headers(is_json=False),
            timeout=self.timeout,
        )
        if not (200 <= template_resp.status_code < 300):
            return

        template_obj = self._json_object(template_resp)
        submitters = template_obj.get("submitters")
        schema = template_obj.get("schema")
        if not isinstance(submitters, list) or not submitters:
            return
        if not isinstance(schema, list) or not schema or not isinstance(schema[0], dict):
            return
        attachment_uuid = str(schema[0].get("attachment_uuid") or "").strip()
        if not attachment_uuid:
            return

        rebuilt_fields: list[dict] = []
        signer_indexes = sorted({signer_idx for signer_idx, _ in placeholder_areas.keys()})
        for signer_idx in signer_indexes:
            submitter_pos = signer_idx - 1
            if submitter_pos < 0 or submitter_pos >= len(submitters):
                continue
            submitter = submitters[submitter_pos]
            if not isinstance(submitter, dict):
                continue
            submitter_uuid = str(submitter.get("uuid") or "").strip()
            if not submitter_uuid:
                continue

            for field_type in ("signature", "date", "email"):
                anchors = placeholder_areas.get((signer_idx, field_type), [])
                if not anchors:
                    continue
                field_name = f"signer{signer_idx}_email" if field_type == "email" else ""
                field_payload = {
                    "uuid": str(uuid4()),
                    "submitter_uuid": submitter_uuid,
                    "name": field_name,
                    "type": "text" if field_type == "email" else field_type,
                    "required": field_type != "email",
                    "preferences": {},
                    "areas": [
                        self._normalize_area(
                            raw=anchor,
                            field_type=field_type,
                            attachment_uuid=attachment_uuid,
                        )
                        for anchor in anchors
                    ],
                }
                if field_type == "email":
                    field_payload["readonly"] = True
                rebuilt_fields.append(field_payload)

        if not rebuilt_fields:
            return

        requests.patch(
            f"{self.base_url}/api/templates/{template_id}",
            headers=self._headers(is_json=True),
            json={"fields": rebuilt_fields},
            timeout=self.timeout,
        )

    def _resolve_signer_email_fields(self, *, template_id: str) -> dict[int, str]:
        try:
            resp = requests.get(
                f"{self.base_url}/api/templates/{template_id}",
                headers=self._headers(is_json=False),
                timeout=self.timeout,
            )
        except Exception:
            return {}
        if not (200 <= resp.status_code < 300):
            return {}

        template_obj = self._json_object(resp)
        fields = template_obj.get("fields")
        submitters = template_obj.get("submitters")
        if not isinstance(fields, list) or not isinstance(submitters, list):
            return {}

        uuid_to_signer_index: dict[str, int] = {}
        for idx, submitter in enumerate(submitters, start=1):
            if not isinstance(submitter, dict):
                continue
            submitter_uuid = str(submitter.get("uuid") or "").strip()
            if submitter_uuid:
                uuid_to_signer_index[submitter_uuid] = idx

        signer_email_fields: dict[int, str] = {}
        name_re = re.compile(r"^signer(\d+)_email$", re.IGNORECASE)
        for field in fields:
            if not isinstance(field, dict):
                continue
            if str(field.get("type") or "").strip().lower() != "text":
                continue
            field_name = str(field.get("name") or "").strip()
            match = name_re.match(field_name)
            if not match:
                continue
            signer_idx = int(match.group(1))
            submitter_uuid = str(field.get("submitter_uuid") or "").strip()
            if submitter_uuid and submitter_uuid in uuid_to_signer_index:
                signer_idx = uuid_to_signer_index[submitter_uuid]
            signer_email_fields[signer_idx] = field_name

        return signer_email_fields

    @staticmethod
    def _build_submitters_payload(signers: list[str], signer_email_fields: dict[int, str]) -> list[dict]:
        submitters_payload: list[dict] = []
        for idx, signer_email in enumerate(signers, start=1):
            item: dict[str, object] = {"email": signer_email}
            email_field_name = signer_email_fields.get(idx)
            if email_field_name:
                item["values"] = {email_field_name: signer_email}
                item["readonly_fields"] = [email_field_name]
            submitters_payload.append(item)
        return submitters_payload

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
        def _from_slug(slug: str) -> str:
            base = (self.public_base_url or self.base_url).rstrip("/")
            return f"{base}/s/{slug.strip('/')}"

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
                slug = entry.get("slug")
                if isinstance(slug, str) and slug.strip():
                    return self._rewrite_public_url(_from_slug(slug))
        for key in ("url", "signing_url", "submitter_url"):
            val = submission.get(key)
            if isinstance(val, str) and val.strip():
                return self._rewrite_public_url(val.strip())
        slug = submission.get("slug")
        if isinstance(slug, str) and slug.strip():
            return self._rewrite_public_url(_from_slug(slug))
        return None

    @staticmethod
    def _first_object(payload: object) -> dict:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    return item
        return {}

    def create_submission(
        self,
        *,
        pdf_bytes: bytes,
        alignment_pdf_bytes: bytes | None = None,
        signers: list[str],
        title: str,
        metadata: dict[str, str] | None = None,
        template_id: int | None = None,
    ) -> DocuSealSubmission:
        if not signers:
            raise RuntimeError("DocuSeal submission requires at least one signer")

        selected_template_id: str | None = str(template_id).strip() if template_id is not None else None
        if selected_template_id:
            selected_template_id = self._clone_and_replace_template(
                base_template_id=selected_template_id,
                upload_pdf_bytes=pdf_bytes,
                alignment_pdf_bytes=alignment_pdf_bytes,
                title=title,
            )
        else:
            files = {"files[0]": ("document.pdf", pdf_bytes, "application/pdf")}
            create_template = requests.post(
                f"{self.base_url}/api/templates",
                headers=self._headers(is_json=False),
                files=files,
                timeout=self.timeout,
            )
            if 200 <= create_template.status_code < 300:
                template_obj = self._first_object(create_template.json())
                selected_template_id = str(template_obj.get("id") or template_obj.get("template_id") or "").strip()
            else:
                details = create_template.text[:400]
                if create_template.status_code in {404, 422} and "Pro Edition" in details:
                    raise RuntimeError(
                        "DocuSeal API template upload is unavailable on this deployment. "
                        "Configure docuseal.default_template_id and DOCUSEAL_WEB_EMAIL/DOCUSEAL_WEB_PASSWORD."
                    )
                raise RuntimeError(f"DocuSeal template create failed: {create_template.status_code} {details}")

        if not selected_template_id:
            raise RuntimeError(
                "DocuSeal template resolution failed. Configure docuseal.default_template_id/template_ids."
            )

        signer_email_fields = self._resolve_signer_email_fields(template_id=selected_template_id)
        signer_objs = self._build_submitters_payload(signers, signer_email_fields)
        plain_signer_objs = [{"email": s} for s in signers]
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
                "submitters": plain_signer_objs,
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
                submission = self._first_object(resp.json())
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
