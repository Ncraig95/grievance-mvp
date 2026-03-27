from __future__ import annotations

import io
import logging
import re
import subprocess
import tempfile
from datetime import date
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse
from uuid import uuid4

import requests

try:
    import pdfplumber
except Exception:  # pragma: no cover - optional runtime import in tests/dev
    pdfplumber = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DocuSealSubmission:
    submission_id: str
    signing_link: str | None
    template_id: str | None
    raw: dict


_TEXT_FIELD_DIMENSION_HINTS: dict[str, dict[str, float]] = {
    # 3G3A stage-owned fill fields.
    # Keep multiline blocks constrained to their row bands; avoid oversized overlays.
    "q6_company_statement": {"min_w": 430.0, "min_h": 46.0, "max_h": 56.0},
    "q7_proposed_disposition_second_level": {"min_w": 430.0, "min_h": 46.0, "max_h": 56.0},
    "q7_company_rep_name_attuid": {"min_w": 220.0, "min_h": 20.0, "max_h": 24.0, "y_lift": 6.0},
    "q8_union_disposition": {"min_w": 240.0, "min_h": 30.0, "max_h": 42.0},
    "article_affected": {"min_w": 176.0, "min_h": 18.0, "max_h": 20.0, "y_lift": 4.0},
}
_DATE_FIELD_DIMENSION_HINTS: dict[str, dict[str, float]] = {
    "q5_l2_date": {"min_w": 140.0, "min_h": 20.0, "max_h": 28.0, "y_lift": 6.0},
    "date_true_intent": {"min_w": 112.0, "min_h": 12.0, "max_h": 16.0, "y_lift": 2.0},
}
_FORM_SIGNATURE_FIELD_DIMENSION_HINTS: dict[str, dict[str, dict[str, float]]] = {
    "att_mobility_bargaining_suggestion": {
        "signer1_signature": {"min_h": 14.0, "max_h": 16.0, "y_lift": 16.0},
    },
}
_STATEMENT_SIGNER_DATE_HINTS: dict[str, dict[str, float]] = {
    # Statement form date placeholders sit close to baseline; keep lift/padding tight.
    "signer1_date": {"min_w": 112.0, "min_h": 12.0, "max_h": 14.0, "y_lift": 2.0},
    "signer2_date": {"min_w": 112.0, "min_h": 12.0, "max_h": 14.0, "y_lift": 2.0},
    "signer3_date": {"min_w": 112.0, "min_h": 12.0, "max_h": 14.0, "y_lift": 2.0},
}
_FORM_DATE_FIELD_DIMENSION_HINTS: dict[str, dict[str, dict[str, float]]] = {
    "statement_of_occurrence": _STATEMENT_SIGNER_DATE_HINTS,
    "grievance_form": _STATEMENT_SIGNER_DATE_HINTS,
}
_MULTILINE_TEXT_FIELDS = {
    "q6_company_statement",
    "q7_proposed_disposition_second_level",
    "q8_union_disposition",
}
_MULTILINE_TEXT_PREFERENCES: dict[str, object] = {
    # DocuSeal overlay text wraps reliably only when text spans are width-bound.
    # Setting explicit left/top alignment enables w-full render classes.
    "multiline": True,
    "align": "left",
    "valign": "top",
}
_CHECKBOX_GLYPHS = {"☐", "☒", "☑", "□"}


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
        submitters_order: str = "preserved",
        submitters_order_by_form: dict[str, str] | None = None,
        signature_layout_mode: str = "table_preferred",
        signature_layout_mode_by_form: dict[str, str] | None = None,
        signature_table_trace_enabled: bool = True,
        signature_table_trace_by_form: dict[str, bool] | None = None,
        signature_table_guard_enabled: bool = True,
        signature_table_guard_tolerance: float = 0.015,
        signature_table_guard_min_gap: float = 0.005,
        signature_table_maps: dict[str, dict[str, dict[str, float | int]]] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.logger = logging.getLogger("grievance_api.docuseal")
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.web_base_url = (web_base_url or "").rstrip("/") or None
        self.web_email = (web_email or "").strip() or None
        self.web_password = (web_password or "").strip() or None
        self.submitters_order = self._normalize_submitters_order(submitters_order)
        raw_submitters_order_by_form = submitters_order_by_form or {}
        self.submitters_order_by_form = {
            str(k).strip(): self._normalize_submitters_order(v)
            for k, v in raw_submitters_order_by_form.items()
            if str(k).strip()
        }
        self.signature_layout_mode = self._normalize_signature_layout_mode(signature_layout_mode)
        raw_layout_map = signature_layout_mode_by_form or {}
        self.signature_layout_mode_by_form = {
            str(k).strip(): self._normalize_signature_layout_mode(v)
            for k, v in raw_layout_map.items()
            if str(k).strip()
        }
        self.signature_table_trace_enabled = bool(signature_table_trace_enabled)
        raw_trace_by_form = signature_table_trace_by_form or {}
        self.signature_table_trace_by_form = {
            str(k).strip(): bool(v)
            for k, v in raw_trace_by_form.items()
            if str(k).strip()
        }
        self.signature_table_guard_enabled = bool(signature_table_guard_enabled)
        self.signature_table_guard_tolerance = max(0.0, float(signature_table_guard_tolerance or 0.015))
        self.signature_table_guard_min_gap = max(0.0, float(signature_table_guard_min_gap or 0.005))
        self.signature_table_maps = self._normalize_signature_table_maps(signature_table_maps or {})

    @staticmethod
    def _safe_filename(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()).strip("_.")
        return f"{cleaned or 'document'}.pdf"

    @staticmethod
    def _placeholder_patterns() -> dict[str, re.Pattern[str]]:
        return {
            # pdftotext -bbox often strips surrounding braces from placeholders, so
            # we match the inner marker token and normalize optional braces separately.
            "signature": re.compile(r"^Sig_es_:signer(\d+):([A-Za-z0-9_]+)$", re.IGNORECASE),
            "date": re.compile(r"^Dte_es_:signer(\d+):([A-Za-z0-9_]+)$", re.IGNORECASE),
            "email": re.compile(r"^Eml_es_:signer(\d+):([A-Za-z0-9_]+)$", re.IGNORECASE),
            "text": re.compile(r"^Txt_es_:signer(\d+):([A-Za-z0-9_]+)$", re.IGNORECASE),
        }

    @staticmethod
    def _normalize_placeholder_token(token: str) -> str:
        cleaned = (token or "").strip()
        if cleaned.startswith("{{") and cleaned.endswith("}}"):
            cleaned = cleaned[2:-2]
        return cleaned.strip()

    @staticmethod
    def _default_submitter_name(index: int) -> str:
        names = {
            1: "First Party",
            2: "Second Party",
            3: "Third Party",
            4: "Fourth Party",
            5: "Fifth Party",
        }
        return names.get(index, f"Party {index}")

    @staticmethod
    def _normalize_signature_layout_mode(value: object) -> str:
        mode = str(value or "table_preferred").strip().lower()
        if mode not in {"table_preferred", "generic"}:
            return "table_preferred"
        return mode

    @staticmethod
    def _normalize_submitters_order(value: object) -> str:
        mode = str(value or "preserved").strip().lower()
        if mode not in {"preserved", "random"}:
            return "preserved"
        return mode

    @staticmethod
    def _current_date_iso() -> str:
        return date.today().isoformat()

    def _resolve_submitters_order(self, *, form_key: str | None) -> str:
        key = str(form_key or "").strip()
        if key:
            if key in self.submitters_order_by_form:
                return self.submitters_order_by_form[key]
            lowered = key.lower()
            if lowered in self.submitters_order_by_form:
                return self.submitters_order_by_form[lowered]
        return self.submitters_order

    def _resolve_signature_layout_mode(self, *, form_key: str | None) -> str:
        key = str(form_key or "").strip()
        if key:
            if key in self.signature_layout_mode_by_form:
                return self.signature_layout_mode_by_form[key]
            lowered = key.lower()
            if lowered in self.signature_layout_mode_by_form:
                return self.signature_layout_mode_by_form[lowered]
        return self.signature_layout_mode

    @staticmethod
    def _normalize_signature_table_maps(
        raw: dict[str, dict[str, dict[str, float | int]]],
    ) -> dict[str, dict[str, dict[str, float | int]]]:
        out: dict[str, dict[str, dict[str, float | int]]] = {}
        for raw_form_key, raw_cells in raw.items():
            form_key = str(raw_form_key or "").strip()
            if not form_key or not isinstance(raw_cells, dict):
                continue
            normalized_cells: dict[str, dict[str, float | int]] = {}
            for raw_cell_key, raw_cell in raw_cells.items():
                cell_key = str(raw_cell_key or "").strip().lower()
                if not re.match(r"^signer\d+_(signature|date)$", cell_key):
                    continue
                if not isinstance(raw_cell, dict):
                    continue
                try:
                    page = int(raw_cell.get("page", 0))
                    x = float(raw_cell["x"])
                    y = float(raw_cell["y"])
                    w = float(raw_cell["w"])
                    h = float(raw_cell["h"])
                except Exception:
                    continue
                if page < 0:
                    continue
                if x < 0.0 or y < 0.0 or w <= 0.0 or h <= 0.0:
                    continue
                if (x + w) > 1.0 or (y + h) > 1.0:
                    continue
                normalized_cells[cell_key] = {"page": page, "x": x, "y": y, "w": w, "h": h}
            if normalized_cells:
                out[form_key] = normalized_cells
        return out

    def _resolve_signature_table_trace_enabled(self, *, form_key: str | None) -> bool:
        key = str(form_key or "").strip()
        if key:
            if key in self.signature_table_trace_by_form:
                return bool(self.signature_table_trace_by_form[key])
            lowered = key.lower()
            if lowered in self.signature_table_trace_by_form:
                return bool(self.signature_table_trace_by_form[lowered])
        return bool(self.signature_table_trace_enabled)

    def _resolve_signature_table_map(self, *, form_key: str | None) -> dict[str, dict[str, float | int]] | None:
        key = str(form_key or "").strip()
        if not key:
            return None
        if key in self.signature_table_maps:
            return self.signature_table_maps[key]
        lowered = key.lower()
        if lowered in self.signature_table_maps:
            return self.signature_table_maps[lowered]
        return None

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
        form_key: str | None = None,
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
            form_key=form_key,
            table_pdf_bytes=upload_pdf_bytes,
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
        pending_parts: list[str] | None = None
        pending_box: dict[str, float | int] | None = None
        pending_first_box: dict[str, float | int] | None = None

        for line in proc.stdout.splitlines():
            page_match = page_re.search(line)
            if page_match:
                page_index += 1
                page_w = float(page_match.group(1))
                page_h = float(page_match.group(2))
                pending_parts = None
                pending_box = None
                pending_first_box = None
                continue

            word_match = word_re.search(line)
            if not word_match or page_index < 0:
                continue

            token_raw = unescape(word_match.group(5))
            raw_box = {
                "x_min": float(word_match.group(1)),
                "y_min": float(word_match.group(2)),
                "x_max": float(word_match.group(3)),
                "y_max": float(word_match.group(4)),
                "page": page_index,
                "page_w": page_w,
                "page_h": page_h,
            }
            aggregate_box = raw_box
            token_candidate = token_raw

            if pending_parts is not None and pending_box is not None:
                pending_parts.append(token_raw)
                pending_box["x_min"] = min(float(pending_box["x_min"]), raw_box["x_min"])
                pending_box["y_min"] = min(float(pending_box["y_min"]), raw_box["y_min"])
                pending_box["x_max"] = max(float(pending_box["x_max"]), raw_box["x_max"])
                pending_box["y_max"] = max(float(pending_box["y_max"]), raw_box["y_max"])
                if "}}" not in token_raw:
                    continue
                token_candidate = "".join(pending_parts)
                y_min = float(pending_box["y_min"])
                y_max = float(pending_box["y_max"])
                if pending_first_box is not None:
                    first_y_min = float(pending_first_box["y_min"])
                    first_y_max = float(pending_first_box["y_max"])
                    first_h = max(1.0, first_y_max - first_y_min)
                    merged_h = max(1.0, y_max - y_min)
                    # Wrapped placeholders can split onto a second line in narrow cells
                    # (for example date_true_intent). Keep first-line vertical bounds so
                    # the field anchor does not become artificially tall/low.
                    if merged_h > (first_h * 1.6):
                        y_min = first_y_min
                        y_max = first_y_max
                aggregate_box = {
                    "x_min": float(pending_box["x_min"]),
                    "y_min": y_min,
                    "x_max": float(pending_box["x_max"]),
                    "y_max": y_max,
                    "page": int(pending_box["page"]),
                    "page_w": float(pending_box["page_w"]),
                    "page_h": float(pending_box["page_h"]),
                }
                pending_parts = None
                pending_box = None
                pending_first_box = None
            elif "{{" in token_raw and "}}" not in token_raw:
                pending_parts = [token_raw]
                pending_box = {
                    "x_min": raw_box["x_min"],
                    "y_min": raw_box["y_min"],
                    "x_max": raw_box["x_max"],
                    "y_max": raw_box["y_max"],
                    "page": raw_box["page"],
                    "page_w": raw_box["page_w"],
                    "page_h": raw_box["page_h"],
                }
                pending_first_box = {
                    "x_min": raw_box["x_min"],
                    "y_min": raw_box["y_min"],
                    "x_max": raw_box["x_max"],
                    "y_max": raw_box["y_max"],
                    "page": raw_box["page"],
                    "page_w": raw_box["page_w"],
                    "page_h": raw_box["page_h"],
                }
                continue

            token = self._normalize_placeholder_token(token_candidate)
            if not token:
                continue
            signer_index: int | None = None
            field_key: str | None = None
            for candidate_type, pattern in patterns.items():
                match = pattern.match(token)
                if match:
                    signer_index = int(match.group(1))
                    field_name = str(match.group(2) or "").strip().lower()
                    if candidate_type == "text":
                        if not field_name:
                            break
                        field_key = f"text:{field_name}"
                    else:
                        if field_name and field_name != candidate_type:
                            field_key = f"{candidate_type}:{field_name}"
                        else:
                            field_key = candidate_type
                    break
            if signer_index is None or field_key is None:
                continue

            areas.setdefault((signer_index, field_key), []).append(
                {
                    "x_min": float(aggregate_box["x_min"]),
                    "y_min": float(aggregate_box["y_min"]),
                    "x_max": float(aggregate_box["x_max"]),
                    "y_max": float(aggregate_box["y_max"]),
                    "page": int(aggregate_box["page"]),
                    "page_w": float(aggregate_box["page_w"]),
                    "page_h": float(aggregate_box["page_h"]),
                }
            )

        return areas

    @staticmethod
    def _normalize_word_token(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    @staticmethod
    def _word_center_y(word: dict) -> float:
        return (float(word["y_min"]) + float(word["y_max"])) / 2.0

    def _extract_pdf_words(self, *, pdf_bytes: bytes) -> list[dict]:
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
            return []

        page_re = re.compile(r'<page[^>]*width="([0-9.]+)"[^>]*height="([0-9.]+)"', re.IGNORECASE)
        word_re = re.compile(
            r'<word[^>]*xMin="([0-9.]+)"[^>]*yMin="([0-9.]+)"[^>]*xMax="([0-9.]+)"[^>]*yMax="([0-9.]+)"[^>]*>(.*?)</word>',
            re.IGNORECASE,
        )

        page_index = -1
        page_w = 0.0
        page_h = 0.0
        words: list[dict] = []

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
            words.append(
                {
                    "text": unescape(word_match.group(5)),
                    "x_min": float(word_match.group(1)),
                    "y_min": float(word_match.group(2)),
                    "x_max": float(word_match.group(3)),
                    "y_max": float(word_match.group(4)),
                    "page": page_index,
                    "page_w": page_w,
                    "page_h": page_h,
                }
            )

        return words

    def _find_checkbox_left_of_label(self, *, words: list[dict], label: dict) -> dict | None:
        label_page = int(label["page"])
        label_mid_y = self._word_center_y(label)
        candidates = [
            word
            for word in words
            if int(word["page"]) == label_page
            and str(word.get("text") or "").strip() in _CHECKBOX_GLYPHS
            and float(word["x_max"]) <= (float(label["x_min"]) + 1.0)
            and (float(label["x_min"]) - float(word["x_max"])) <= 24.0
            and abs(self._word_center_y(word) - label_mid_y) <= 2.8
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: float(item["x_max"]))

    @staticmethod
    def _normalize_radio_area(
        *,
        raw: dict,
        attachment_uuid: str,
        option_uuid: str,
    ) -> dict:
        page_w = max(1.0, float(raw["page_w"]))
        page_h = max(1.0, float(raw["page_h"]))
        raw_w = max(1.0, float(raw["x_max"]) - float(raw["x_min"]))
        raw_h = max(1.0, float(raw["y_max"]) - float(raw["y_min"]))
        box_w = max(9.0, raw_w + 3.0)
        box_h = max(9.0, raw_h + 3.0)

        center_x = (float(raw["x_min"]) + float(raw["x_max"])) / 2.0
        center_y = (float(raw["y_min"]) + float(raw["y_max"])) / 2.0
        x_min = max(0.0, center_x - (box_w / 2.0))
        y_min = max(0.0, center_y - (box_h / 2.0))
        x_max = min(page_w, x_min + box_w)
        y_max = min(page_h, y_min + box_h)
        if x_max <= x_min:
            x_max = min(page_w, x_min + 1.0)
        if y_max <= y_min:
            y_max = min(page_h, y_min + 1.0)

        return {
            "x": x_min / page_w,
            "y": y_min / page_h,
            "w": (x_max - x_min) / page_w,
            "h": (y_max - y_min) / page_h,
            "attachment_uuid": attachment_uuid,
            "option_uuid": option_uuid,
            "page": int(raw["page"]),
        }

    def _build_3g3a_stage_radio_fields(
        self,
        *,
        pdf_bytes: bytes,
        submitter_uuid: str,
        attachment_uuid: str,
        include_q8_disposition: bool,
        include_company_true_intent: bool,
        include_union_true_intent: bool,
    ) -> list[dict]:
        words = self._extract_pdf_words(pdf_bytes=pdf_bytes)
        if not words:
            return []

        labeled_boxes: list[dict] = []
        for word in words:
            token = self._normalize_word_token(str(word.get("text") or ""))
            if token not in {"accepted", "rejected", "appealed", "requested", "yes", "no"}:
                continue
            box = self._find_checkbox_left_of_label(words=words, label=word)
            if not box:
                continue
            labeled_boxes.append({"token": token, "label": word, "box": box})

        if not labeled_boxes:
            return []

        yes_no_entries = [item for item in labeled_boxes if item["token"] in {"yes", "no"}]
        q10_y_hint: float | None = None
        company_q10_boxes: dict[str, dict] = {}
        union_q10_boxes: dict[str, dict] = {}

        if yes_no_entries:
            line_clusters: list[dict[str, object]] = []
            for entry in sorted(yes_no_entries, key=lambda item: self._word_center_y(item["label"])):
                entry_y = self._word_center_y(entry["label"])
                cluster = None
                for candidate in line_clusters:
                    if abs(entry_y - float(candidate["y"])) <= 3.0:
                        cluster = candidate
                        break
                if cluster is None:
                    cluster = {"y": entry_y, "items": []}
                    line_clusters.append(cluster)
                items = cluster["items"]
                items.append(entry)
                cluster["y"] = sum(self._word_center_y(i["label"]) for i in items) / float(len(items))

            best_cluster = max(line_clusters, key=lambda item: len(item["items"])) if line_clusters else None
            if best_cluster and len(best_cluster["items"]) >= 4:
                q10_items = sorted(best_cluster["items"], key=lambda item: float(item["label"]["x_min"]))
                q10_y_hint = sum(self._word_center_y(item["label"]) for item in q10_items) / float(len(q10_items))
                x_positions = [float(item["label"]["x_min"]) for item in q10_items]
                split_idx = max(range(len(x_positions) - 1), key=lambda idx: x_positions[idx + 1] - x_positions[idx])
                left_group = q10_items[: split_idx + 1]
                right_group = q10_items[split_idx + 1 :]

                # On the 3G3A form, company true-intent is printed on the right pair
                # and union true-intent is printed on the left pair.
                company_q10_boxes = {str(item["token"]): item["box"] for item in right_group if item["token"] in {"yes", "no"}}
                union_q10_boxes = {str(item["token"]): item["box"] for item in left_group if item["token"] in {"yes", "no"}}

                if not {"yes", "no"}.issubset(company_q10_boxes.keys()):
                    company_q10_boxes = {}
                if not {"yes", "no"}.issubset(union_q10_boxes.keys()):
                    union_q10_boxes = {}

        q8_entries = [
            item
            for item in labeled_boxes
            if item["token"] in {"accepted", "rejected", "appealed", "requested"}
            and float(item["label"]["x_min"]) < 220.0
        ]
        if q10_y_hint is not None:
            ranged = [
                item
                for item in q8_entries
                if (q10_y_hint - 140.0) <= self._word_center_y(item["label"]) <= (q10_y_hint - 20.0)
            ]
            if ranged:
                q8_entries = ranged

        q8_target_y = (q10_y_hint - 70.0) if q10_y_hint is not None else None
        q8_boxes: dict[str, dict] = {}
        for token in ("accepted", "rejected", "appealed", "requested"):
            candidates = [item for item in q8_entries if item["token"] == token]
            if not candidates:
                continue
            if q8_target_y is None:
                chosen = min(candidates, key=lambda item: self._word_center_y(item["label"]))
            else:
                chosen = min(candidates, key=lambda item: abs(self._word_center_y(item["label"]) - q8_target_y))
            q8_boxes[token] = chosen["box"]

        def _build_radio_field(
            *,
            name: str,
            choices: list[tuple[str, dict]],
            default_value: str | None = None,
        ) -> dict:
            options: list[dict] = []
            areas: list[dict] = []
            for option_value, raw_box in choices:
                option_uuid = str(uuid4())
                options.append({"uuid": option_uuid, "value": option_value})
                areas.append(
                    self._normalize_radio_area(
                        raw=raw_box,
                        attachment_uuid=attachment_uuid,
                        option_uuid=option_uuid,
                    )
                )
            payload = {
                "uuid": str(uuid4()),
                "submitter_uuid": submitter_uuid,
                "name": name,
                "type": "radio",
                "required": False,
                "preferences": {},
                "options": options,
                "areas": areas,
            }
            option_values = {str(o.get("value") or "") for o in options}
            if default_value and default_value in option_values:
                payload["default_value"] = default_value
            return payload

        radio_fields: list[dict] = []
        if include_q8_disposition and {"accepted", "rejected", "appealed", "requested"}.issubset(q8_boxes.keys()):
            radio_fields.append(
                _build_radio_field(
                    name="q8_union_disposition_choice",
                    choices=[
                        ("Accepted", q8_boxes["accepted"]),
                        ("Rejected", q8_boxes["rejected"]),
                        ("Appealed", q8_boxes["appealed"]),
                        ("Requested Mediation", q8_boxes["requested"]),
                    ],
                )
            )
        if include_company_true_intent and {"yes", "no"}.issubset(company_q10_boxes.keys()):
            radio_fields.append(
                _build_radio_field(
                    name="q10_company_true_intent_choice",
                    choices=[
                        ("Yes", company_q10_boxes["yes"]),
                        ("No", company_q10_boxes["no"]),
                    ],
                    default_value="No",
                )
            )
        if include_union_true_intent and {"yes", "no"}.issubset(union_q10_boxes.keys()):
            radio_fields.append(
                _build_radio_field(
                    name="q10_union_true_intent_choice",
                    choices=[
                        ("Yes", union_q10_boxes["yes"]),
                        ("No", union_q10_boxes["no"]),
                    ],
                    default_value="No",
                )
            )

        return radio_fields

    @staticmethod
    def _normalize_area(
        *,
        raw: dict,
        field_type: str,
        attachment_uuid: str,
        field_name: str = "",
        form_key: str | None = None,
    ) -> dict:
        word_w = max(1.0, raw["x_max"] - raw["x_min"])
        word_h = max(1.0, raw["y_max"] - raw["y_min"])
        max_h: float | None = None

        if field_type == "signature":
            min_w, min_h, pad_w, pad_h = 140.0, 28.0, 8.0, 4.0
            y_lift = 14.0
            normalized_field_name = field_name.lower()
            form_hints = _FORM_SIGNATURE_FIELD_DIMENSION_HINTS.get(str(form_key or "").strip().lower())
            hint = form_hints.get(normalized_field_name) if form_hints else None
            if hint:
                min_w = float(hint.get("min_w", min_w))
                min_h = float(hint.get("min_h", min_h))
                y_lift = float(hint.get("y_lift", y_lift))
                if "max_h" in hint:
                    max_h = float(hint.get("max_h", 0.0))
        elif field_type == "text":
            min_w, min_h, pad_w, pad_h = 220.0, 24.0, 4.0, 2.0
            y_lift = 2.0
            hint = _TEXT_FIELD_DIMENSION_HINTS.get(field_name.lower())
            if hint:
                min_w = float(hint.get("min_w", min_w))
                min_h = float(hint.get("min_h", min_h))
                y_lift = float(hint.get("y_lift", y_lift))
                if "max_h" in hint:
                    max_h = float(hint.get("max_h", 0.0))
        elif field_type == "date":
            min_w, min_h, pad_w, pad_h = 120.0, 26.0, 4.0, 2.0
            y_lift = 6.0
            normalized_field_name = field_name.lower()
            hint = _DATE_FIELD_DIMENSION_HINTS.get(normalized_field_name)
            form_hints = _FORM_DATE_FIELD_DIMENSION_HINTS.get(str(form_key or "").strip().lower())
            if form_hints:
                hint = form_hints.get(normalized_field_name, hint)
            if hint:
                min_w = float(hint.get("min_w", min_w))
                min_h = float(hint.get("min_h", min_h))
                y_lift = float(hint.get("y_lift", y_lift))
                if "max_h" in hint:
                    max_h = float(hint.get("max_h", 0.0))
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
        if max_h is not None and max_h > 0:
            h = min(h, max_h)

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

    @staticmethod
    def _normalize_exact_area(
        *,
        raw: dict,
        attachment_uuid: str,
    ) -> dict:
        # Table trace/map overrides are already computed cell geometry.
        # Keep them exact (no generic signature/date padding or y-lift).
        page_w = max(1.0, float(raw["page_w"]))
        page_h = max(1.0, float(raw["page_h"]))
        x_min = max(0.0, float(raw["x_min"]))
        y_min = max(0.0, float(raw["y_min"]))
        x_max = min(page_w, float(raw["x_max"]))
        y_max = min(page_h, float(raw["y_max"]))
        if x_max <= x_min:
            x_max = min(page_w, x_min + 1.0)
        if y_max <= y_min:
            y_max = min(page_h, y_min + 1.0)
        return {
            "x": x_min / page_w,
            "y": y_min / page_h,
            "w": (x_max - x_min) / page_w,
            "h": (y_max - y_min) / page_h,
            "attachment_uuid": attachment_uuid,
            "page": int(raw["page"]),
        }

    @staticmethod
    def _is_3g3a_form(form_key: str | None) -> bool:
        key = str(form_key or "").strip().lower()
        return key == "bst_grievance_form_3g3a"

    @staticmethod
    def _inject_3g3a_missing_date_anchors(
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
        form_key: str | None,
    ) -> dict[tuple[int, str], list[dict]]:
        if not DocuSealClient._is_3g3a_form(form_key):
            return placeholder_areas

        out: dict[tuple[int, str], list[dict]] = {
            key: [dict(anchor) for anchor in anchors]
            for key, anchors in placeholder_areas.items()
        }
        signer_indexes = sorted({idx for idx, _ in out.keys()})
        for signer_idx in signer_indexes:
            sig_anchors = out.get((signer_idx, "signature")) or []
            if not sig_anchors:
                continue
            # Stage-2 row has signature + company rep text but legacy template omits
            # the matching date placeholder in that row.
            if not out.get((signer_idx, "text:q7_company_rep_name_attuid")):
                continue
            if out.get((signer_idx, "date")):
                continue

            sig_anchor = sig_anchors[0]
            ref_date = (
                (out.get((signer_idx, "date:date_true_intent")) or [None])[0]
                or (out.get((signer_idx, "date:q5_l2_date")) or [None])[0]
            )

            page_w = float(sig_anchor["page_w"])
            if ref_date:
                width = max(40.0, float(ref_date["x_max"]) - float(ref_date["x_min"]))
                x_min = float(ref_date["x_min"])
            else:
                width = max(72.0, page_w * 0.13)
                x_min = float(sig_anchor["x_max"]) + (page_w * 0.16)

            min_gap = max(12.0, page_w * 0.02)
            min_x = float(sig_anchor["x_max"]) + min_gap
            if x_min < min_x:
                x_min = min_x
            x_min = max(0.0, min(x_min, page_w - 1.0))
            x_max = min(page_w, x_min + width)
            if x_max <= x_min:
                continue

            out[(signer_idx, "date")] = [
                {
                    "x_min": x_min,
                    "y_min": float(sig_anchor["y_min"]),
                    "x_max": x_max,
                    "y_max": float(sig_anchor["y_max"]),
                    "page": int(sig_anchor["page"]),
                    "page_w": float(sig_anchor["page_w"]),
                    "page_h": float(sig_anchor["page_h"]),
                }
            ]

        return out

    @staticmethod
    def _inject_true_intent_date_anchors(
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
    ) -> dict[tuple[int, str], list[dict]]:
        out: dict[tuple[int, str], list[dict]] = {
            key: [dict(anchor) for anchor in anchors]
            for key, anchors in placeholder_areas.items()
        }
        signer_indexes = sorted({idx for idx, _ in out.keys()})
        for signer_idx in signer_indexes:
            if out.get((signer_idx, "date")):
                continue
            sig_anchor = ((out.get((signer_idx, "signature:signature_true_intent")) or [None])[0])
            if not sig_anchor:
                continue

            ref_date = ((out.get((signer_idx, "date:date_true_intent")) or [None])[0])
            page_w = float(sig_anchor["page_w"])
            if ref_date:
                width = max(40.0, float(ref_date["x_max"]) - float(ref_date["x_min"]))
                x_min = float(ref_date["x_min"])
                y_min = float(ref_date["y_min"])
                y_max = float(ref_date["y_max"])
            else:
                width = max(72.0, page_w * 0.13)
                x_min = float(sig_anchor["x_max"]) + max(12.0, page_w * 0.02)
                y_min = float(sig_anchor["y_min"])
                y_max = float(sig_anchor["y_max"])

            min_x = float(sig_anchor["x_max"]) + max(12.0, page_w * 0.02)
            if x_min < min_x:
                x_min = min_x
            x_min = max(0.0, min(x_min, page_w - 1.0))
            x_max = min(page_w, x_min + width)
            if x_max <= x_min:
                continue

            out[(signer_idx, "date")] = [
                {
                    "x_min": x_min,
                    "y_min": y_min,
                    "x_max": x_max,
                    "y_max": y_max,
                    "page": int(sig_anchor["page"]),
                    "page_w": float(sig_anchor["page_w"]),
                    "page_h": float(sig_anchor["page_h"]),
                }
            ]
        return out

    @staticmethod
    def _is_settlement_form(form_key: str | None) -> bool:
        key = str(form_key or "").strip().lower()
        return key in {"settlement_form_3106", "settlement_form"}

    @staticmethod
    def _build_signer_rows(
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
        form_key: str | None,
    ) -> list[tuple[int, dict, dict]]:
        rows: list[tuple[int, dict, dict]] = []
        signer_indexes = sorted({idx for idx, _ in placeholder_areas.keys()})
        for signer_idx in signer_indexes:
            sig_anchors = placeholder_areas.get((signer_idx, "signature")) or []
            date_anchors = placeholder_areas.get((signer_idx, "date")) or []
            if not sig_anchors or not date_anchors:
                continue
            sig_anchor = sig_anchors[0]
            date_anchor = date_anchors[0]
            if int(sig_anchor["page"]) != int(date_anchor["page"]):
                continue
            rows.append((signer_idx, sig_anchor, date_anchor))

        if not rows:
            return []

        if DocuSealClient._is_settlement_form(form_key):
            rows.sort(
                key=lambda item: (
                    int(item[1]["page"]),
                    ((item[1]["y_min"] + item[1]["y_max"]) / 2.0 + (item[2]["y_min"] + item[2]["y_max"]) / 2.0)
                    / 2.0,
                )
            )
            remapped: list[tuple[int, dict, dict]] = []
            for row_index, (_, sig_anchor, date_anchor) in enumerate(rows, start=1):
                remapped.append((row_index, sig_anchor, date_anchor))
            return remapped

        rows.sort(key=lambda item: item[0])
        return rows

    @staticmethod
    def _expected_signature_override_keys(
        rows: list[tuple[int, dict, dict]],
    ) -> set[tuple[int, str]]:
        expected: set[tuple[int, str]] = set()
        for signer_idx, _, _ in rows:
            expected.add((signer_idx, "signature"))
            expected.add((signer_idx, "date"))
        return expected

    @staticmethod
    def _resolve_page_dimensions(
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
    ) -> dict[int, tuple[float, float]]:
        out: dict[int, tuple[float, float]] = {}
        for anchors in placeholder_areas.values():
            for anchor in anchors:
                page = int(anchor["page"])
                if page in out:
                    continue
                out[page] = (float(anchor["page_w"]), float(anchor["page_h"]))
        return out

    @staticmethod
    def _top_bottom_from_shape(shape: dict, page_height: float) -> tuple[float, float] | None:
        top_raw = shape.get("top")
        bottom_raw = shape.get("bottom")
        if top_raw is not None and bottom_raw is not None:
            top = float(top_raw)
            bottom = float(bottom_raw)
            return (min(top, bottom), max(top, bottom))
        y0_raw = shape.get("y0")
        y1_raw = shape.get("y1")
        if y0_raw is None or y1_raw is None:
            return None
        y0 = float(y0_raw)
        y1 = float(y1_raw)
        top = page_height - max(y0, y1)
        bottom = page_height - min(y0, y1)
        return (min(top, bottom), max(top, bottom))

    @staticmethod
    def _cluster_vertical_segments(
        segments: list[dict[str, float]],
        tolerance: float = 2.0,
    ) -> list[dict[str, float]]:
        if not segments:
            return []
        sorted_segments = sorted(segments, key=lambda item: item["x"])
        groups: list[list[dict[str, float]]] = [[sorted_segments[0]]]
        for seg in sorted_segments[1:]:
            group = groups[-1]
            center = sum(item["x"] for item in group) / len(group)
            if abs(seg["x"] - center) <= tolerance:
                group.append(seg)
            else:
                groups.append([seg])

        clustered: list[dict[str, float]] = []
        for group in groups:
            clustered.append(
                {
                    "x": sum(item["x"] for item in group) / len(group),
                    "y_min": min(item["y_min"] for item in group),
                    "y_max": max(item["y_max"] for item in group),
                }
            )
        return clustered

    @staticmethod
    def _cluster_horizontal_segments(
        segments: list[dict[str, float]],
        tolerance: float = 2.0,
    ) -> list[dict[str, float]]:
        if not segments:
            return []
        sorted_segments = sorted(segments, key=lambda item: item["y"])
        groups: list[list[dict[str, float]]] = [[sorted_segments[0]]]
        for seg in sorted_segments[1:]:
            group = groups[-1]
            center = sum(item["y"] for item in group) / len(group)
            if abs(seg["y"] - center) <= tolerance:
                group.append(seg)
            else:
                groups.append([seg])

        clustered: list[dict[str, float]] = []
        for group in groups:
            clustered.append(
                {
                    "y": sum(item["y"] for item in group) / len(group),
                    "x_min": min(item["x_min"] for item in group),
                    "x_max": max(item["x_max"] for item in group),
                }
            )
        return clustered

    def _extract_pdf_table_segments(self, *, pdf_bytes: bytes) -> dict[int, dict[str, object]]:
        if pdfplumber is None:
            return {}

        out: dict[int, dict[str, object]] = {}
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:  # type: ignore[union-attr]
                for page_idx, page in enumerate(pdf.pages):
                    page_w = float(page.width)
                    page_h = float(page.height)
                    vertical_segments: list[dict[str, float]] = []
                    horizontal_segments: list[dict[str, float]] = []

                    for line in page.lines:
                        x0_raw = line.get("x0")
                        x1_raw = line.get("x1")
                        if x0_raw is None or x1_raw is None:
                            continue
                        x0 = float(x0_raw)
                        x1 = float(x1_raw)
                        top_bottom = self._top_bottom_from_shape(line, page_h)
                        if not top_bottom:
                            continue
                        top, bottom = top_bottom
                        if abs(x0 - x1) <= 1.5 and (bottom - top) >= 8.0:
                            vertical_segments.append({"x": (x0 + x1) / 2.0, "y_min": top, "y_max": bottom})
                        elif abs(bottom - top) <= 1.5 and abs(x1 - x0) >= 8.0:
                            horizontal_segments.append(
                                {"y": (top + bottom) / 2.0, "x_min": min(x0, x1), "x_max": max(x0, x1)}
                            )

                    for rect in page.rects:
                        x0_raw = rect.get("x0")
                        x1_raw = rect.get("x1")
                        top_bottom = self._top_bottom_from_shape(rect, page_h)
                        if x0_raw is None or x1_raw is None or not top_bottom:
                            continue
                        x0 = float(x0_raw)
                        x1 = float(x1_raw)
                        top, bottom = top_bottom
                        width = abs(x1 - x0)
                        height = abs(bottom - top)
                        if width < 8.0 and height < 8.0:
                            continue

                        if width >= 8.0 and height >= 8.0:
                            vertical_segments.append({"x": x0, "y_min": top, "y_max": bottom})
                            vertical_segments.append({"x": x1, "y_min": top, "y_max": bottom})
                            horizontal_segments.append({"y": top, "x_min": min(x0, x1), "x_max": max(x0, x1)})
                            horizontal_segments.append({"y": bottom, "x_min": min(x0, x1), "x_max": max(x0, x1)})
                            continue

                        # Many Word-generated table borders are exported as thin rects.
                        # Capture those line-like shapes so auto-grown row boundaries still trace.
                        if width >= 8.0 and height < 8.0:
                            y = (top + bottom) / 2.0
                            horizontal_segments.append({"y": y, "x_min": min(x0, x1), "x_max": max(x0, x1)})
                            continue

                        if height >= 8.0 and width < 8.0:
                            x = (x0 + x1) / 2.0
                            vertical_segments.append({"x": x, "y_min": top, "y_max": bottom})

                    out[page_idx] = {
                        "page_w": page_w,
                        "page_h": page_h,
                        "vertical": self._cluster_vertical_segments(vertical_segments),
                        "horizontal": self._cluster_horizontal_segments(horizontal_segments),
                    }
        except Exception:
            self.logger.exception("docuseal_table_trace_extract_failed")
            return {}
        return out

    @staticmethod
    def _find_cell_for_anchor(
        *,
        anchor: dict,
        vertical_segments: list[dict[str, float]],
        horizontal_segments: list[dict[str, float]],
        page_w: float,
        page_h: float,
    ) -> dict | None:
        center_x = (float(anchor["x_min"]) + float(anchor["x_max"])) / 2.0
        center_y = (float(anchor["y_min"]) + float(anchor["y_max"])) / 2.0

        left_candidates = [
            seg
            for seg in vertical_segments
            if seg["x"] < center_x and (seg["y_min"] - 2.0) <= center_y <= (seg["y_max"] + 2.0)
        ]
        right_candidates = [
            seg
            for seg in vertical_segments
            if seg["x"] > center_x and (seg["y_min"] - 2.0) <= center_y <= (seg["y_max"] + 2.0)
        ]
        top_candidates = [
            seg
            for seg in horizontal_segments
            if seg["y"] < center_y and (seg["x_min"] - 2.0) <= center_x <= (seg["x_max"] + 2.0)
        ]
        bottom_candidates = [
            seg
            for seg in horizontal_segments
            if seg["y"] > center_y and (seg["x_min"] - 2.0) <= center_x <= (seg["x_max"] + 2.0)
        ]
        if not left_candidates or not right_candidates or not top_candidates or not bottom_candidates:
            return None

        left = max(left_candidates, key=lambda item: item["x"])["x"]
        right = min(right_candidates, key=lambda item: item["x"])["x"]
        top = max(top_candidates, key=lambda item: item["y"])["y"]
        bottom = min(bottom_candidates, key=lambda item: item["y"])["y"]

        if (right - left) < 35.0 or (bottom - top) < 14.0:
            return None

        inset_x = min(6.0, (right - left) * 0.08)
        inset_y = min(4.0, (bottom - top) * 0.18)
        x_min = max(0.0, left + inset_x)
        x_max = min(page_w, right - inset_x)
        y_min = max(0.0, top + inset_y)
        y_max = min(page_h, bottom - inset_y)
        if x_max <= x_min or y_max <= y_min:
            return None

        return {
            "x_min": x_min,
            "y_min": y_min,
            "x_max": x_max,
            "y_max": y_max,
            "page": int(anchor["page"]),
            "page_w": page_w,
            "page_h": page_h,
        }

    @staticmethod
    def _find_vertical_bounds_for_x(
        *,
        x_center: float,
        vertical_segments: list[dict[str, float]],
    ) -> tuple[float, float] | None:
        left_candidates = [seg["x"] for seg in vertical_segments if seg["x"] < x_center]
        right_candidates = [seg["x"] for seg in vertical_segments if seg["x"] > x_center]
        if not left_candidates or not right_candidates:
            return None
        left = max(left_candidates)
        right = min(right_candidates)
        if (right - left) < 35.0:
            return None
        return left, right

    @staticmethod
    def _build_cell_from_bounds(
        *,
        left: float,
        right: float,
        top: float,
        bottom: float,
        page: int,
        page_w: float,
        page_h: float,
    ) -> dict | None:
        if (bottom - top) < 14.0:
            return None
        inset_x = min(6.0, (right - left) * 0.08)
        inset_y = min(4.0, (bottom - top) * 0.18)
        x_min = max(0.0, left + inset_x)
        x_max = min(page_w, right - inset_x)
        y_min = max(0.0, top + inset_y)
        y_max = min(page_h, bottom - inset_y)
        if x_max <= x_min or y_max <= y_min:
            return None
        return {
            "x_min": x_min,
            "y_min": y_min,
            "x_max": x_max,
            "y_max": y_max,
            "page": int(page),
            "page_w": page_w,
            "page_h": page_h,
        }

    @staticmethod
    def _horizontal_row_bands_for_x(
        *,
        x_center: float,
        horizontal_segments: list[dict[str, float]],
    ) -> list[tuple[float, float]]:
        ys = sorted(
            {
                float(seg["y"])
                for seg in horizontal_segments
                if (float(seg["x_min"]) - 2.0) <= x_center <= (float(seg["x_max"]) + 2.0)
            }
        )
        if len(ys) < 2:
            return []
        bands: list[tuple[float, float]] = []
        for idx in range(len(ys) - 1):
            top = ys[idx]
            bottom = ys[idx + 1]
            if (bottom - top) >= 14.0:
                bands.append((top, bottom))
        return bands

    def _build_settlement_trace_overrides(
        self,
        *,
        rows: list[tuple[int, dict, dict]],
        page_segments: dict[int, dict[str, object]],
    ) -> tuple[dict[tuple[int, str], list[dict]], str]:
        ordered_rows = sorted(rows, key=lambda item: item[0])
        if len(ordered_rows) < 3:
            return {}, "trace_settlement_missing_rows"

        signer1_sig_anchor = ordered_rows[0][1]
        page = int(signer1_sig_anchor["page"])
        page_info = page_segments.get(page)
        if not isinstance(page_info, dict):
            return {}, f"trace_missing_page_{page}"
        vertical = page_info.get("vertical")
        horizontal = page_info.get("horizontal")
        if not isinstance(vertical, list) or not isinstance(horizontal, list):
            return {}, f"trace_invalid_segments_page_{page}"
        page_w = float(page_info.get("page_w") or signer1_sig_anchor["page_w"])
        page_h = float(page_info.get("page_h") or signer1_sig_anchor["page_h"])

        sig_center_x = (float(signer1_sig_anchor["x_min"]) + float(signer1_sig_anchor["x_max"])) / 2.0
        sig_bands = self._horizontal_row_bands_for_x(x_center=sig_center_x, horizontal_segments=horizontal)
        if not sig_bands:
            return {}, "trace_settlement_no_sig_bands"

        signer1_center_y = (float(signer1_sig_anchor["y_min"]) + float(signer1_sig_anchor["y_max"])) / 2.0
        row_start_index: int | None = None
        for idx, (top, bottom) in enumerate(sig_bands):
            if top <= signer1_center_y <= bottom:
                row_start_index = idx
                break
        if row_start_index is None:
            row_start_index = min(
                range(len(sig_bands)),
                key=lambda idx: abs(((sig_bands[idx][0] + sig_bands[idx][1]) / 2.0) - signer1_center_y),
            )

        overrides: dict[tuple[int, str], list[dict]] = {}
        for row_offset, (signer_idx, sig_anchor, date_anchor) in enumerate(ordered_rows):
            band_index = row_start_index + row_offset
            if band_index >= len(sig_bands):
                return {}, f"trace_settlement_band_missing_signer{signer_idx}"
            top, bottom = sig_bands[band_index]

            sig_center_x = (float(sig_anchor["x_min"]) + float(sig_anchor["x_max"])) / 2.0
            date_center_x = (float(date_anchor["x_min"]) + float(date_anchor["x_max"])) / 2.0
            sig_bounds = self._find_vertical_bounds_for_x(x_center=sig_center_x, vertical_segments=vertical)
            date_bounds = self._find_vertical_bounds_for_x(x_center=date_center_x, vertical_segments=vertical)
            if not sig_bounds or not date_bounds:
                return {}, f"trace_settlement_column_missing_signer{signer_idx}"

            sig_cell = self._build_cell_from_bounds(
                left=sig_bounds[0],
                right=sig_bounds[1],
                top=top,
                bottom=bottom,
                page=page,
                page_w=page_w,
                page_h=page_h,
            )
            date_cell = self._build_cell_from_bounds(
                left=date_bounds[0],
                right=date_bounds[1],
                top=top,
                bottom=bottom,
                page=page,
                page_w=page_w,
                page_h=page_h,
            )
            if not sig_cell or not date_cell:
                return {}, f"trace_settlement_cell_invalid_signer{signer_idx}"

            overrides[(signer_idx, "signature")] = [sig_cell]
            overrides[(signer_idx, "date")] = [date_cell]

        return overrides, "trace_settlement_rows_success"

    def _build_table_trace_overrides(
        self,
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
        pdf_bytes: bytes,
        form_key: str | None,
        table_pdf_bytes: bytes | None = None,
    ) -> tuple[dict[tuple[int, str], list[dict]], str]:
        rows = self._build_signer_rows(placeholder_areas=placeholder_areas, form_key=form_key)
        if not rows:
            return {}, "trace_no_signature_rows"

        trace_source_pdf_bytes = table_pdf_bytes if table_pdf_bytes else pdf_bytes
        page_segments = self._extract_pdf_table_segments(pdf_bytes=trace_source_pdf_bytes)
        if not page_segments:
            return {}, "trace_no_page_segments"

        if self._is_settlement_form(form_key):
            settlement_overrides, settlement_reason = self._build_settlement_trace_overrides(
                rows=rows,
                page_segments=page_segments,
            )
            if settlement_overrides:
                return settlement_overrides, settlement_reason

        overrides: dict[tuple[int, str], list[dict]] = {}
        for signer_idx, sig_anchor, date_anchor in rows:
            page = int(sig_anchor["page"])
            page_info = page_segments.get(page)
            if not isinstance(page_info, dict):
                return {}, f"trace_missing_page_{page}"
            vertical = page_info.get("vertical")
            horizontal = page_info.get("horizontal")
            if not isinstance(vertical, list) or not isinstance(horizontal, list):
                return {}, f"trace_invalid_segments_page_{page}"
            page_w = float(page_info.get("page_w") or sig_anchor["page_w"])
            page_h = float(page_info.get("page_h") or sig_anchor["page_h"])

            sig_cell = self._find_cell_for_anchor(
                anchor=sig_anchor,
                vertical_segments=vertical,
                horizontal_segments=horizontal,
                page_w=page_w,
                page_h=page_h,
            )
            date_cell = self._find_cell_for_anchor(
                anchor=date_anchor,
                vertical_segments=vertical,
                horizontal_segments=horizontal,
                page_w=page_w,
                page_h=page_h,
            )
            if not sig_cell or not date_cell:
                return {}, f"trace_cell_not_found_signer{signer_idx}"

            overrides[(signer_idx, "signature")] = [sig_cell]
            overrides[(signer_idx, "date")] = [date_cell]

        expected_keys = self._expected_signature_override_keys(rows)
        if not expected_keys.issubset(set(overrides.keys())):
            return {}, "trace_missing_expected_keys"

        return overrides, "trace_success"

    @staticmethod
    def _normalize_raw_box(raw: dict) -> dict[str, float | int]:
        page_w = max(1.0, float(raw["page_w"]))
        page_h = max(1.0, float(raw["page_h"]))
        x_min = float(raw["x_min"]) / page_w
        y_min = float(raw["y_min"]) / page_h
        x_max = float(raw["x_max"]) / page_w
        y_max = float(raw["y_max"]) / page_h
        return {
            "page": int(raw["page"]),
            "x": x_min,
            "y": y_min,
            "w": max(0.0, x_max - x_min),
            "h": max(0.0, y_max - y_min),
            "x2": x_max,
            "y2": y_max,
        }

    def _guard_trace_overrides(
        self,
        *,
        trace_overrides: dict[tuple[int, str], list[dict]],
        placeholder_areas: dict[tuple[int, str], list[dict]],
        form_key: str | None,
    ) -> tuple[bool, str, dict[str, object]]:
        metrics: dict[str, object] = {
            "guard_enabled": bool(self.signature_table_guard_enabled),
            "guard_tolerance": float(self.signature_table_guard_tolerance),
            "guard_min_gap": float(self.signature_table_guard_min_gap),
            "trace_boxes": {},
            "row_checks": {},
            "map_deltas": {},
        }
        if not self.signature_table_guard_enabled:
            return True, "guard_disabled", metrics

        rows = self._build_signer_rows(placeholder_areas=placeholder_areas, form_key=form_key)
        if not rows:
            return False, "guard_fail_no_rows", metrics

        expected_keys = self._expected_signature_override_keys(rows)
        missing_keys = sorted(expected_keys - set(trace_overrides.keys()))
        if missing_keys:
            metrics["missing_keys"] = [f"signer{idx}_{field}" for idx, field in missing_keys]
            return False, "guard_fail_missing_keys", metrics

        row_centers: list[tuple[int, float]] = []
        for signer_idx, _, _ in sorted(rows, key=lambda item: item[0]):
            sig_raw = (trace_overrides.get((signer_idx, "signature")) or [{}])[0]
            date_raw = (trace_overrides.get((signer_idx, "date")) or [{}])[0]
            if not sig_raw or not date_raw:
                return False, "guard_fail_missing_row_fields", metrics

            sig_box = self._normalize_raw_box(sig_raw)
            date_box = self._normalize_raw_box(date_raw)
            metrics["trace_boxes"][f"signer{signer_idx}_signature"] = sig_box
            metrics["trace_boxes"][f"signer{signer_idx}_date"] = date_box

            if int(sig_box["page"]) != int(date_box["page"]):
                return False, "guard_fail_page_mismatch", metrics

            sig_w = float(sig_box["w"])
            sig_h = float(sig_box["h"])
            date_w = float(date_box["w"])
            date_h = float(date_box["h"])
            if sig_w < 0.05 or date_w < 0.05 or sig_h < 0.015 or date_h < 0.015:
                return False, "guard_fail_too_small", metrics
            if sig_w > 0.80 or date_w > 0.45 or sig_h > 0.20 or date_h > 0.20:
                return False, "guard_fail_too_large", metrics

            overlap = max(
                0.0,
                min(float(sig_box["x2"]), float(date_box["x2"])) - max(float(sig_box["x"]), float(date_box["x"])),
            )
            gap = float(date_box["x"]) - float(sig_box["x2"])
            row_center = (
                (float(sig_box["y"]) + float(sig_box["y2"])) / 2.0 + (float(date_box["y"]) + float(date_box["y2"])) / 2.0
            ) / 2.0
            row_centers.append((signer_idx, row_center))
            metrics["row_checks"][f"signer{signer_idx}"] = {
                "overlap": overlap,
                "gap": gap,
                "center_y": row_center,
            }

            if overlap > 0.0:
                return False, "guard_fail_overlap", metrics
            if gap < self.signature_table_guard_min_gap:
                return False, "guard_fail_min_gap", metrics

        row_centers.sort(key=lambda item: item[0])
        for idx in range(len(row_centers) - 1):
            if not (row_centers[idx][1] < row_centers[idx + 1][1]):
                return False, "guard_fail_row_order", metrics

        form_map = self._resolve_signature_table_map(form_key=form_key)
        if form_map:
            if self._is_settlement_form(form_key):
                metrics["guard_map_delta_skipped"] = True
                return True, "guard_pass", metrics
            for signer_idx, _, _ in rows:
                for field_type in ("signature", "date"):
                    key = f"signer{signer_idx}_{field_type}"
                    trace_box_raw = (trace_overrides.get((signer_idx, field_type)) or [{}])[0]
                    if not trace_box_raw:
                        return False, "guard_fail_map_trace_missing", metrics
                    map_box = form_map.get(key)
                    if not map_box:
                        return False, "guard_fail_map_cell_missing", metrics

                    trace_box = self._normalize_raw_box(trace_box_raw)
                    deltas = {
                        "dx": abs(float(trace_box["x"]) - float(map_box["x"])),
                        "dy": abs(float(trace_box["y"]) - float(map_box["y"])),
                        "dw": abs(float(trace_box["w"]) - float(map_box["w"])),
                        "dh": abs(float(trace_box["h"]) - float(map_box["h"])),
                    }
                    metrics["map_deltas"][key] = deltas
                    compare_keys = ("dx", "dy", "dw", "dh")
                    max_delta = max(deltas[name] for name in compare_keys)
                    if max_delta > self.signature_table_guard_tolerance:
                        metrics["guard_map_delta_max"] = max_delta
                        metrics["guard_map_delta_components"] = list(compare_keys)
                        metrics["guard_map_delta_key"] = key
                        return False, "guard_fail_map_delta", metrics

        return True, "guard_pass", metrics

    def _build_table_map_overrides(
        self,
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
        form_key: str | None,
    ) -> tuple[dict[tuple[int, str], list[dict]], str]:
        table_map = self._resolve_signature_table_map(form_key=form_key)
        if not table_map:
            return {}, "map_missing"

        page_dimensions = self._resolve_page_dimensions(placeholder_areas=placeholder_areas)
        rows = self._build_signer_rows(placeholder_areas=placeholder_areas, form_key=form_key)
        expected_keys = self._expected_signature_override_keys(rows)
        if not expected_keys:
            return {}, "map_no_signature_rows"

        overrides: dict[tuple[int, str], list[dict]] = {}
        missing_page_dims: set[int] = set()
        for cell_key, cell in table_map.items():
            match = re.match(r"^signer(\d+)_(signature|date)$", cell_key)
            if not match:
                continue
            signer_idx = int(match.group(1))
            field_type = str(match.group(2))
            page = int(cell["page"])
            dims = page_dimensions.get(page)
            if not dims:
                missing_page_dims.add(page)
                continue
            page_w, page_h = dims
            x = float(cell["x"]) * page_w
            y = float(cell["y"]) * page_h
            w = float(cell["w"]) * page_w
            h = float(cell["h"]) * page_h
            overrides[(signer_idx, field_type)] = [
                {
                    "x_min": x,
                    "y_min": y,
                    "x_max": x + w,
                    "y_max": y + h,
                    "page": page,
                    "page_w": page_w,
                    "page_h": page_h,
                }
            ]

        if missing_page_dims:
            return {}, f"map_missing_page_dims:{','.join(str(i) for i in sorted(missing_page_dims))}"

        missing_expected = sorted(expected_keys - set(overrides.keys()))
        if missing_expected:
            rendered = ",".join(f"signer{idx}_{field}" for idx, field in missing_expected)
            return {}, f"map_missing_expected_keys:{rendered}"

        return overrides, "map_success"

    def _resolve_signature_table_overrides(
        self,
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
        pdf_bytes: bytes,
        form_key: str | None,
        table_pdf_bytes: bytes | None = None,
    ) -> tuple[dict[tuple[int, str], list[dict]], str, str, dict[str, object]]:
        layout_mode = self._resolve_signature_layout_mode(form_key=form_key)
        if layout_mode != "table_preferred":
            return {}, "generic_fallback", "layout_mode_generic", {"layout_mode": layout_mode}

        trace_reason = "trace_disabled"
        trace_metrics: dict[str, object] = {}
        if self._resolve_signature_table_trace_enabled(form_key=form_key):
            trace_overrides, trace_reason = self._build_table_trace_overrides(
                placeholder_areas=placeholder_areas,
                pdf_bytes=pdf_bytes,
                form_key=form_key,
                table_pdf_bytes=table_pdf_bytes,
            )
            if trace_overrides:
                guard_ok, guard_reason, guard_metrics = self._guard_trace_overrides(
                    trace_overrides=trace_overrides,
                    placeholder_areas=placeholder_areas,
                    form_key=form_key,
                )
                trace_metrics = {
                    "trace_reason": trace_reason,
                    "guard_reason": guard_reason,
                    "guard_metrics": guard_metrics,
                }
                if guard_ok:
                    return trace_overrides, "trace", f"{trace_reason};{guard_reason}", trace_metrics
                trace_reason = f"{trace_reason};{guard_reason}"
            else:
                trace_metrics = {"trace_reason": trace_reason}

        map_overrides, map_reason = self._build_table_map_overrides(
            placeholder_areas=placeholder_areas,
            form_key=form_key,
        )
        if map_overrides:
            return map_overrides, "map_fallback", map_reason, {
                "trace_rejected": trace_metrics,
                "map_reason": map_reason,
            }

        return {}, "generic_fallback", f"trace_reason={trace_reason};map_reason={map_reason}", {
            "trace_rejected": trace_metrics,
            "map_reason": map_reason,
        }

    def _apply_placeholder_field_alignment(
        self,
        *,
        template_id: str,
        pdf_bytes: bytes,
        form_key: str | None = None,
        table_pdf_bytes: bytes | None = None,
    ) -> None:
        placeholder_areas = self._extract_placeholder_areas(pdf_bytes=pdf_bytes)
        if not placeholder_areas:
            return
        placeholder_areas = self._inject_3g3a_missing_date_anchors(
            placeholder_areas=placeholder_areas,
            form_key=form_key,
        )
        placeholder_areas = self._inject_true_intent_date_anchors(
            placeholder_areas=placeholder_areas,
        )
        signer_indexes = sorted({signer_idx for signer_idx, _ in placeholder_areas.keys()})
        if not signer_indexes:
            return
        table_overrides, placement_strategy, placement_reason, placement_metrics = self._resolve_signature_table_overrides(
            placeholder_areas=placeholder_areas,
            pdf_bytes=pdf_bytes,
            form_key=form_key,
            table_pdf_bytes=table_pdf_bytes,
        )
        self.logger.info(
            "docuseal_signature_placement_strategy",
            extra={
                "template_id": template_id,
                "form_key": str(form_key or ""),
                "strategy": placement_strategy,
                "reason": placement_reason,
                "override_count": len(table_overrides),
                "metrics": placement_metrics,
            },
        )

        template_resp = requests.get(
            f"{self.base_url}/api/templates/{template_id}",
            headers=self._headers(is_json=False),
            timeout=self.timeout,
        )
        if not (200 <= template_resp.status_code < 300):
            return

        template_obj = self._json_object(template_resp)
        submitters = template_obj.get("submitters")
        fields = template_obj.get("fields")
        schema = template_obj.get("schema")
        if not isinstance(submitters, list) or not submitters:
            return
        required_submitter_count = max(signer_indexes)

        # DocuSeal API can return submitter names without UUIDs. Restore/preserve UUIDs
        # from existing fields and synthesize missing ones so submissions remain creatable.
        field_submitter_uuids: list[str] = []
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                su = str(field.get("submitter_uuid") or "").strip()
                if su and su not in field_submitter_uuids:
                    field_submitter_uuids.append(su)

        submitter_payload: list[dict[str, str]] = []
        missing_submitter_uuid = False
        for idx in range(1, required_submitter_count + 1):
            existing = submitters[idx - 1] if idx - 1 < len(submitters) and isinstance(submitters[idx - 1], dict) else {}
            name = str(existing.get("name") or "").strip() or self._default_submitter_name(idx)
            submitter_uuid = str(existing.get("uuid") or "").strip()
            if not submitter_uuid and idx - 1 < len(field_submitter_uuids):
                submitter_uuid = field_submitter_uuids[idx - 1]
            if not submitter_uuid:
                submitter_uuid = str(uuid4())
                missing_submitter_uuid = True
            submitter_payload.append({"name": name, "uuid": submitter_uuid})

        needs_submitter_patch = (
            len(submitters) != required_submitter_count
            or missing_submitter_uuid
            or any(not str((s.get("uuid") if isinstance(s, dict) else "") or "").strip() for s in submitters[:required_submitter_count])
        )
        if needs_submitter_patch:
            submitter_resp = requests.patch(
                f"{self.base_url}/api/templates/{template_id}",
                headers=self._headers(is_json=True),
                json={"submitters": submitter_payload},
                timeout=self.timeout,
            )
            if not (200 <= submitter_resp.status_code < 300):
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
            fields = template_obj.get("fields")
            schema = template_obj.get("schema")
            if not isinstance(submitters, list) or len(submitters) < required_submitter_count:
                return
        if not isinstance(schema, list) or not schema or not isinstance(schema[0], dict):
            return
        attachment_uuid = str(schema[0].get("attachment_uuid") or "").strip()
        if not attachment_uuid:
            return

        submitter_uuid_by_index: dict[int, str] = {}
        for idx in range(1, required_submitter_count + 1):
            uuid_from_template = ""
            if idx - 1 < len(submitters) and isinstance(submitters[idx - 1], dict):
                uuid_from_template = str(submitters[idx - 1].get("uuid") or "").strip()
            if not uuid_from_template and idx - 1 < len(submitter_payload):
                uuid_from_template = submitter_payload[idx - 1]["uuid"]
            if uuid_from_template:
                submitter_uuid_by_index[idx] = uuid_from_template

        rebuilt_fields: list[dict] = []
        for signer_idx in signer_indexes:
            submitter_pos = signer_idx - 1
            if submitter_pos < 0:
                continue
            submitter_uuid = submitter_uuid_by_index.get(signer_idx, "").strip()
            if not submitter_uuid:
                continue

            signer_fields = [
                (field_key, anchors)
                for (cur_signer, field_key), anchors in placeholder_areas.items()
                if cur_signer == signer_idx
            ]
            signer_fields.sort(key=lambda item: item[0])
            for field_key, anchors in signer_fields:
                if not anchors:
                    continue
                field_type = field_key
                field_name = ""
                if ":" in field_key:
                    field_type, field_name = field_key.split(":", 1)
                field_name = field_name.strip().lower()
                if field_type == "date" and field_name == "date_true_intent":
                    continue
                if not field_name:
                    field_name = f"signer{signer_idx}_{field_type}"

                effective_anchors = anchors
                used_table_override = False
                if field_type in {"signature", "date"} and field_name in {f"signer{signer_idx}_{field_type}", field_type}:
                    table_match = table_overrides.get((signer_idx, field_type))
                    if table_match:
                        effective_anchors = table_match
                        used_table_override = True
                        first = table_match[0]
                        self.logger.debug(
                            "docuseal_signature_field_override",
                            extra={
                                "template_id": template_id,
                                "form_key": str(form_key or ""),
                                "strategy": placement_strategy,
                                "signer_index": signer_idx,
                                "field_type": field_type,
                                "page": int(first["page"]),
                                "x_min": float(first["x_min"]),
                                "y_min": float(first["y_min"]),
                                "x_max": float(first["x_max"]),
                                "y_max": float(first["y_max"]),
                            },
                        )

                required = True
                readonly = False
                preferences: dict[str, object] = {}
                default_value: str | None = None
                if field_type == "text":
                    required = "true_intent" not in field_name
                    if field_name in _MULTILINE_TEXT_FIELDS:
                        preferences = dict(_MULTILINE_TEXT_PREFERENCES)
                elif field_type == "email":
                    field_type = "text"
                    field_name = f"signer{signer_idx}_email"
                    required = False
                    readonly = True
                elif field_type in {"signature", "date"}:
                    if field_type == "date":
                        # Enforce system date for signer date fields so they track
                        # signing-time context without manual user entry.
                        readonly = True
                        default_value = self._current_date_iso()
                    if "true_intent" in field_name:
                        required = False

                field_payload = {
                    "uuid": str(uuid4()),
                    "submitter_uuid": submitter_uuid,
                    "name": field_name,
                    "type": field_type,
                    "required": required,
                    "preferences": preferences,
                    "areas": [],
                }
                for anchor in effective_anchors:
                    if used_table_override and field_type in {"signature", "date"}:
                        field_payload["areas"].append(
                            self._normalize_exact_area(
                                raw=anchor,
                                attachment_uuid=attachment_uuid,
                            )
                        )
                    else:
                        field_payload["areas"].append(
                            self._normalize_area(
                                raw=anchor,
                                field_type=field_type,
                                attachment_uuid=attachment_uuid,
                                field_name=field_name,
                                form_key=form_key,
                            )
                        )
                if readonly:
                    field_payload["readonly"] = True
                if default_value:
                    field_payload["default_value"] = default_value
                rebuilt_fields.append(field_payload)

        is_3g3a_stage2_alignment = (
            self._is_3g3a_form(form_key)
            and signer_indexes == [1]
            and bool(placeholder_areas.get((1, "text:q7_company_rep_name_attuid")))
        )
        is_3g3a_stage3_alignment = (
            self._is_3g3a_form(form_key)
            and signer_indexes == [1]
            and bool(placeholder_areas.get((1, "text:q8_union_rep_name_attuid")))
        )
        if is_3g3a_stage2_alignment or is_3g3a_stage3_alignment:
            submitter_uuid = submitter_uuid_by_index.get(1, "").strip()
            if submitter_uuid:
                radio_fields = self._build_3g3a_stage_radio_fields(
                    pdf_bytes=pdf_bytes,
                    submitter_uuid=submitter_uuid,
                    attachment_uuid=attachment_uuid,
                    include_q8_disposition=is_3g3a_stage3_alignment,
                    include_company_true_intent=is_3g3a_stage2_alignment,
                    include_union_true_intent=is_3g3a_stage3_alignment,
                )
                rebuilt_fields.extend(radio_fields)

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

        # Prefer submission-level URLs first (if present), then fall back
        # to signer-specific links.
        for key in ("url", "signing_url", "submitter_url"):
            val = submission.get(key)
            if isinstance(val, str) and val.strip():
                return self._rewrite_public_url(val.strip())
        slug = submission.get("slug")
        if isinstance(slug, str) and slug.strip():
            return self._rewrite_public_url(_from_slug(slug))

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
        return None

    def extract_signing_links_by_email(self, submission: dict) -> dict[str, str]:
        def _from_slug(slug: str) -> str:
            base = (self.public_base_url or self.base_url).rstrip("/")
            return f"{base}/s/{slug.strip('/')}"

        out: dict[str, str] = {}
        for key in ("submitters", "signers"):
            raw = submission.get(key)
            if not isinstance(raw, list):
                continue
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                email = str(entry.get("email") or "").strip().lower()
                if not email:
                    continue

                url = ""
                for link_key in ("url", "signing_url", "link"):
                    val = entry.get(link_key)
                    if isinstance(val, str) and val.strip():
                        url = val.strip()
                        break
                if not url:
                    slug = entry.get("slug")
                    if isinstance(slug, str) and slug.strip():
                        url = _from_slug(slug)
                if not url:
                    continue
                out[email] = self._rewrite_public_url(url) or url
        return out

    def fetch_signing_links_by_email(self, *, submission_id: str) -> dict[str, str]:
        sid = str(submission_id or "").strip()
        if not sid:
            return {}
        try:
            resp = requests.get(
                f"{self.base_url}/api/submissions/{sid}",
                headers=self._headers(is_json=False),
                timeout=self.timeout,
            )
        except Exception:
            return {}
        if not (200 <= resp.status_code < 300):
            return {}
        try:
            payload = resp.json()
        except Exception:
            return {}
        sub = self._extract_submission_object(payload)
        if not sub and isinstance(payload, dict):
            sub = payload
        if not isinstance(sub, dict):
            return {}
        return self.extract_signing_links_by_email(sub)

    @staticmethod
    def _first_object(payload: object) -> dict:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    return item
        return {}

    @staticmethod
    def _extract_submission_object(payload: object) -> dict:
        # DocuSeal payload shape varies by endpoint/version:
        # - direct object: {"id": ...}
        # - wrapped object: {"data": [{...}]}
        # - list: [{...}] or [] on failed create
        if isinstance(payload, dict):
            if any(key in payload for key in ("id", "submission_id", "submissionId")):
                return payload
            for key in ("submission", "data", "result"):
                if key not in payload:
                    continue
                found = DocuSealClient._extract_submission_object(payload.get(key))
                if found:
                    return found
            return {}
        if isinstance(payload, list):
            for item in payload:
                found = DocuSealClient._extract_submission_object(item)
                if found:
                    return found
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
        form_key: str | None = None,
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
                form_key=form_key,
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
        submitters_order = self._resolve_submitters_order(form_key=form_key)
        payload_variants = [
            {
                "template_id": selected_template_id,
                "submitters": signer_objs,
                "submitters_order": submitters_order,
                "name": title,
                "send_email": False,
                "metadata": metadata or {},
            },
            {
                "template_id": selected_template_id,
                "submitters": plain_signer_objs,
                "submitters_order": submitters_order,
                "name": title,
                "send_email": False,
                "metadata": metadata or {},
            },
            {
                "template_id": selected_template_id,
                "signers": signers,
                "submitters_order": submitters_order,
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
            if not (200 <= resp.status_code < 300):
                last_err = f"{resp.status_code} {resp.text[:400]}"
                continue

            try:
                raw_payload = resp.json()
            except Exception:
                last_err = f"{resp.status_code} invalid JSON: {resp.text[:400]}"
                continue

            candidate = self._extract_submission_object(raw_payload)
            candidate_id = str(
                candidate.get("submission_id")
                or candidate.get("submissionId")
                or candidate.get("id")
                or ""
            ).strip()
            if candidate_id:
                submission = candidate
                break

            last_err = f"{resp.status_code} submission response missing id: {str(raw_payload)[:400]}"

        if submission is None:
            raise RuntimeError(f"DocuSeal submission create failed: {last_err}")

        submission_id = str(
            submission.get("submission_id")
            or submission.get("submissionId")
            or submission.get("id")
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
