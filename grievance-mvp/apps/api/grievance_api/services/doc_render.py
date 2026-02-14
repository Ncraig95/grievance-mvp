from __future__ import annotations

from pathlib import Path
from docxtpl import DocxTemplate

def render_docx(template_path: str, context: dict, out_path: str) -> None:
    tpl = DocxTemplate(template_path)
    tpl.render(context)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    tpl.save(out_path)
