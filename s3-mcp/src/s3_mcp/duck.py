"""Consultas DuckDB sobre datos en S3 (solo lectura)."""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import unquote

from s3_mcp.config import get_settings
from botocore.exceptions import ClientError

from s3_mcp.ops import (
    ToolError,
    _ensure_bucket,
    client_for_bucket,
    find_objects,
    resolve_bucket_prefix,
)
from s3_mcp.util import clip_cell, human_size, parse_s3_uri, to_iso

_S3_URI_IN_SQL = re.compile(r"s3://[^\s'\"`]+", re.IGNORECASE)
_FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "COPY",
    "ATTACH",
    "DETACH",
    "INSTALL",
    "LOAD",
    "CREATE",
    "DROP",
    "ALTER",
    "EXPORT",
    "IMPORT",
    "PRAGMA",
    "CALL",
    "EXECUTE",
    "SET",
    "RESET",
    "VACUUM",
    "ANALYZE",
}
_FORBIDDEN_FUNCS = ("read_text", "read_blob", "read_ndjson", "read_json")
_ALLOWED_PREFIXES = ("SELECT", "WITH", "DESCRIBE", "SUMMARIZE")
_LOCAL_PATH_MARKERS = re.compile(
    r"(?:read_parquet|read_csv(?:_auto)?|read_json(?:_auto)?)\s*\(\s*['\"](?!s3://)([^'\"]+)",
    re.IGNORECASE,
)


