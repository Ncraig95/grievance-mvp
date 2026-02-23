from __future__ import annotations

import html
import logging
import re
import tempfile
from pathlib import Path
from zipfile import ZipFile

from docxtpl import DocxTemplate


_JINJA_PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
_LEFTOVER_PLACEHOLDER_RE = re.compile(r"{{.*?}}", flags=re.DOTALL)
_XML_TAG_RE = re.compile(r"<[^>]+>")
_XML_TOKEN_RE = re.compile(r"(<[^>]+>)")
_SAFE_JINJA_EXPR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_NORMALIZE_KEY_RE = re.compile(r"[^A-Za-z0-9]+")
_LOG = logging.getLogger("grievance_api")
_DOCUSEAL_TAG_RE = re.compile(r"^(Sig|Dte|Eml|Txt)_es_:signer\d+:[A-Za-z0-9_]+$", re.IGNORECASE)


def _is_xml_tag_token(token: str) -> bool:
    return token.startswith("<") and token.endswith(">")


def _normalize_split_placeholders_in_xml(xml_text: str) -> str:
    """Rejoin placeholders split across multiple Word runs/text nodes."""

    tokens = _XML_TOKEN_RE.split(xml_text)
    idx = 0
    while idx < len(tokens):
        if _is_xml_tag_token(tokens[idx]):
            idx += 1
            continue

        text = tokens[idx]
        search_pos = 0
        while True:
            start = text.find("{{", search_pos)
            if start < 0:
                break

            end = text.find("}}", start + 2)
            end_idx = idx
            inner_parts: list[str] = []
            if end >= 0:
                inner_parts.append(text[start + 2 : end])
            else:
                inner_parts.append(text[start + 2 :])
                scan_idx = idx + 1
                found_end = False
                while scan_idx < len(tokens):
                    if _is_xml_tag_token(tokens[scan_idx]):
                        scan_idx += 1
                        continue
                    candidate = tokens[scan_idx]
                    candidate_end = candidate.find("}}")
                    if candidate_end < 0:
                        inner_parts.append(candidate)
                        scan_idx += 1
                        continue
                    inner_parts.append(candidate[:candidate_end])
                    end = candidate_end
                    end_idx = scan_idx
                    found_end = True
                    break
                if not found_end:
                    search_pos = start + 2
                    continue

            normalized_inner = " ".join("".join(inner_parts).split())
            replacement = "{{ " + normalized_inner + " }}"

            if end_idx == idx:
                text = text[:start] + replacement + text[end + 2 :]
                tokens[idx] = text
                search_pos = start + len(replacement)
                continue

            tokens[idx] = text[:start] + replacement
            fill_idx = idx + 1
            while fill_idx < end_idx:
                if not _is_xml_tag_token(tokens[fill_idx]):
                    tokens[fill_idx] = ""
                fill_idx += 1
            end_text = tokens[end_idx]
            tokens[end_idx] = end_text[end + 2 :]
            text = tokens[idx]
            search_pos = len(text)

        idx += 1

    return "".join(tokens)


def _escape_signature_placeholders(xml_text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        # DocuSeal signature/date tags use ":" and are not valid Jinja variables.
        # Some legacy templates include malformed placeholders that are not valid Jinja;
        # keep them literal for post-processing replacement instead of failing render.
        if ":" in expr or not _SAFE_JINJA_EXPR_RE.fullmatch(expr):
            return "{% raw %}" + match.group(0) + "{% endraw %}"
        return match.group(0)

    return _JINJA_PLACEHOLDER_RE.sub(_replace, xml_text)


def _prepare_template_docx(template_path: str, *, normalize_split_placeholders: bool) -> str:
    source = Path(template_path)
    if not source.exists():
        return template_path

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with ZipFile(source, "r") as zin, ZipFile(tmp_path, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                    patched = data.decode("utf-8", errors="ignore")
                    if normalize_split_placeholders:
                        patched = _normalize_split_placeholders_in_xml(patched)
                    patched = _escape_signature_placeholders(patched)
                    data = patched.encode("utf-8")
                zout.writestr(info, data)
        return str(tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _replace_leftover_placeholders(
    xml_text: str,
    context: dict,
    *,
    strip_signature_placeholders: bool,
) -> str:
    def _lookup_value(raw_inner: str) -> object | None:
        candidates: list[str] = []

        def _add(value: str) -> None:
            val = value.strip()
            if val and val not in candidates:
                candidates.append(val)

        compact_spaces = " ".join(raw_inner.split())
        underscore_spaces = re.sub(r"\s+", "_", compact_spaces)
        normalized = _NORMALIZE_KEY_RE.sub("_", compact_spaces).strip("_")

        for value in (
            raw_inner,
            compact_spaces,
            underscore_spaces,
            normalized,
            compact_spaces.lower(),
            underscore_spaces.lower(),
            normalized.lower(),
        ):
            _add(value)

        for key in candidates:
            if key in context:
                return context.get(key)
        return None

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        inner = _XML_TAG_RE.sub("", raw[2:-2]).strip()
        if not inner:
            return raw
        if ":" in inner:
            if strip_signature_placeholders and _DOCUSEAL_TAG_RE.fullmatch(inner):
                return ""
            return raw
        value = _lookup_value(inner)
        if value is None:
            # Drop unresolved non-signature placeholders so no raw {{...}} leaks into final docs.
            return ""
        return html.escape(str(value), quote=False)

    return _LEFTOVER_PLACEHOLDER_RE.sub(_replace, xml_text)


def _postprocess_rendered_docx(out_path: str, context: dict, *, strip_signature_placeholders: bool) -> None:
    source = Path(out_path)
    with tempfile.NamedTemporaryFile(
        suffix=".docx",
        delete=False,
        dir=str(source.parent),
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with ZipFile(source, "r") as zin, ZipFile(tmp_path, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename.startswith("word/") and info.filename.endswith(".xml"):
                    patched = _replace_leftover_placeholders(
                        data.decode("utf-8", errors="ignore"),
                        context,
                        strip_signature_placeholders=strip_signature_placeholders,
                    )
                    data = patched.encode("utf-8")
                zout.writestr(info, data)
        tmp_path.replace(source)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def render_docx(
    template_path: str,
    context: dict,
    out_path: str,
    *,
    strip_signature_placeholders: bool = False,
    normalize_split_placeholders: bool = True,
) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    patched_template_path: str | None = None
    try:
        patched_template_path = _prepare_template_docx(
            template_path,
            normalize_split_placeholders=normalize_split_placeholders,
        )
        tpl = DocxTemplate(patched_template_path)
        tpl.render(context)
        tpl.save(out_path)
        _postprocess_rendered_docx(
            out_path,
            context,
            strip_signature_placeholders=strip_signature_placeholders,
        )
        return
    except Exception as exc:
        _LOG.exception("docx_render_fallback", extra={"template_path": template_path, "error": str(exc)})
        raise RuntimeError(f"DOCX render failed for template '{template_path}': {exc}") from exc
    finally:
        if patched_template_path and patched_template_path != template_path:
            Path(patched_template_path).unlink(missing_ok=True)
