from __future__ import annotations

from pathlib import Path
from docxtpl import DocxTemplate
from docx import Document

def render_docx(template_path: str, context: dict, out_path: str) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        tpl = DocxTemplate(template_path)
        tpl.render(context)
        tpl.save(out_path)
        return
    except Exception:
        # Fallback for missing/invalid template files during bootstrap/testing.
        doc = Document()
        doc.add_heading("Grievance Document", level=1)
        for key, value in context.items():
            doc.add_paragraph(f"{key}: {value}")
        doc.save(out_path)
