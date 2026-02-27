from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grievance_api.services.pdf_convert import docx_to_pdf


class _FakeGraphUploader:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls: list[dict] = []
        self.dry_run = False

    def convert_local_docx_to_pdf_bytes(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return self.payload


class _FailingGraphUploader(_FakeGraphUploader):
    def convert_local_docx_to_pdf_bytes(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        raise RuntimeError("graph conversion failed")


class PdfConvertEngineTests(unittest.TestCase):
    def test_graph_engine_writes_pdf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            docx = tdir / "sample.docx"
            docx.write_bytes(b"docx-bytes")

            uploader = _FakeGraphUploader(payload=b"%PDF-graph-test%")
            out = docx_to_pdf(
                str(docx),
                str(tdir),
                30,
                engine="graph_word_online",
                graph_uploader=uploader,  # type: ignore[arg-type]
                graph_site_hostname="contoso.sharepoint.com",
                graph_site_path="/sites/Grievances",
                graph_library="Documents",
                graph_temp_folder_path="Grievances/_docx_pdf_convert",
            )

            self.assertTrue(Path(out).exists())
            self.assertEqual(Path(out).read_bytes(), b"%PDF-graph-test%")
            self.assertEqual(len(uploader.calls), 1)
            self.assertEqual(uploader.calls[0]["temp_folder_path"], "Grievances/_docx_pdf_convert")

    def test_graph_engine_requires_uploader(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            docx = Path(td) / "sample.docx"
            docx.write_bytes(b"x")
            with self.assertRaises(RuntimeError):
                docx_to_pdf(
                    str(docx),
                    td,
                    30,
                    engine="graph_word_online",
                    graph_site_hostname="contoso.sharepoint.com",
                    graph_site_path="/sites/Grievances",
                    graph_library="Documents",
                )

    def test_graph_engine_requires_site_library_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            docx = Path(td) / "sample.docx"
            docx.write_bytes(b"x")
            uploader = _FakeGraphUploader(payload=b"%PDF%")
            with self.assertRaises(RuntimeError):
                docx_to_pdf(
                    str(docx),
                    td,
                    30,
                    engine="graph_word_online",
                    graph_uploader=uploader,  # type: ignore[arg-type]
                    graph_site_hostname="",
                    graph_site_path="/sites/Grievances",
                    graph_library="Documents",
                )

    def test_unknown_engine_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            docx = Path(td) / "sample.docx"
            docx.write_bytes(b"x")
            with self.assertRaises(RuntimeError):
                docx_to_pdf(str(docx), td, 30, engine="unknown-engine")

    def test_graph_engine_falls_back_to_libreoffice_when_graph_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            docx = tdir / "sample.docx"
            docx.write_bytes(b"docx-bytes")
            fallback_pdf = tdir / "sample.pdf"
            fallback_pdf.write_bytes(b"%PDF-libo-fallback%")

            uploader = _FailingGraphUploader(payload=b"")
            with patch("grievance_api.services.pdf_convert._docx_to_pdf_libo", return_value=str(fallback_pdf)) as mocked:
                out = docx_to_pdf(
                    str(docx),
                    str(tdir),
                    30,
                    engine="graph_word_online",
                    graph_uploader=uploader,  # type: ignore[arg-type]
                    graph_site_hostname="contoso.sharepoint.com",
                    graph_site_path="/sites/Grievances",
                    graph_library="Documents",
                    graph_temp_folder_path="Grievances/_docx_pdf_convert",
                )
            self.assertEqual(out, str(fallback_pdf))
            self.assertEqual(len(uploader.calls), 1)
            mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
