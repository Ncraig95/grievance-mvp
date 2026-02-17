from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Mapping


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text_body: str
    html_body: str | None


class EmailTemplateStore:
    """
    File naming convention under templates_dir:
      - <template_key>.subject.txt
      - <template_key>.txt
      - <template_key>.html (optional)
    """

    def __init__(self, templates_dir: str):
        self.templates_dir = Path(templates_dir)

    def _render_template_file(
        self,
        *,
        filename: str,
        context: Mapping[str, str],
        required: bool,
    ) -> str | None:
        p = self.templates_dir / filename
        if not p.exists():
            if required:
                raise FileNotFoundError(f"Missing required email template: {p}")
            return None
        raw = p.read_text(encoding="utf-8")
        try:
            return Template(raw).substitute(context)
        except KeyError as exc:
            raise ValueError(f"Template {p} missing placeholder value: {exc}") from exc

    def render(self, template_key: str, context: Mapping[str, object]) -> RenderedEmail:
        normalized_context = {k: "" if v is None else str(v) for k, v in context.items()}
        subject = self._render_template_file(
            filename=f"{template_key}.subject.txt",
            context=normalized_context,
            required=True,
        )
        text_body = self._render_template_file(
            filename=f"{template_key}.txt",
            context=normalized_context,
            required=True,
        )
        html_body = self._render_template_file(
            filename=f"{template_key}.html",
            context=normalized_context,
            required=False,
        )
        return RenderedEmail(
            subject=(subject or "").strip(),
            text_body=text_body or "",
            html_body=html_body,
        )