def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def strip_literals(sql: str) -> str:
    sql = re.sub(r"N?'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '"x"', sql)
    return sql


def validate_query_sql(sql: str) -> None:
    """Valida que el SQL sea una única sentencia de solo lectura permitida."""
    if not sql or not sql.strip():
        raise ToolError("El SQL está vacío.", "InvalidArgument")
    no_comments = strip_sql_comments(sql).strip()
    if not no_comments:
        raise ToolError("El SQL está vacío.", "InvalidArgument")
    upper = strip_literals(no_comments).upper()
    if ";" in upper.rstrip().rstrip(";"):
        raise ToolError("Solo se permite una sentencia SQL.", "InvalidArgument")
    first = upper.split()[0] if upper.split() else ""
    if not first.startswith(_ALLOWED_PREFIXES):
        raise ToolError(
            f"Solo se permiten sentencias que empiecen por {', '.join(_ALLOWED_PREFIXES)}.",
            "InvalidArgument",
        )
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            raise ToolError(f"Palabra clave '{kw}' no permitida.", "InvalidArgument")
    lowered = no_comments.lower()
    for func in _FORBIDDEN_FUNCS:
        if func in lowered:
            raise ToolError(f"Función '{func}' no permitida.", "InvalidArgument")
    if _LOCAL_PATH_MARKERS.search(no_comments):
        raise ToolError(
            "Solo se permiten rutas s3:// en read_parquet/read_csv_auto/read_json_auto.",
            "InvalidArgument",
        )


def _extract_s3_uris(sql: str) -> list[str]:
    return list(dict.fromkeys(_S3_URI_IN_SQL.findall(sql)))


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    bucket, key = parse_s3_uri(uri)
    if not bucket:
        raise ToolError(f"URI S3 inválida en el SQL: {uri}", "InvalidArgument")
    _ensure_bucket(bucket)
    return bucket, unquote(key or "")


def _expand_s3_uri(uri: str) -> list[tuple[str, str, str]]:
    """Devuelve (bucket, key, uri_original) para cada objeto que cubre la URI (glob incluido)."""
    bucket, pattern = _parse_s3_uri(uri)
    if "*" not in pattern and "?" not in pattern:
        return [(bucket, pattern, uri)]
    prefix = pattern.split("*", 1)[0]
    glob_pattern = pattern[len(prefix) :] if prefix else pattern
    if prefix and not prefix.endswith("/") and "/" in prefix:
        prefix = prefix.rsplit("/", 1)[0] + "/"
    found = find_objects(
        bucket=bucket,
        prefix=prefix,
        pattern=glob_pattern or "*",
        limit=10_000,
    )
    if not found.get("matches"):
        raise ToolError(f"Ningún objeto coincide con {uri}", "NotFound")
    return [(bucket, item["key"], uri) for item in found["matches"]]


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _configure_httpfs(conn: Any) -> bool:
    """Intenta INSTALL+LOAD httpfs y CREATE SECRET. Devuelve False si falla (sin red, etc.)."""
    try:
        conn.execute("INSTALL httpfs")
        conn.execute("LOAD httpfs")
    except Exception:
        return False
    settings = get_settings()
    if not settings.credentials_configured:
        return False
    parts = [
        "CREATE OR REPLACE SECRET s3_mcp_secret (",
        "TYPE S3,",
        f"KEY_ID '{_sql_literal(settings.access_key_id)}',",
        f"SECRET '{_sql_literal(settings.secret_access_key)}',",
        f"REGION '{_sql_literal(settings.region)}'",
    ]
    if settings.session_token:
        parts.insert(-1, f"SESSION_TOKEN '{_sql_literal(settings.session_token)}',")
    if settings.endpoint_url:
        endpoint = settings.endpoint_url.rstrip("/")
        use_ssl = "true" if endpoint.lower().startswith("https") else "false"
        host = endpoint.split("://", 1)[-1]
        parts.insert(-1, f"ENDPOINT '{host}',")
        parts.insert(-1, "URL_STYLE 'path',")
        parts.insert(-1, f"USE_SSL {use_ssl},")
    parts[-1] = parts[-1] + ");"
    try:
        conn.execute(" ".join(parts))
    except Exception:
        return False
    return True


def _download_for_local(
    sql: str,
) -> tuple[str, int, str, list[str]]:
    """Descarga objetos referenciados y reescribe el SQL a rutas locales."""
    settings = get_settings()
    budget = settings.max_query_bytes
    downloaded = 0
    temp_root = tempfile.mkdtemp(prefix="s3-mcp-query-")
    cleanup: list[str] = [temp_root]
    mapping: dict[str, str] = {}
    rewritten = sql
    for uri in _extract_s3_uris(sql):
        if uri in mapping:
            continue
        targets = _expand_s3_uri(uri)
        local_paths: list[str] = []
        for bucket, key, _src in targets:
            client = client_for_bucket(bucket)
            head = client.head_object(Bucket=bucket, Key=key)
            size = int(head.get("ContentLength") or 0)
            if downloaded + size > budget:
                shutil.rmtree(temp_root, ignore_errors=True)
                raise ToolError(
                    f"La descarga superaría S3_QUERY_MAX_BYTES ({human_size(budget)}).",
                    "ObjectTooLarge",
                )
            safe_name = key.replace("/", "__")
            local_path = f"{temp_root}/{uuid.uuid4().hex}_{safe_name}"
            client.download_file(bucket, key, local_path)
            downloaded += size
            local_paths.append(local_path.replace("\\", "/"))
        if len(local_paths) == 1:
            mapping[uri] = local_paths[0]
        else:
            mapping[uri] = local_paths
    for uri, target in mapping.items():
        if isinstance(target, list):
            joined = ", ".join(f"'{_sql_literal(path)}'" for path in target)
            replacement = f"[{joined}]"
        else:
            replacement = f"'{_sql_literal(target)}'"
        quoted = f"'{uri}'"
        if quoted in rewritten:
            rewritten = rewritten.replace(quoted, replacement)
        elif f'"{uri}"' in rewritten:
            rewritten = rewritten.replace(f'"{uri}"', replacement)
        else:
            rewritten = rewritten.replace(uri, replacement)
    return rewritten, downloaded, "local_download", cleanup


def _serialize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return clip_cell(value)
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime):
            return to_iso(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        return value.isoformat()
    if isinstance(value, Decimal):
        return clip_cell(str(value))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:200]).hex()
    if isinstance(value, (list, dict)):
        return clip_cell(value)
    return clip_cell(str(value))


def _rows_from_result(result: Any, limit: int) -> tuple[list[str], list[dict], bool]:
    columns = [desc[0] for desc in result.description]
    fetched = result.fetchmany(limit + 1)
    truncated = len(fetched) > limit
    if truncated:
        fetched = fetched[:limit]
    rows = [{col: _serialize_value(row[idx]) for idx, col in enumerate(columns)} for row in fetched]
    return columns, rows, truncated


