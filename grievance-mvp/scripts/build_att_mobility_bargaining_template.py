from __future__ import annotations

import argparse
import tempfile
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt


SOURCE_DOC = Path("Docx Files Template/AT&TMobility2023BargainingSuggestion_Form.docx")
DEFAULT_OUTPUT = SOURCE_DOC


def _set_cell_border(cell, **kwargs) -> None:  # noqa: ANN001
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_borders = tc_pr.first_child_found_in("w:tcBorders")
    if tc_borders is None:
        tc_borders = OxmlElement("w:tcBorders")
        tc_pr.append(tc_borders)
    for edge in ("left", "top", "right", "bottom"):
        edge_data = kwargs.get(edge)
        if not edge_data:
            continue
        tag = f"w:{edge}"
        element = tc_borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            tc_borders.append(element)
        for key, value in edge_data.items():
            element.set(qn(f"w:{key}"), str(value))


def _set_default_font(document: Document) -> None:
    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10.5)
    for style_name in ("Title", "Heading 1", "Heading 2"):
        if style_name in styles:
            styles[style_name].font.name = "Arial"


def _extract_logo_to_temp(source_doc: Path) -> Path | None:
    if not source_doc.exists():
        return None
    with zipfile.ZipFile(source_doc) as archive:
        names = [name for name in archive.namelist() if name.startswith("word/media/")]
        if not names:
            return None
        suffix = Path(names[0]).suffix or ".png"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(archive.read(names[0]))
            return Path(tmp.name)


def _add_heading(document: Document) -> None:
    heading = document.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = heading.add_run("ONLY ONE CONTRACT ARTICLE PER FORM")
    run.bold = True
    run.underline = True
    run.font.size = Pt(13)
    heading.space_after = Pt(8)


def _add_top_block(document: Document, logo_path: Path | None) -> None:
    table = document.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.columns[0].width = Inches(1.5)
    table.columns[1].width = Inches(4.9)

    left = table.cell(0, 0)
    right = table.cell(0, 1)
    left.vertical_alignment = WD_ALIGN_VERTICAL.TOP
    right.vertical_alignment = WD_ALIGN_VERTICAL.TOP

    if logo_path and logo_path.exists():
        p = left.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        run = p.add_run()
        run.add_picture(str(logo_path), width=Inches(0.9))

    nested = right.add_table(rows=1, cols=1)
    nested.alignment = WD_TABLE_ALIGNMENT.RIGHT
    nested.autofit = False
    nested.columns[0].width = Inches(4.7)
    box_cell = nested.cell(0, 0)
    box_cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    _set_cell_border(
        box_cell,
        left={"val": "single", "sz": 18, "color": "000000"},
        top={"val": "single", "sz": 18, "color": "000000"},
        right={"val": "single", "sz": 18, "color": "000000"},
        bottom={"val": "single", "sz": 18, "color": "000000"},
    )

    p1 = box_cell.paragraphs[0]
    p1.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r1 = p1.add_run("FOR LOCAL USE ONLY:")
    r1.bold = True
    r1.font.size = Pt(8.5)

    p2 = box_cell.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r2 = p2.add_run("ARTICLE AFFECTED: ")
    r2.bold = True
    r2.font.size = Pt(10)
    p2.add_run("{{Txt_es_:signer1:article_affected}}")
    p2.add_run(" __________________________")


def _add_title(document: Document) -> None:
    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.space_before = Pt(6)
    p.space_after = Pt(6)
    run = p.add_run("BARGAINING DEMAND AT&T MOBILITY")
    run.bold = True
    run.font.size = Pt(12)


def _add_labeled_line(document: Document, label: str, placeholder: str, *, align: str = "left") -> None:
    p = document.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if align == "right" else WD_ALIGN_PARAGRAPH.LEFT
    p.space_after = Pt(3)
    label_run = p.add_run(f"{label} ")
    label_run.bold = True
    label_run.font.size = Pt(10.5)
    value_run = p.add_run(f"{{{{ {placeholder} }}}}")
    value_run.underline = True


def _add_dynamic_section(document: Document, title: str, rows_key: str) -> None:
    title_p = document.add_paragraph()
    title_p.space_before = Pt(6)
    title_p.space_after = Pt(0)
    title_run = title_p.add_run(title)
    title_run.bold = True
    title_run.font.size = Pt(10.5)

    begin = document.add_paragraph()
    begin.add_run(f"{{%p for line in {rows_key} %}}")

    body = document.add_paragraph()
    body.paragraph_format.left_indent = Inches(0.15)
    body.paragraph_format.space_after = Pt(0)
    body.add_run("{{ line.text }}")

    end = document.add_paragraph()
    end.add_run("{%p endfor %}")


def _add_contact_block(document: Document) -> None:
    phones = document.add_paragraph()
    phones.space_before = Pt(6)
    work = phones.add_run("TELEPHONE: (WORK) ")
    work.bold = True
    phones.add_run("{{ work_phone }}")
    phones.add_run("    (HOME) ").bold = True
    phones.add_run("{{ home_phone }}")

    email = document.add_paragraph()
    email_run = email.add_run("NON-WORK EMAIL ADDRESS: ")
    email_run.bold = True
    email.add_run("{{ non_work_email }}")


def _add_signature_line(document: Document) -> None:
    p = document.add_paragraph()
    p.space_before = Pt(8)
    label_run = p.add_run("SIGNATURE OF LOCAL PRESIDENT: ")
    label_run.bold = True
    p.add_run("{{Sig_es_:signer1:signature}}")
    p.add_run(" ________________________________")


def build_template(output_path: Path, source_doc: Path) -> None:
    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.7)
    section.right_margin = Inches(0.7)
    section.start_type = WD_SECTION.NEW_PAGE
    _set_default_font(document)

    logo_path = _extract_logo_to_temp(source_doc)
    try:
        _add_heading(document)
        _add_top_block(document, logo_path)
        _add_title(document)
        _add_labeled_line(document, "LOCAL#:", "local_number", align="right")
        _add_labeled_line(document, "DEMAND FROM LOCAL:", "demand_from_local")
        _add_labeled_line(document, "TITLE OF MOBILITY MEMBER SUBMITTING DEMAND:", "submitting_member_title")
        _add_labeled_line(document, "NAME OF MOBILITY MEMBER SUBMITTING DEMAND:", "submitting_member_name")
        _add_dynamic_section(document, "DEMAND:", "demand_rows")
        _add_dynamic_section(document, "REASON:", "reason_rows")
        _add_dynamic_section(document, "SPECIFIC EXAMPLES:", "specific_examples_rows")
        _add_contact_block(document)
        _add_signature_line(document)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(output_path))
    finally:
        if logo_path:
            logo_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-doc", default=str(SOURCE_DOC))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_template(Path(args.output), Path(args.source_doc))


if __name__ == "__main__":
    main()
