"""Extracción de texto/tablas de documentos office y similares (sin deps obligatorias)."""

from __future__ import annotations

import csv
import json
import zipfile
from typing import Any, Optional
from xml.etree import ElementTree as ET

from windows_fileshare_mcp.util import clip_cell, extension_of

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _strip_xml_text(element: ET.Element) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        parts.append(_strip_xml_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _xlsx_via_openpyxl(path: str, sheet: Optional[str], max_rows: int) -> dict[str, Any]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet is None:
            name = wb.sheetnames[0]
        elif sheet.isdigit():
            idx = int(sheet)
            name = wb.sheetnames[idx]
        else:
            name = sheet
            if name not in wb.sheetnames:
                raise ValueError(f"hoja no encontrada: {sheet}")
        ws = wb[name]
        rows: list[list[Any]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            rows.append([clip_cell(c) for c in row])
        return {
            "format": "xlsx",
            "engine": "openpyxl",
            "sheet": name,
            "sheets": list(wb.sheetnames),
            "rows": rows,
            "returned_rows": len(rows),
            "truncated": len(rows) >= max_rows,
        }
    finally:
        wb.close()


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    out: list[str] = []
    for si in root.findall(f"{_NS_MAIN}si"):
        text = "".join(t.text or "" for t in si.iter(f"{_NS_MAIN}t"))
        out.append(text)
    return out


def _xlsx_sheet_name(zf: zipfile.ZipFile, sheet_sel: Optional[str]) -> tuple[str, str]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target: dict[str, str] = {}
    for rel in rels:
        if rel.get("Type", "").endswith("/worksheet"):
            rid_to_target[rel.get("Id", "")] = rel.get("Target", "")
    sheets: list[tuple[str, str]] = []
    for sh in wb.findall(f"{_NS_MAIN}sheet"):
        name = sh.get("name", "Sheet")
        rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rid_to_target.get(rid, "")
        if target.startswith("/"):
            target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        sheets.append((name, target))
    if not sheets:
        raise ValueError("libro sin hojas")
    if sheet_sel is None:
        return sheets[0]
    if sheet_sel.isdigit():
        idx = int(sheet_sel)
        if idx < 0 or idx >= len(sheets):
            raise ValueError(f"índice de hoja inválido: {sheet_sel}")
        return sheets[idx]
    for name, path in sheets:
        if name.lower() == sheet_sel.lower():
            return name, path
    raise ValueError(f"hoja no encontrada: {sheet_sel}")


def _xlsx_via_zip(path: str, sheet: Optional[str], max_rows: int) -> dict[str, Any]:
    with zipfile.ZipFile(path, "r") as zf:
        sheet_names = []
        try:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            sheet_names = [sh.get("name", "") for sh in wb.findall(f"{_NS_MAIN}sheet")]
        except KeyError:
            pass
        name, sheet_path = _xlsx_sheet_name(zf, sheet)
        shared = _xlsx_shared_strings(zf)
        root = ET.fromstring(zf.read(sheet_path))
        rows: list[list[Any]] = []
        for row_el in root.findall(f".//{_NS_MAIN}row"):
            if len(rows) >= max_rows:
                break
            cells: list[Any] = []
            for c in row_el.findall(f"{_NS_MAIN}c"):
                ref_type = c.get("t")
                v_el = c.find(f"{_NS_MAIN}v")
                if v_el is None or v_el.text is None:
                    cells.append("")
                    continue
                if ref_type == "s":
                    idx = int(v_el.text)
                    cells.append(shared[idx] if 0 <= idx < len(shared) else v_el.text)
                else:
                    cells.append(v_el.text)
            rows.append(cells)
        return {
            "format": "xlsx",
            "engine": "zipfile+xml",
            "sheet": name,
            "sheets": sheet_names,
            "rows": rows,
            "returned_rows": len(rows),
            "truncated": len(rows) >= max_rows,
        }


def _docx_via_zip(path: str, max_chars: int) -> dict[str, Any]:
    with zipfile.ZipFile(path, "r") as zf:
        data = zf.read("word/document.xml")
    root = ET.fromstring(data)
    paragraphs: list[str] = []
    for p in root.iter(f"{_NS_W}p"):
        text = _strip_xml_text(p).strip()
        if text:
            paragraphs.append(text)
    text = "\n".join(paragraphs)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "…"
    return {
        "format": "docx",
        "engine": "zipfile+xml",
        "paragraphs": paragraphs[:500],
        "text": text,
        "truncated": truncated,
    }


def _read_csv(path: str, max_rows: int) -> dict[str, Any]:
    rows: list[list[str]] = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
    return {
        "format": "csv",
        "rows": rows,
        "returned_rows": len(rows),
        "truncated": len(rows) >= max_rows,
    }


def _read_json_doc(path: str, max_chars: int) -> dict[str, Any]:
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        data = json.load(fh)
    pretty = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    truncated = len(pretty) > max_chars
    if truncated:
        pretty = pretty[:max_chars] + "…"
    return {"format": "json", "text": pretty, "truncated": truncated}


def _read_pdf(path: str, max_pages: int) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError(
            "lectura PDF requiere el paquete pypdf instalado en el intérprete del servidor"
        ) from exc
    reader = PdfReader(path)
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            break
        pages.append(page.extract_text() or "")
    text = "\n\n".join(pages)
    return {
        "format": "pdf",
        "engine": "pypdf",
        "pages_read": len(pages),
        "page_count": len(reader.pages),
        "text": text,
        "truncated": len(reader.pages) > len(pages),
    }


def read_document(path: str, sheet: Optional[str] = None, max_rows: int = 200) -> dict[str, Any]:
    """Extrae contenido según extensión. Lanza ValueError con mensaje claro si no soportado."""
    ext = extension_of(path)
    max_rows = max(1, max_rows)
    max_chars = max_rows * 500
    if ext == "xlsx":
        try:
            return _xlsx_via_openpyxl(path, sheet, max_rows)
        except ImportError:
            return _xlsx_via_zip(path, sheet, max_rows)
    if ext == "docx":
        return _docx_via_zip(path, max_chars)
    if ext == "csv":
        return _read_csv(path, max_rows)
    if ext == "json":
        return _read_json_doc(path, max_chars)
    if ext == "pdf":
        return _read_pdf(path, max_pages=min(max_rows, 50))
    raise ValueError(f"extensión no soportada para read_document: .{ext or '?'}")