def query_data(sql: str, bucket: Optional[str] = None) -> dict[str, Any]:
    validate_query_sql(sql)
    if bucket:
        expected = _ensure_bucket(bucket)
        for uri in _extract_s3_uris(sql):
            bkt, _ = _parse_s3_uri(uri)
            if bkt != expected:
                raise ToolError(
                    f"La URI {uri} no pertenece al bucket indicado ({expected}).",
                    "InvalidArgument",
                )
    try:
        import duckdb
    except ImportError as exc:
        raise ToolError("duckdb no está instalado.", "DependencyMissing") from exc

    settings = get_settings()
    conn = duckdb.connect()
    cleanup_dirs: list[str] = []
    mode = "httpfs"
    run_sql = sql
    bytes_downloaded = 0
    try:
        if not _configure_httpfs(conn):
            run_sql, bytes_downloaded, mode, cleanup_dirs = _download_for_local(sql)
        result = conn.execute(run_sql)
        if result.description is None:
            return {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
                "mode": mode,
                "bytes_downloaded": bytes_downloaded,
            }
        columns, rows, truncated = _rows_from_result(result, settings.max_query_rows)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "mode": mode,
            "bytes_downloaded": bytes_downloaded,
        }
    finally:
        conn.close()
        for path in cleanup_dirs:
            shutil.rmtree(path, ignore_errors=True)


def _parquet_schema_for_key(bucket: str, key: str) -> list[dict[str, str]]:
    from s3_mcp.ops import _open_parquet, _S3RangeRaw

    client = client_for_bucket(bucket)
    head = client.head_object(Bucket=bucket, Key=key)
    size = int(head.get("ContentLength") or 0)
    limit = get_settings().max_parquet_read_bytes
    raw = _S3RangeRaw(client, bucket, key, size, max_bytes=limit)
    parquet = _open_parquet(raw)
    return [{"name": field.name, "type": str(field.type)} for field in parquet.schema_arrow]


def _schema_fingerprint(schema: list[dict[str, str]]) -> tuple[tuple[str, str], ...]:
    return tuple((col["name"], col["type"]) for col in schema)


def schema_of_prefix(
    prefix: str,
    bucket: Optional[str] = None,
    sample_files: int = 20,
) -> dict[str, Any]:
    sample_files = max(1, min(int(sample_files), 200))
    name, resolved = resolve_bucket_prefix(bucket, prefix)
    matches = find_objects(
        bucket=name,
        prefix=resolved,
        extensions=["parquet"],
        sort_by="last_modified",
        descending=True,
        limit=sample_files,
    )
    files = matches.get("matches") or []
    if not files:
        raise ToolError("No hay archivos parquet bajo el prefijo.", "NotFound")
    sampled: list[dict[str, Any]] = []
    groups: dict[tuple[tuple[str, str], ...], list[str]] = {}
    errors: list[dict[str, str]] = []
    for item in files:
        key = item["key"]
        try:
            schema = _parquet_schema_for_key(name, key)
            fp = _schema_fingerprint(schema)
            groups.setdefault(fp, []).append(key)
            sampled.append({"key": key, "schema": schema, "num_columns": len(schema)})
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})
    canonical_fp = max(groups.keys(), key=lambda fp: len(groups[fp])) if groups else ()
    canonical_schema = [{"name": n, "type": t} for n, t in canonical_fp]
    drift = [
        {"keys": keys, "schema": [{"name": n, "type": t} for n, t in fp]}
        for fp, keys in groups.items()
        if fp != canonical_fp
    ]
    return {
        "bucket": name,
        "prefix": resolved,
        "files_sampled": len(sampled),
        "files_available": matches.get("matched_total", len(files)),
        "scan_truncated": matches.get("scan_truncated", False),
        "canonical_schema": canonical_schema,
        "schema_drift": drift,
        "files": sampled,
        "errors": errors or None,
    }


def _duck_read_source(paths: list[str], fmt: str) -> str:
    escaped = ", ".join(f"'{p}'" for p in paths)
    if fmt == "parquet":
        if len(paths) == 1:
            return f"read_parquet('{paths[0]}')"
        return f"read_parquet([{escaped}])"
    if fmt == "csv":
        if len(paths) == 1:
            return f"read_csv_auto('{paths[0]}')"
        return f"read_csv_auto([{escaped}])"
    if len(paths) == 1:
        return f"read_json_auto('{paths[0]}')"
    return f"read_json_auto([{escaped}])"


