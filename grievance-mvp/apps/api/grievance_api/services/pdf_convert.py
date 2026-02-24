from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from .sharepoint_graph import GraphUploader


def _docx_to_pdf_libo(docx_path: str, out_dir: str, timeout_seconds: int) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "soffice",
            "--headless",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            out_dir,
            docx_path,
        ],
        check=True,
        timeout=timeout_seconds,
    )

    pdf_path = str(Path(out_dir) / (Path(docx_path).stem + ".pdf"))
    if not Path(pdf_path).exists():
        raise RuntimeError("PDF conversion failed: output PDF not found")
    return pdf_path


def docx_to_pdf(
    docx_path: str,
    out_dir: str,
    timeout_seconds: int,
    *,
    engine: str = "libreoffice",
    graph_uploader: GraphUploader | None = None,
    graph_site_hostname: str = "",
    graph_site_path: str = "",
    graph_library: str = "",
    graph_temp_folder_path: str = "",
) -> str:
    selected = (engine or "libreoffice").strip().lower()
    if selected in {"", "libreoffice", "soffice"}:
        return _docx_to_pdf_libo(docx_path, out_dir, timeout_seconds)

    if selected in {"graph_word_online", "graph", "microsoft_word_online", "word_online"}:
        if graph_uploader is None:
            raise RuntimeError("Graph PDF conversion selected but graph_uploader is not available")
        if not graph_site_hostname or not graph_site_path or not graph_library:
            raise RuntimeError("Graph PDF conversion selected but Graph site/library config is incomplete")

        # Keep dry-run and local dev behavior predictable.
        if getattr(graph_uploader, "dry_run", False):
            return _docx_to_pdf_libo(docx_path, out_dir, timeout_seconds)

        temp_folder = (graph_temp_folder_path or "_docx_pdf_convert").strip().strip("/")
        if not temp_folder:
            temp_folder = "_docx_pdf_convert"

        unique_name = f"{Path(docx_path).stem}-{uuid.uuid4().hex}.docx"
        pdf_bytes = graph_uploader.convert_local_docx_to_pdf_bytes(
            site_hostname=graph_site_hostname,
            site_path=graph_site_path,
            library=graph_library,
            temp_folder_path=temp_folder,
            filename=unique_name,
            local_path=docx_path,
        )
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        pdf_path = str(Path(out_dir) / (Path(docx_path).stem + ".pdf"))
        Path(pdf_path).write_bytes(pdf_bytes)
        if not Path(pdf_path).exists():
            raise RuntimeError("PDF conversion failed: output PDF not found")
        return pdf_path

    raise RuntimeError(f"Unsupported DOCX->PDF engine '{engine}'")
