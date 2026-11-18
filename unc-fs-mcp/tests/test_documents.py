"""Tests de extracción de documentos (sin red)."""

from __future__ import annotations

import json
import zipfile

from unc_fs_mcp.documents import read_document


def test_read_json_document(tmp_path):
    p = tmp_path / "doc.json"
    p.write_text(json.dumps({"a": 1, "b": [2, 3]}), encoding="utf-8")
    out = read_document(str(p), max_rows=50)
    assert out["format"] == "json"
    assert '"a": 1' in out["text"]


def test_read_docx_zip(tmp_path):
    p = tmp_path / "t.docx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hola docx</w:t></w:r></w:p></w:body></w:document>",
        )
    out = read_document(str(p))
    assert out["format"] == "docx"
    assert "Hola docx" in out["text"]