def _resolve_table_paths(
    bucket: Optional[str],
    key_or_prefix: str,
    max_files: int = 50,
) -> tuple[str, list[str], str]:
    uri_bucket, uri_path = parse_s3_uri(key_or_prefix)
    if uri_bucket:
        name = _ensure_bucket(uri_bucket)
        path = uri_path or ""
    else:
        name, path = resolve_bucket_prefix(bucket, key_or_prefix)
    if path and not path.endswith("/") and "*" not in path:
        client = client_for_bucket(name)
        try:
            client.head_object(Bucket=name, Key=path)
            is_prefix = False
        except ClientError:
            is_prefix = True
    else:
        is_prefix = True
    if not is_prefix:
        lower = path.lower()
        if lower.endswith(".parquet"):
            fmt = "parquet"
        elif lower.endswith((".csv", ".tsv", ".csv.gz", ".tsv.gz")):
            fmt = "csv"
        elif lower.endswith((".json", ".jsonl", ".ndjson", ".json.gz")):
            fmt = "json"
        else:
            raise ToolError("Formato no soportado para sample_rows.", "InvalidArgument")
        return name, [path], fmt
    found = find_objects(
        bucket=name,
        prefix=path.rstrip("/") + ("/" if path and not path.endswith("/") else ""),
        extensions=["parquet", "csv", "json", "jsonl", "ndjson"],
        limit=max_files,
    )
    keys = [item["key"] for item in found.get("matches") or []]
    if not keys:
        raise ToolError("No hay archivos tabulares bajo la ruta.", "NotFound")
    fmt = "parquet" if keys[0].lower().endswith(".parquet") else "csv"
    if keys[0].lower().endswith((".json", ".jsonl", ".ndjson")):
        fmt = "json"
    return name, keys, fmt


def sample_rows(
    key_or_prefix: str,
    bucket: Optional[str] = None,
    n: int = 20,
    random: bool = True,
) -> dict[str, Any]:
    n = max(1, min(int(n), 500))
    try:
        import duckdb
    except ImportError as exc:
        raise ToolError("duckdb no está instalado.", "DependencyMissing") from exc

    name, keys, fmt = _resolve_table_paths(bucket, key_or_prefix)
    settings = get_settings()
    conn = duckdb.connect()
    cleanup_dirs: list[str] = []
    mode = "httpfs"
    try:
        use_httpfs = _configure_httpfs(conn)
        paths: list[str] = []
        downloaded = 0
        if use_httpfs:
            paths = [f"s3://{name}/{key}" for key in keys]
        else:
            temp_root = tempfile.mkdtemp(prefix="s3-mcp-sample-")
            cleanup_dirs.append(temp_root)
            client = client_for_bucket(name)
            for key in keys:
                head = client.head_object(Bucket=name, Key=key)
                size = int(head.get("ContentLength") or 0)
                if downloaded + size > settings.max_query_bytes:
                    raise ToolError(
                        f"La descarga superaría S3_QUERY_MAX_BYTES ({human_size(settings.max_query_bytes)}).",
                        "ObjectTooLarge",
                    )
                local = f"{temp_root}/{key.replace('/', '__')}"
                client.download_file(name, key, local)
                downloaded += size
                paths.append(local.replace("\\", "/"))
            mode = "local_download"
        source = _duck_read_source(paths, fmt)
        if random:
            query = f"SELECT * FROM {source} USING SAMPLE {n}"
        else:
            query = f"SELECT * FROM {source} LIMIT {n}"
        result = conn.execute(query)
        columns, rows, truncated = _rows_from_result(result, n)
        return {
            "bucket": name,
            "keys": keys,
            "format": fmt,
            "random": bool(random),
            "columns": columns,
            "rows": rows,
            "rows_returned": len(rows),
            "truncated": truncated,
            "mode": mode,
        }
    finally:
        conn.close()
        for path in cleanup_dirs:
            shutil.rmtree(path, ignore_errors=True)
