from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
import zipfile

from .docuseal_client import DocuSealSubmission
from .graph_mail import MailAttachment, SentGraphMail
from .sharepoint_graph import (
    CaseFolderAmbiguousError,
    CaseFolderNotFoundError,
    CaseFolderRef,
    DirectoryUserRef,
    SharePointFileRef,
    UploadedFileRef,
)


class LocalGraphMailer:
    """Local-safe Graph mail replacement that writes messages to disk."""

    local_provider = True

    def __init__(self, *, data_root: str, sender_user_id: str):
        self.data_root = Path(data_root)
        self.sender_user_id = sender_user_id
        self.root = self.data_root / "local_mock" / "mail"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
        return cleaned.strip("._") or "attachment"

    @staticmethod
    def _normalized_recipients(to_recipients: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in to_recipients:
            email = str(raw or "").strip()
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            out.append(email)
        return out

    def _write_message(
        self,
        *,
        mode: str,
        to_recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None,
        attachments: list[MailAttachment] | None,
        custom_headers: dict[str, str] | None,
        extra: dict[str, object] | None = None,
    ) -> SentGraphMail:
        recipients = self._normalized_recipients(to_recipients)
        if not recipients:
            raise RuntimeError("local mail send has no valid recipients")

        message_id = f"local-mail-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:12]}"
        internet_message_id = f"<{message_id}@local.invalid>"
        message_dir = self.root / message_id
        attachments_dir = message_dir / "attachments"
        message_dir.mkdir(parents=True, exist_ok=True)

        attachment_rows: list[dict[str, object]] = []
        for index, attachment in enumerate(attachments or [], start=1):
            filename = self._safe_filename(attachment.filename)
            target = attachments_dir / f"{index:02d}-{filename}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(attachment.content_bytes)
            attachment_rows.append(
                {
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size_bytes": len(attachment.content_bytes),
                    "path": str(target),
                    "sha256": hashlib.sha256(attachment.content_bytes).hexdigest(),
                }
            )

        payload: dict[str, object] = {
            "mode": mode,
            "graph_message_id": message_id,
            "internet_message_id": internet_message_id,
            "sender_user_id": self.sender_user_id,
            "to_recipients": recipients,
            "subject": subject,
            "text_body": text_body,
            "html_body": html_body,
            "custom_headers": custom_headers or {},
            "attachments": attachment_rows,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)
        (message_dir / "message.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return SentGraphMail(graph_message_id=message_id, internet_message_id=internet_message_id)

    def send_mail(
        self,
        *,
        to_recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
        attachments: list[MailAttachment] | None = None,
        custom_headers: dict[str, str] | None = None,
    ) -> SentGraphMail:
        return self._write_message(
            mode="json",
            to_recipients=to_recipients,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            attachments=attachments,
            custom_headers=custom_headers,
        )

    def send_mime_mail(
        self,
        *,
        to_recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
        attachments: list[MailAttachment] | None = None,
        custom_headers: dict[str, str] | None = None,
        from_display_name: str | None = None,
        reply_to_address: str | None = None,
        reply_to_name: str | None = None,
    ) -> SentGraphMail:
        return self._write_message(
            mode="mime",
            to_recipients=to_recipients,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            attachments=attachments,
            custom_headers=custom_headers,
            extra={
                "from_display_name": from_display_name,
                "reply_to_address": reply_to_address,
                "reply_to_name": reply_to_name,
            },
        )


class LocalSharePointUploader:
    """Local-safe SharePoint replacement backed by folders under data_root."""

    local_provider = True
    dry_run = False

    def __init__(self, *, data_root: str):
        self.data_root = Path(data_root)
        self.root = self.data_root / "local_mock" / "sharepoint"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_part(value: str) -> str:
        text = str(value or "").strip().replace("\\", "_").replace("/", "_")
        if text in {"", ".", ".."}:
            return "_"
        return text

    @staticmethod
    def _split_path(value: str) -> list[str]:
        return [part.strip() for part in str(value or "").replace("\\", "/").split("/") if part.strip()]

    @staticmethod
    def _matches_grievance_id_prefix(*, grievance_id: str, folder_name: str) -> bool:
        wanted = (grievance_id or "").strip().lower()
        candidate = (folder_name or "").strip().lower()
        return bool(wanted and candidate and (candidate == wanted or candidate.startswith(f"{wanted} ")))

    def _library_root(self, library: str) -> Path:
        root = self.root / self._safe_part(library or "Documents")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _drive_id(self, library: str) -> str:
        return f"local-drive:{self._safe_part(library or 'Documents')}"

    @staticmethod
    def _library_from_drive_id(drive_id: str) -> str:
        prefix = "local-drive:"
        if str(drive_id or "").startswith(prefix):
            return str(drive_id)[len(prefix) :]
        return "Documents"

    def _ensure_folder(self, *, library: str, folder_path: str) -> tuple[Path, str]:
        cur = self._library_root(library)
        normalized_parts: list[str] = []
        for part in self._split_path(folder_path):
            safe = self._safe_part(part)
            cur = cur / safe
            cur.mkdir(parents=True, exist_ok=True)
            normalized_parts.append(safe)
        return cur, "/".join(normalized_parts)

    def _resolve_folder(self, *, library: str, folder_path: str) -> tuple[Path, str] | None:
        cur = self._library_root(library)
        normalized_parts: list[str] = []
        for part in self._split_path(folder_path):
            safe = self._safe_part(part)
            cur = cur / safe
            if not cur.is_dir():
                return None
            normalized_parts.append(safe)
        return cur, "/".join(normalized_parts)

    @staticmethod
    def _web_url(path: str) -> str:
        return "local://sharepoint/" + quote(path.strip("/"), safe="/")

    @staticmethod
    def _item_id(path: str) -> str:
        return path.strip("/")

    def search_directory_users(self, search_text: str, *, limit: int = 10) -> list[DirectoryUserRef]:
        _ = (search_text, limit)
        return []

    def list_licensed_directory_users(self, *, limit: int = 999) -> list[DirectoryUserRef]:
        _ = limit
        return []

    def list_case_folder_names(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        case_parent_folder: str,
    ) -> list[str]:
        _ = (site_hostname, site_path)
        parent, _normalized = self._ensure_folder(library=library, folder_path=case_parent_folder)
        return sorted(child.name for child in parent.iterdir() if child.is_dir())

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
        _ = (site_hostname, site_path)
        parent, parent_path = self._ensure_folder(library=library, folder_path=case_parent_folder)
        desired_name = f"{grievance_id} {member_name}".strip()
        wanted_token = grievance_id.lower().strip()
        selected: Path | None = None
        exact: Path | None = None
        prefix: Path | None = None
        contains: Path | None = None
        for child in sorted(parent.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            lowered = child.name.lower()
            if lowered == desired_name.lower():
                exact = child
                break
            if wanted_token and (lowered == wanted_token or lowered.startswith(f"{wanted_token} ")) and prefix is None:
                prefix = child
                continue
            if wanted_token and wanted_token in lowered and contains is None:
                contains = child
        selected = exact or prefix or contains
        if selected is None:
            selected = parent / self._safe_part(desired_name)
            selected.mkdir(parents=True, exist_ok=True)
        folder_path = "/".join(part for part in [parent_path, selected.name] if part)
        return CaseFolderRef(
            drive_id=self._drive_id(library),
            folder_id=folder_path,
            folder_name=selected.name,
            web_url=self._web_url(folder_path),
        )

    def find_case_folder_by_grievance_id_exact(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        case_parent_folder: str,
        grievance_id: str,
    ) -> CaseFolderRef:
        _ = (site_hostname, site_path)
        parent, parent_path = self._ensure_folder(library=library, folder_path=case_parent_folder)
        matches = [
            child
            for child in sorted(parent.iterdir(), key=lambda item: item.name.lower())
            if child.is_dir() and self._matches_grievance_id_prefix(grievance_id=grievance_id, folder_name=child.name)
        ]
        if not matches:
            raise CaseFolderNotFoundError(
                f"no case folder matched grievance_id '{grievance_id}' in '{case_parent_folder}'"
            )
        if len(matches) > 1:
            raise CaseFolderAmbiguousError(grievance_id, [item.name for item in matches])
        folder_path = "/".join(part for part in [parent_path, matches[0].name] if part)
        return CaseFolderRef(
            drive_id=self._drive_id(library),
            folder_id=folder_path,
            folder_name=matches[0].name,
            web_url=self._web_url(folder_path),
        )

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
        _ = (site_hostname, site_path)
        folder_path = "/".join(
            part.strip("/")
            for part in (case_parent_folder, case_folder_name, subfolder)
            if part and part.strip("/")
        )
        folder, normalized_folder = self._ensure_folder(library=library, folder_path=folder_path)
        safe_filename = self._safe_part(filename)
        target = folder / safe_filename
        target.write_bytes(file_bytes)
        normalized_path = "/".join(part for part in [normalized_folder, safe_filename] if part)
        return UploadedFileRef(
            drive_id=self._drive_id(library),
            item_id=self._item_id(normalized_path),
            web_url=self._web_url(normalized_path),
            path=normalized_path,
        )

    def upload_local_file_to_case_subfolder(self, *, local_path: str, **kwargs) -> UploadedFileRef:  # noqa: ANN003
        return self.upload_to_case_subfolder(file_bytes=Path(local_path).read_bytes(), **kwargs)

    def upload_local_file_to_folder_path(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        folder_path: str,
        filename: str,
        local_path: str,
    ) -> UploadedFileRef:
        _ = (site_hostname, site_path)
        folder, normalized_folder = self._ensure_folder(library=library, folder_path=folder_path)
        safe_filename = self._safe_part(filename)
        target = folder / safe_filename
        target.write_bytes(Path(local_path).read_bytes())
        normalized_path = "/".join(part for part in [normalized_folder, safe_filename] if part)
        return UploadedFileRef(
            drive_id=self._drive_id(library),
            item_id=self._item_id(normalized_path),
            web_url=self._web_url(normalized_path),
            path=normalized_path,
        )

    def list_files_in_folder_path(
        self,
        *,
        site_hostname: str,
        site_path: str,
        library: str,
        folder_path: str,
        recursive: bool = False,
    ) -> list[SharePointFileRef]:
        _ = (site_hostname, site_path)
        resolved = self._resolve_folder(library=library, folder_path=folder_path)
        if resolved is None:
            return []
        folder, normalized_folder = resolved
        files = sorted(folder.rglob("*") if recursive else folder.iterdir(), key=lambda item: str(item).lower())
        rows: list[SharePointFileRef] = []
        for item in files:
            if not item.is_file():
                continue
            rel = item.relative_to(self._library_root(library)).as_posix()
            rows.append(
                SharePointFileRef(
                    drive_id=self._drive_id(library),
                    item_id=self._item_id(rel),
                    name=item.name,
                    web_url=self._web_url(rel),
                    path=rel if recursive else "/".join(part for part in [normalized_folder, item.name] if part),
                )
            )
        return rows

    def download_item_bytes(self, *, drive_id: str, item_id: str) -> bytes:
        library = self._library_from_drive_id(drive_id)
        target = self._library_root(library) / item_id.strip("/")
        if not target.is_file():
            return b""
        return target.read_bytes()

    def delete_item(self, *, drive_id: str, item_id: str) -> None:
        library = self._library_from_drive_id(drive_id)
        target = self._library_root(library) / item_id.strip("/")
        if target.exists() and target.is_file():
            target.unlink()

    def convert_local_docx_to_pdf_bytes(self, **kwargs) -> bytes:  # noqa: ANN003
        _ = kwargs
        raise RuntimeError("local SharePoint mock does not provide Word Online conversion")


class LocalDocuSealClient:
    """Local-safe DocuSeal replacement backed by files under data_root."""

    local_provider = True

    def __init__(self, *, data_root: str, public_base_url: str | None = None):
        self.data_root = Path(data_root)
        self.public_base_url = (public_base_url or "local://docuseal").rstrip("/")
        self.root = self.data_root / "local_mock" / "docuseal" / "submissions"
        self.root.mkdir(parents=True, exist_ok=True)

    def _submission_dir(self, submission_id: str) -> Path:
        return self.root / self._safe_id(submission_id)

    @staticmethod
    def _safe_id(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "").strip())
        return cleaned.strip("._") or "local-submission"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _metadata_path(self, submission_id: str) -> Path:
        return self._submission_dir(submission_id) / "metadata.json"

    def _read_metadata(self, submission_id: str) -> dict[str, object]:
        path = self._metadata_path(submission_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_metadata(self, submission_id: str, payload: dict[str, object]) -> None:
        sub_dir = self._submission_dir(submission_id)
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "metadata.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _signing_link(self, *, submission_id: str, slug: str) -> str:
        return f"local://docuseal/submissions/{quote(submission_id, safe='')}/sign/{quote(slug, safe='')}"

    def create_submission(
        self,
        *,
        pdf_bytes: bytes,
        alignment_pdf_bytes: bytes | None = None,
        signers: list[str],
        title: str,
        metadata: dict[str, str] | None = None,
        template_id: int | None = None,
        form_key: str | None = None,
    ) -> DocuSealSubmission:
        normalized_signers = [str(email).strip() for email in signers if str(email).strip()]
        if not normalized_signers:
            raise RuntimeError("local DocuSeal submission requires at least one signer")

        submission_id = f"local-sub-{uuid4().hex[:12]}"
        sub_dir = self._submission_dir(submission_id)
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "submitted.pdf").write_bytes(pdf_bytes)
        if alignment_pdf_bytes:
            (sub_dir / "alignment.pdf").write_bytes(alignment_pdf_bytes)

        submitters: list[dict[str, object]] = []
        for index, email in enumerate(normalized_signers, start=1):
            slug = f"{submission_id}-signer{index}"
            submitters.append(
                {
                    "id": f"local-submitter-{submission_id}-{index}",
                    "email": email,
                    "slug": slug,
                    "status": "pending",
                    "url": self._signing_link(submission_id=submission_id, slug=slug),
                    "signing_url": self._signing_link(submission_id=submission_id, slug=slug),
                }
            )

        raw: dict[str, object] = {
            "id": submission_id,
            "submission_id": submission_id,
            "status": "pending",
            "name": title,
            "submitters": submitters,
            "metadata": metadata or {},
        }
        stored = {
            **raw,
            "template_id": str(template_id or "local-template"),
            "form_key": form_key or "",
            "created_at_utc": self._now(),
            "completed_at_utc": None,
        }
        self._write_metadata(submission_id, stored)
        first_link = str(submitters[0]["signing_url"])
        return DocuSealSubmission(
            submission_id=submission_id,
            signing_link=first_link,
            template_id=str(template_id or "local-template"),
            raw=raw,
        )

    def extract_signing_links_by_email(self, raw: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for submitter in raw.get("submitters", []) if isinstance(raw, dict) else []:
            if not isinstance(submitter, dict):
                continue
            email = str(submitter.get("email") or "").strip().lower()
            link = str(submitter.get("signing_url") or submitter.get("url") or "").strip()
            if email and link:
                out[email] = link
        return out

    def fetch_signing_links_by_email(self, *, submission_id: str) -> dict[str, str]:
        return self.extract_signing_links_by_email(self._read_metadata(submission_id))

    def list_submitters(self, *, submission_id: str) -> list[dict[str, object]]:
        payload = self._read_metadata(submission_id)
        submitters = payload.get("submitters")
        if isinstance(submitters, list):
            return [dict(item) for item in submitters if isinstance(item, dict)]
        return []

    def update_submitter(
        self,
        *,
        submitter_id: str | int,
        email: str | None = None,
        send_email: bool = False,
        completed: bool | None = None,
        fields: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        _ = send_email
        submitter_key = str(submitter_id or "").strip()
        for metadata_path in self.root.glob("*/metadata.json"):
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            submitters = payload.get("submitters")
            if not isinstance(submitters, list):
                continue
            for submitter in submitters:
                if not isinstance(submitter, dict):
                    continue
                if str(submitter.get("id") or "") != submitter_key:
                    continue
                if email:
                    submitter["email"] = str(email).strip()
                if completed is not None:
                    submitter["status"] = "completed" if completed else "pending"
                    if completed:
                        submitter["completed_at"] = self._now()
                if fields is not None:
                    submitter["fields"] = fields
                submission_id = str(payload.get("submission_id") or payload.get("id") or metadata_path.parent.name)
                self._write_metadata(submission_id, payload)
                return dict(submitter)
        raise RuntimeError(f"local DocuSeal submitter not found: {submitter_id}")

    def auto_complete_submitter(self, *, submitter_id: str | int, fields: list[dict[str, object]]) -> dict[str, object]:
        return self.update_submitter(submitter_id=submitter_id, send_email=False, completed=True, fields=fields)

    def delete_submission(self, *, submission_id: str, permanently: bool = True) -> dict[str, object]:
        _ = permanently
        return {"ok": True, "already_missing": not self._submission_dir(submission_id).exists(), "status_code": 200}

    def download_completed_artifacts(self, *, submission_id: str) -> dict[str, object]:
        payload = self._read_metadata(submission_id)
        if not payload:
            raise RuntimeError(f"local DocuSeal submission not found: {submission_id}")
        sub_dir = self._submission_dir(submission_id)
        submitted = sub_dir / "submitted.pdf"
        if not submitted.exists():
            raise RuntimeError(f"local DocuSeal submitted PDF missing: {submission_id}")
        signed_pdf = sub_dir / "signed.pdf"
        if not signed_pdf.exists():
            signed_pdf.write_bytes(submitted.read_bytes())

        for submitter in payload.get("submitters", []):
            if isinstance(submitter, dict):
                submitter["status"] = "completed"
                submitter.setdefault("completed_at", self._now())
        payload["status"] = "completed"
        payload["completed_at_utc"] = payload.get("completed_at_utc") or self._now()
        self._write_metadata(submission_id, payload)

        audit = {
            "submission_id": submission_id,
            "status": "completed",
            "completed_at_utc": payload["completed_at_utc"],
            "provider": "local_docuseal",
        }
        (sub_dir / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("signed.pdf", signed_pdf.read_bytes())
            zf.writestr("audit.json", json.dumps(audit, indent=2, sort_keys=True))
        zip_bytes = buf.getvalue()
        (sub_dir / "completed.zip").write_bytes(zip_bytes)
        return {
            "completed_zip_bytes": zip_bytes,
            "signed_pdf_bytes": signed_pdf.read_bytes(),
            "documents": {"documents": [{"name": "signed.pdf", "url": f"local://docuseal/submissions/{submission_id}/signed.pdf"}]},
            "submission": payload,
        }
