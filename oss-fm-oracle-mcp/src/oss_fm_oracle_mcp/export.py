"""Exportación de consultas a CSV/Parquet en disco (sin pasar por el LLM)."""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from oss_fm_oracle_mcp.config import Settings, get_settings, resolve_rcs_optional
from oss_fm_oracle_mcp.oracle import execute_query
from oss_fm_oracle_mcp.safety import SqlSafetyError, validate_readonly_sql

logger = logging.getLogger("oss-fm-oracle-mcp")

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slug(sql: str) -> str:
    parts = (sql or "query").strip().split()[:3]
    raw = "_".join(parts)[:40]
    return (_SLUG_RE.sub("_", raw).strip("_") or "query")[:60]


def export_query(
    sql: str,
    rcs: str | list[str] | None = None,
    format: Literal["csv", "parquet"] = "csv",
    max_rows: int | None = None,
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Ejecuta SELECT/WITH y escribe un archivo por RC bajo export_dir."""
    settings = settings or get_settings()
    settings.validate_credentials()
    names = resolve_rcs_optional(rcs)
    limit = settings.effective_rows(max_rows)
    fmt = format.lower().strip()
    if fmt not in ("csv", "parquet"):
        raise ValueError("format debe ser 'csv' o 'parquet'")

    try:
        validated = validate_readonly_sql(sql)
    except SqlSafetyError as e:
        return {"ok": False, "error": str(e), "error_kind": "safety"}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = settings.export_dir / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(validated.cleaned)

    files: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for rc in names:
        path = out_dir / f"{rc}_{slug}.{fmt}"
        try:
            meta = _write_from_query_result(
                rc, path, fmt, sql, limit, settings, timeout_s
            )
            files.append(meta)
        except Exception as e:
            logger.warning("export_query falló %s: %s", rc, e)
            errors.append({"rc": rc, "error": str(e)})

    ok = len(files) > 0
    return {
        "ok": ok,
        "export_dir": str(out_dir),
        "format": fmt,
        "max_rows_per_rc": limit,
        "files": files,
        "errors": errors or None,
        "rcs_requested": names,
    }


def _write_from_query_result(
    rc: str,
    path: Path,
    fmt: str,
    sql: str,
    max_rows: int,
    settings: Settings,
    timeout_s: int | None,
) -> dict[str, Any]:
    """Usa execute_query (incluye troceo rc8) y persiste filas en disco."""
    started = time.perf_counter()
    result = execute_query(rc, sql, max_rows=max_rows, timeout_s=timeout_s, settings=settings)
    if not result.ok:
        raise RuntimeError(result.error or "consulta fallida")
    columns = result.columns
    rows = result.rows
    if fmt == "csv":
        _write_csv_file(path, columns, rows)
    else:
        _write_parquet_file(path, columns, rows)
    return {
        "rc": rc,
        "path": str(path),
        "row_count": len(rows),
        "columns": columns,
        "truncated": result.truncated,
        "chunked": result.chunked,
        "execution_time_s": round(time.perf_counter() - started, 3),
    }


def _write_csv_file(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c) for c in columns])


def _write_parquet_file(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not columns:
        _empty_parquet(path)
        return
    data = {c: [row.get(c) for row in rows] for c in columns}
    table = pa.table(data)
    pq.write_table(table, path)


def _empty_parquet(path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.table({}), path)
