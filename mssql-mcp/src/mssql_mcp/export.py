"""Exportación de resultados SQL a CSV/Parquet (local o S3) sin pasar por el LLM."""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import pyodbc

from mssql_mcp.config import Settings, pick_s3_credentials
from mssql_mcp.db import Session, _connect, _unique_columns, serialize_value

logger = logging.getLogger("mssql-mcp")

_BATCH = 50_000
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slug(sql: str) -> str:
    base = (sql or "query").strip().split()[0:3]
    raw = "_".join(base)[:40]
    s = _SLUG_RE.sub("_", raw).strip("_") or "query"
    return s[:60]


def _default_local_path(settings: Settings, sql: str, ext: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    return settings.export_dir / f"{stamp}_{_slug(sql)}.{ext}"


def _parse_s3(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    if p.scheme != "s3" or not p.netloc:
        raise ValueError("destination S3 debe ser s3://bucket/prefix/archivo.ext")
    bucket = p.netloc
    key = p.path.lstrip("/")
    if not key:
        raise ValueError("destination S3 requiere clave (s3://bucket/ruta/archivo.ext)")
    return bucket, key


def _write_csv_stream(
    session: Session,
    sql: str,
    path: Path,
    max_rows: int,
) -> tuple[int, list[str], int]:
    started = time.time()
    cur = session._cursor(sql)
    if cur.description is None:
        path.write_text("", encoding="utf-8")
        return 0, [], int((time.time() - started) * 1000)
    cols = _unique_columns(cur.description)
    total = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(cols)
        while total < max_rows:
            batch = cur.fetchmany(min(_BATCH, max_rows - total))
            if not batch:
                break
            for row in batch:
                writer.writerow([serialize_value(v, 0) for v in row])
            total += len(batch)
    return total, cols, int((time.time() - started) * 1000)


def _write_parquet_stream(
    session: Session,
    sql: str,
    path: Path,
    max_rows: int,
) -> tuple[int, list[str], int]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    started = time.time()
    cur = session._cursor(sql)
    if cur.description is None:
        pq.write_table(pa.table({}), path)
        return 0, [], int((time.time() - started) * 1000)
    cols = _unique_columns(cur.description)
    writer: Optional[pq.ParquetWriter] = None
    total = 0
    try:
        while total < max_rows:
            batch = cur.fetchmany(min(_BATCH, max_rows - total))
            if not batch:
                break
            arrays = [pa.array([serialize_value(r[i], 0) for r in batch]) for i in range(len(cols))]
            table = pa.table(dict(zip(cols, arrays)))
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema)
            writer.write_table(table)
            total += len(batch)
    finally:
        if writer is not None:
            writer.close()
        elif path.exists() is False:
            pq.write_table(pa.table({c: [] for c in cols}), path)
    return total, cols, int((time.time() - started) * 1000)


def _upload_s3(local: Path, uri: str, settings: Settings) -> int:
    import boto3

    bucket, key = _parse_s3(uri)
    ak, sk, token = pick_s3_credentials()
    if not ak or not sk:
        raise PermissionError("Faltan credenciales S3 (access_key/secret_key o S3_* / AWS_* en .env)")
    kw: dict[str, Any] = {
        "aws_access_key_id": ak,
        "aws_secret_access_key": sk,
        "region_name": settings.s3_region,
    }
    if token:
        kw["aws_session_token"] = token
    client = boto3.client("s3", **kw)
    client.upload_file(str(local), bucket, key)
    return local.stat().st_size


def export_query_result(
    settings: Settings,
    cnx: pyodbc.Connection,
    sql: str,
    *,
    fmt: str,
    destination: Optional[str],
) -> dict[str, Any]:
    """Ejecuta sql en cnx (ya conectada) y escribe el resultado completo."""
    fmt = (fmt or "csv").strip().lower()
    if fmt not in ("csv", "parquet"):
        raise ValueError("format debe ser 'csv' o 'parquet'")
    ext = "csv" if fmt == "csv" else "parquet"
    started = time.time()
    session = Session(cnx, settings)
    dest = (destination or "").strip()
    if dest.lower().startswith("s3://"):
        local = _default_local_path(settings, sql, ext)
        write_fn = _write_csv_stream if fmt == "csv" else _write_parquet_stream
        rows, cols, _ = write_fn(session, sql, local, settings.export_max_rows)
        bytes_written = _upload_s3(local, dest, settings)
        try:
            local.unlink(missing_ok=True)
        except OSError as e:
            logger.debug(f"no se pudo borrar temporal {local}: {e}")
        out_path = dest
    else:
        out_path_obj = Path(dest) if dest else _default_local_path(settings, sql, ext)
        out_path_obj.parent.mkdir(parents=True, exist_ok=True)
        write_fn = _write_csv_stream if fmt == "csv" else _write_parquet_stream
        rows, cols, _ = write_fn(session, sql, out_path_obj, settings.export_max_rows)
        bytes_written = out_path_obj.stat().st_size
        out_path = str(out_path_obj)
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "path": out_path,
        "format": fmt,
        "rows": rows,
        "bytes": bytes_written,
        "columns": cols,
        "elapsed_ms": elapsed_ms,
        "max_rows_limit": settings.export_max_rows,
    }


def open_export_connection(settings: Settings, server, database: Optional[str]) -> pyodbc.Connection:
    return _connect(settings, server, database)
