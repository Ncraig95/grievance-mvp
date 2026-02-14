from __future__ import annotations

import subprocess
from pathlib import Path

def docx_to_pdf(docx_path: str, out_dir: str, timeout_seconds: int) -> str:
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
