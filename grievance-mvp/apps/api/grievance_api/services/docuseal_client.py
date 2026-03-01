from __future__ import annotations

import io
import logging
import re
import subprocess
import tempfile
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
}
_DATE_FIELD_DIMENSION_HINTS: dict[str, dict[str, float]] = {
    "q5_l2_date": {"min_w": 140.0, "min_h": 20.0, "max_h": 28.0, "y_lift": 6.0},
}
_MULTILINE_TEXT_FIELDS = {
    "q6_company_statement",
    "q7_proposed_disposition_second_level",
    "q8_union_disposition",
}


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
        signature_layout_mode: str = "table_preferred",
        signature_layout_mode_by_form: dict[str, str] | None = None,
        signature_table_trace_enabled: bool = True,
        signature_table_trace_by_form: dict[str, bool] | None = None,
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
    def _normalize_area(
        *,
        raw: dict,
        field_type: str,
        attachment_uuid: str,
        field_name: str = "",
    ) -> dict:
        word_w = max(1.0, raw["x_max"] - raw["x_min"])
        word_h = max(1.0, raw["y_max"] - raw["y_min"])
        max_h: float | None = None

        if field_type == "signature":
            min_w, min_h, pad_w, pad_h = 140.0, 28.0, 8.0, 4.0
            y_lift = 14.0
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
            # Keep generic date fields visually aligned with signature rows.
            min_w, min_h, pad_w, pad_h = 120.0, 26.0, 4.0, 2.0
            y_lift = 6.0
            hint = _DATE_FIELD_DIMENSION_HINTS.get(field_name.lower())
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
                        if abs(bottom - top) < 8.0 or abs(x1 - x0) < 8.0:
                            continue
                        vertical_segments.append({"x": x0, "y_min": top, "y_max": bottom})
                        vertical_segments.append({"x": x1, "y_min": top, "y_max": bottom})
                        horizontal_segments.append({"y": top, "x_min": min(x0, x1), "x_max": max(x0, x1)})
                        horizontal_segments.append({"y": bottom, "x_min": min(x0, x1), "x_max": max(x0, x1)})

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

    def _build_table_trace_overrides(
        self,
        *,
        placeholder_areas: dict[tuple[int, str], list[dict]],
        pdf_bytes: bytes,
        form_key: str | None,
    ) -> tuple[dict[tuple[int, str], list[dict]], str]:
        rows = self._build_signer_rows(placeholder_areas=placeholder_areas, form_key=form_key)
        if not rows:
            return {}, "trace_no_signature_rows"

        page_segments = self._extract_pdf_table_segments(pdf_bytes=pdf_bytes)
        if not page_segments:
            return {}, "trace_no_page_segments"

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
    ) -> tuple[dict[tuple[int, str], list[dict]], str, str]:
        layout_mode = self._resolve_signature_layout_mode(form_key=form_key)
        if layout_mode != "table_preferred":
            return {}, "generic_fallback", "layout_mode_generic"

        trace_reason = "trace_disabled"
        if self._resolve_signature_table_trace_enabled(form_key=form_key):
            trace_overrides, trace_reason = self._build_table_trace_overrides(
                placeholder_areas=placeholder_areas,
                pdf_bytes=pdf_bytes,
                form_key=form_key,
            )
            if trace_overrides:
                return trace_overrides, "trace", trace_reason

        map_overrides, map_reason = self._build_table_map_overrides(
            placeholder_areas=placeholder_areas,
            form_key=form_key,
        )
        if map_overrides:
            return map_overrides, "map_fallback", map_reason

        return {}, "generic_fallback", f"trace_reason={trace_reason};map_reason={map_reason}"

    def _apply_placeholder_field_alignment(
        self,
        *,
        template_id: str,
        pdf_bytes: bytes,
        form_key: str | None = None,
    ) -> None:
        placeholder_areas = self._extract_placeholder_areas(pdf_bytes=pdf_bytes)
        if not placeholder_areas:
            return
        signer_indexes = sorted({signer_idx for signer_idx, _ in placeholder_areas.keys()})
        if not signer_indexes:
            return
        table_overrides, placement_strategy, placement_reason = self._resolve_signature_table_overrides(
            placeholder_areas=placeholder_areas,
            pdf_bytes=pdf_bytes,
            form_key=form_key,
        )
        self.logger.info(
            "docuseal_signature_placement_strategy",
            extra={
                "template_id": template_id,
                "form_key": str(form_key or ""),
                "strategy": placement_strategy,
                "reason": placement_reason,
                "override_count": len(table_overrides),
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
                if not field_name:
                    field_name = f"signer{signer_idx}_{field_type}"

                effective_anchors = anchors
                if field_type in {"signature", "date"} and field_name in {f"signer{signer_idx}_{field_type}", field_type}:
                    table_match = table_overrides.get((signer_idx, field_type))
                    if table_match:
                        effective_anchors = table_match
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
                if field_type == "text":
                    required = "true_intent" not in field_name
                    if field_name in _MULTILINE_TEXT_FIELDS:
                        preferences = {"multiline": True}
                elif field_type == "email":
                    field_type = "text"
                    field_name = f"signer{signer_idx}_email"
                    required = False
                    readonly = True
                elif field_type in {"signature", "date"}:
                    if "true_intent" in field_name:
                        required = False

                field_payload = {
                    "uuid": str(uuid4()),
                    "submitter_uuid": submitter_uuid,
                    "name": field_name,
                    "type": field_type,
                    "required": required,
                    "preferences": preferences,
                    "areas": [
                        self._normalize_area(
                            raw=anchor,
                            field_type=field_type,
                            attachment_uuid=attachment_uuid,
                            field_name=field_name,
                        )
                        for anchor in effective_anchors
                    ],
                }
                if readonly:
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
