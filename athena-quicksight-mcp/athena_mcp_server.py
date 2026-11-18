#!/usr/bin/env python3
"""
Athena MCP Server
=================
MCP server para Amazon Athena usando pyathena + FastMCP.

Variables de entorno requeridas:
  AWS_ACCESS_KEY_ID      - Clave de acceso AWS
  AWS_SECRET_ACCESS_KEY  - Clave secreta AWS
  AWS_REGION             - Región AWS (ej: us-east-1)
  ATHENA_OUTPUT_S3       - S3 output location (ej: s3://bucket/prefix/)

Opcionales:
  ATHENA_DATABASE        - Base de datos por defecto
  AWS_SESSION_TOKEN      - Token de sesión (para credenciales temporales)
  ATHENA_WORKGROUP       - Workgroup de Athena (default: primary)
  ATHENA_QUERY_TIMEOUT   - Timeout en segundos para queries (default: 120)
  AWS_DISABLE_SSL_VERIFY - Si "1", deshabilita verificación SSL (para Zscaler/proxy corporativo)

Mejoras implementadas v2:
  - Validación completa de variables de entorno al arrancar
  - Fix: fetchone() ya no descarta una fila al verificar truncamiento
  - Tipos de dato nativos preservados (int, float, date, bool) en lugar de str()
  - Sanitización de identificadores SQL para prevenir inyección
  - Timeout configurable por query
  - Cache de schemas en memoria durante la sesión
  - Tool: get_query_stats — bytes escaneados y costo estimado
  - Tool: paginate_query — paginación real sin re-ejecutar
  - Logging mejorado con nivel configurable

Mejoras implementadas v2.2:
  - SSL bypass para ambientes con proxy corporativo (Zscaler)
    Activar con variable de entorno: AWS_DISABLE_SSL_VERIFY=1
    Patch directo sobre botocore.httpsession.URLLib3Session.send (confirmado por stacktrace)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# ── SSL bypass para ambientes con proxy corporativo (Zscaler) ──────────────
# Stacktrace confirma: botocore.httpsession.URLLib3Session.send es el punto
# donde ocurre el SSLError. Parcheamos esa clase directamente.
import urllib3
import botocore.httpsession

_SKIP_SSL = os.environ.get("AWS_DISABLE_SSL_VERIFY", "").lower() in ("1", "true", "yes")

# Preferred over _SKIP_SSL: trust the proxy's CA instead of trusting nothing.
# pyathena builds its own boto3 session, so the bundle cannot be passed as a
# verify= argument; it has to go through the same patch point.
_CA_BUNDLE = os.environ.get("AWS_CA_BUNDLE", "").strip().strip('"')
# 2026-09-03: fallback al bundle corporativo junto al script si la env no lo define
# (Claude Desktop arranca el MCP con env vacio -> CERTIFICATE_VERIFY_FAILED tras Zscaler).
if not _CA_BUNDLE:
    _default_bundle = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corp-ca-bundle.pem")
    if os.path.isfile(_default_bundle):
        _CA_BUNDLE = _default_bundle
if _CA_BUNDLE and not os.path.isfile(_CA_BUNDLE):
    print(f"AWS_CA_BUNDLE not found, ignoring: {_CA_BUNDLE}", file=sys.stderr)
    _CA_BUNDLE = ""

if _SKIP_SSL or _CA_BUNDLE:
    # An explicit bundle wins: it keeps verification on.
    _verify_value: Any = _CA_BUNDLE if _CA_BUNDLE else False
    if not _CA_BUNDLE:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _orig_httpsession_send = botocore.httpsession.URLLib3Session.send
    def _patched_httpsession_send(self, request):
        self._verify = _verify_value
        return _orig_httpsession_send(self, request)
    botocore.httpsession.URLLib3Session.send = _patched_httpsession_send
# ──────────────────────────────────────────────────────────────────────────

from pyathena import connect
from pyathena.cursor import Cursor
from mcp.server.fastmcp import FastMCP
import sqlparse
from sqlparse import tokens as sql_tokens

from aws_data_tools import _client, _safe_error, register_tools
from quicksight_tools import register_quicksight_tools

# ============================================================
# Configuración desde variables de entorno
# ============================================================

AWS_ACCESS_KEY_ID     = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_SESSION_TOKEN     = os.environ.get("AWS_SESSION_TOKEN", "")
AWS_REGION            = os.environ.get("AWS_REGION", "")
ATHENA_DATABASE       = os.environ.get("ATHENA_DATABASE", "")
ATHENA_WORKGROUP      = os.environ.get("ATHENA_WORKGROUP", "primary")
ATHENA_OUTPUT_S3      = os.environ.get("ATHENA_OUTPUT_S3", "")
ATHENA_QUERY_TIMEOUT  = int(os.environ.get("ATHENA_QUERY_TIMEOUT", "120"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), stream=sys.stderr)
logger = logging.getLogger("athena-mcp")

if _CA_BUNDLE:
    logger.info("SSL verification ON, usando CA bundle corporativo: %s", _CA_BUNDLE)
elif _SKIP_SSL:
    logger.warning(
        "⚠️  SSL verification DISABLED (AWS_DISABLE_SSL_VERIFY=1). Solo para redes "
        "corporativas. Preferible definir AWS_CA_BUNDLE con la CA del proxy."
    )

# ============================================================
# Cache de schemas en memoria (vive mientras el servidor esté activo)
# ============================================================

_schema_cache: dict[str, dict] = {}   # key: "db.table" → resultado de describe
_tables_cache: dict[str, dict] = {}   # key: db → resultado de list_tables
_CACHE_TTL_S = 300                     # 5 minutos

def _cache_key(*parts: str) -> str:
    return ".".join(p.lower() for p in parts if p)

def _cache_get(store: dict, key: str) -> dict | None:
    entry = store.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL_S:
        return entry["data"]
    return None

def _cache_set(store: dict, key: str, data: dict) -> None:
    store[key] = {"ts": time.time(), "data": data}

# ============================================================
# Serialización de tipos nativos
# ============================================================

def _serialize_value(val: Any) -> Any:
    """Convierte tipos de Athena a tipos JSON-serializables conservando semántica."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (datetime,)):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, timedelta):
        return str(val)
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    return str(val)

# ============================================================
# Sanitización de identificadores SQL
# ============================================================

_SAFE_IDENTIFIER = re.compile(r'^[a-zA-Z0-9_\.\-]+$')

def _validate_identifier(name: str, label: str = "identificador") -> str:
    name = name.strip()
    if not name:
        raise ValueError(f"{label} no puede estar vacío.")
    if not _SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"{label} contiene caracteres no permitidos: '{name}'. "
            "Solo se permiten letras, números, guiones bajos, puntos y guiones."
        )
    return name

def _quote_identifier(name: str, label: str = "identificador") -> str:
    """Valida y cita identificadores Athena, incluyendo nombres tipo db.table."""
    safe_name = _validate_identifier(name, label)
    return ".".join(f"`{part}`" for part in safe_name.split("."))


def _check_sql_intent(sql: str, allow_destructive: bool = False) -> None:
    """Require an explicit flag for SQL that removes or replaces existing data."""
    statements = sqlparse.parse(sql)
    if len(statements) != 1 or not sql.strip():
        raise ValueError("Se requiere una sola sentencia SQL no vacía.")
    cleaned = " ".join(
        " " if token.ttype in sql_tokens.Comment or token.ttype in sql_tokens.Literal.String else token.value
        for token in statements[0].flatten()
    )
    destructive = re.search(
        r"\b(?:DROP|DELETE|TRUNCATE|ALTER|UPDATE|MERGE)\b|"
        r"\bCREATE\s+OR\s+REPLACE\b|\bINSERT\s+OVERWRITE\b",
        cleaned, flags=re.IGNORECASE,
    )
    if destructive and not allow_destructive:
        raise ValueError("La sentencia puede eliminar o reemplazar datos; repite con allow_destructive=true.")

# ============================================================
# Conexión a Athena
# ============================================================

def get_connection(database: str | None = None) -> Any:
    """Crea y retorna una conexión a Athena usando variables de entorno."""
    db = database or ATHENA_DATABASE
    kwargs: dict[str, Any] = {"region_name": AWS_REGION}
    if db:
        kwargs["schema_name"] = db
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"]     = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    if AWS_SESSION_TOKEN:
        kwargs["aws_session_token"] = AWS_SESSION_TOKEN
    if ATHENA_OUTPUT_S3:
        kwargs["s3_staging_dir"] = ATHENA_OUTPUT_S3
    if ATHENA_WORKGROUP:
        kwargs["work_group"] = ATHENA_WORKGROUP
    return connect(**kwargs)


def execute_query(sql: str, database: str | None = None, max_rows: int = 500, page: int = 1) -> dict:
    if page < 1:
        page = 1
    conn = get_connection(database)
    try:
        cursor: Cursor = conn.cursor()
        cursor.execute(sql)
        if cursor.description is None:
            return {
                "type": "execute",
                "message": "Query ejecutado sin resultados (DDL/DML).",
                "query_execution_id": getattr(cursor, "query_id", None),
            }
        columns = [desc[0] for desc in cursor.description]
        total_to_fetch = page * max_rows
        all_rows = cursor.fetchmany(total_to_fetch)
        offset = (page - 1) * max_rows
        page_rows = all_rows[offset:]
        next_row = cursor.fetchone()
        has_more = next_row is not None
        result_rows = [
            {col: _serialize_value(val) for col, val in zip(columns, row)}
            for row in page_rows
        ]
        return {
            "type": "query",
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "page": page,
            "max_rows": max_rows,
            "has_more": has_more,
            "query_execution_id": getattr(cursor, "query_id", None),
        }
    finally:
        conn.close()

# ============================================================
# MCP Server
# ============================================================

mcp = FastMCP("athena-quicksight")


@mcp.tool()
def query_athena(sql: str, database: str = "", max_rows: int = 100, page: int = 1,
                 allow_destructive: bool = False) -> str:
    """
    Ejecuta un query SQL en Amazon Athena.

    Args:
        sql: Query SQL a ejecutar (SELECT, SHOW, DESCRIBE, etc.).
        database: Base de datos objetivo. Opcional; usa ATHENA_DATABASE si no se indica.
        max_rows: Máximo de filas a retornar por página (default: 100, max: 1000).
        page: Número de página para paginación (default: 1).

    Returns:
        JSON con columnas, filas, page, has_more y query_execution_id.
    """
    max_rows = min(max(1, max_rows), 1000)
    db = database if database else None
    try:
        _check_sql_intent(sql, allow_destructive)
        result = execute_query(sql, database=db, max_rows=max_rows, page=page)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("query_athena error: %s", type(e).__name__)
        return json.dumps(_safe_error(e), ensure_ascii=False)


@mcp.tool()
def list_databases() -> str:
    """Lista todas las bases de datos disponibles en Athena."""
    try:
        result = execute_query("SHOW DATABASES", max_rows=500)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("list_databases error: %s", type(e).__name__)
        return json.dumps(_safe_error(e), ensure_ascii=False)


@mcp.tool()
def list_tables(database: str = "", use_cache: bool = True) -> str:
    """
    Lista todas las tablas de una base de datos en Athena.

    Args:
        database: Base de datos a consultar (usa la default si no se especifica).
        use_cache: Si True, usa caché en memoria (TTL: 5 min). Default: True.
    """
    db = database if database else (ATHENA_DATABASE or "")
    cache_key = _cache_key("tables", db)
    if use_cache:
        cached = _cache_get(_tables_cache, cache_key)
        if cached:
            cached["_from_cache"] = True
            return json.dumps(cached, ensure_ascii=False, indent=2)
    db_arg = database if database else None
    try:
        result = execute_query("SHOW TABLES", database=db_arg, max_rows=500)
        _cache_set(_tables_cache, cache_key, result)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("list_tables error: %s", type(e).__name__)
        return json.dumps(_safe_error(e), ensure_ascii=False)


@mcp.tool()
def describe_table(table_name: str, database: str = "", use_cache: bool = True) -> str:
    """
    Describe la estructura de una tabla en Athena (columnas y tipos).

    Args:
        table_name: Nombre de la tabla.
        database: Base de datos (usa la default si no se especifica).
        use_cache: Si True, usa caché en memoria (TTL: 5 min). Default: True.
    """
    try:
        safe_table = _validate_identifier(table_name, "table_name")
        quoted_table = _quote_identifier(table_name, "table_name")
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    db = database if database else (ATHENA_DATABASE or "")
    cache_key = _cache_key("schema", db, safe_table)
    if use_cache:
        cached = _cache_get(_schema_cache, cache_key)
        if cached:
            cached["_from_cache"] = True
            return json.dumps(cached, ensure_ascii=False, indent=2)
    db_arg = database if database else None
    try:
        result = execute_query(f"DESCRIBE {quoted_table}", database=db_arg, max_rows=500)
        result["table"] = safe_table
        _cache_set(_schema_cache, cache_key, result)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("describe_table error: %s", type(e).__name__)
        return json.dumps(_safe_error(e), ensure_ascii=False)


@mcp.tool()
def show_create_table(table_name: str, database: str = "") -> str:
    """
    Muestra el DDL (CREATE TABLE) de una tabla en Athena.

    Args:
        table_name: Nombre de la tabla.
        database: Base de datos (usa la default si no se especifica).
    """
    try:
        safe_table = _validate_identifier(table_name, "table_name")
        quoted_table = _quote_identifier(table_name, "table_name")
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    db_arg = database if database else None
    try:
        result = execute_query(f"SHOW CREATE TABLE {quoted_table}", database=db_arg, max_rows=100)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("show_create_table error: %s", type(e).__name__)
        return json.dumps(_safe_error(e), ensure_ascii=False)


@mcp.tool()
def test_connection() -> str:
    """
    Prueba la conexión a Athena ejecutando SELECT 1.

    Returns:
        Estado de la conexión, configuración activa y variables validadas.
    """
    try:
        result = execute_query("SELECT 1 AS test_ok", max_rows=1)
        result["status"] = "connected"
        result["region"]    = AWS_REGION
        result["database"]  = ATHENA_DATABASE or "(no definida)"
        result["workgroup"] = ATHENA_WORKGROUP
        result["output_s3"] = ATHENA_OUTPUT_S3 or "(definido por workgroup)"
        result["timeout_s"] = ATHENA_QUERY_TIMEOUT
        result["ssl_verify_disabled"] = _SKIP_SSL
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("test_connection error: %s", type(e).__name__)
        return json.dumps(
            {"status": "error", **_safe_error(e), "region": AWS_REGION, "database": ATHENA_DATABASE or "(no definida)"},
            ensure_ascii=False,
        )


@mcp.tool()
def sample_data(table_name: str, database: str = "", rows: int = 10) -> str:
    """
    Obtiene una muestra de filas de una tabla en Athena.

    Args:
        table_name: Nombre de la tabla.
        database: Base de datos (usa la default si no se especifica).
        rows: Número de filas (default: 10, max: 100).
    """
    try:
        safe_table = _validate_identifier(table_name, "table_name")
        quoted_table = _quote_identifier(table_name, "table_name")
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    rows = min(max(1, rows), 100)
    db_arg = database if database else None
    try:
        result = execute_query(f"SELECT * FROM {quoted_table} LIMIT {rows}", database=db_arg, max_rows=rows)
        result["table"] = safe_table
        result["sample_size"] = rows
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("sample_data error: %s", type(e).__name__)
        return json.dumps(_safe_error(e), ensure_ascii=False)


@mcp.tool()
def get_query_stats(query_execution_id: str) -> str:
    """
    Retorna estadísticas de ejecución de una query previa en Athena.

    Args:
        query_execution_id: ID de ejecución retornado por query_athena.

    Returns:
        JSON con bytes_scanned, cost_usd, execution_time_ms, state y más.
    """
    try:
        client = _client("athena")
        resp = client.get_query_execution(QueryExecutionId=query_execution_id)
        qe = resp["QueryExecution"]
        stats = qe.get("Statistics", {})
        status = qe.get("Status", {})
        bytes_scanned = stats.get("DataScannedInBytes", 0)
        execution_ms  = stats.get("TotalExecutionTimeInMillis", 0)
        cost_usd = (bytes_scanned / 1_000_000_000_000) * 5.0
        return json.dumps(
            {
                "query_execution_id": query_execution_id,
                "state":             status.get("StateChangeReason", status.get("State", "?")),
                "status":            status.get("State"),
                "bytes_scanned":     bytes_scanned,
                "bytes_scanned_mb":  round(bytes_scanned / (1024 * 1024), 3),
                "cost_usd":          round(cost_usd, 6),
                "execution_time_ms": execution_ms,
                "execution_time_s":  round(execution_ms / 1000, 2),
                "workgroup":         qe.get("WorkGroup"),
                "database":          qe.get("QueryExecutionContext", {}).get("Database"),
                "query_preview":     "[REDACTED]",
            },
            ensure_ascii=False, indent=2,
        )
    except Exception as e:
        logger.error("get_query_stats error: %s", type(e).__name__)
        return json.dumps({**_safe_error(e), "query_execution_id": query_execution_id}, ensure_ascii=False)


@mcp.tool()
def clear_cache() -> str:
    """Limpia el caché en memoria de schemas y tablas."""
    schema_count = len(_schema_cache)
    tables_count = len(_tables_cache)
    _schema_cache.clear()
    _tables_cache.clear()
    return json.dumps(
        {"cleared": True, "schema_entries_removed": schema_count, "table_entries_removed": tables_count},
        ensure_ascii=False, indent=2,
    )


register_tools(mcp)
register_quicksight_tools(mcp)


# ============================================================
# Validación de entorno al arrancar
# ============================================================

def _validate_env() -> list[str]:
    errors = []
    if not AWS_REGION:
        errors.append("AWS_REGION no configurado.")
    if not AWS_ACCESS_KEY_ID:
        errors.append("AWS_ACCESS_KEY_ID no configurado.")
    if not AWS_SECRET_ACCESS_KEY:
        errors.append("AWS_SECRET_ACCESS_KEY no configurado.")
    if not ATHENA_OUTPUT_S3 and ATHENA_WORKGROUP == "primary":
        errors.append(
            "ATHENA_OUTPUT_S3 no definido y workgroup es 'primary'. "
            "Asegúrate de que el workgroup tenga output location configurado."
        )
    return errors


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    errors = _validate_env()
    hard_errors = [e for e in errors if "output location" not in e]
    if hard_errors:
        for err in hard_errors:
            logger.error("Config error: %s", err)
        sys.exit(1)
    for warn in errors:
        if warn not in hard_errors:
            logger.warning("Config warning: %s", warn)

    logger.info("=" * 50)
    logger.info("Iniciando Athena MCP Server v2.2")
    logger.info("  Region:    %s", AWS_REGION)
    logger.info("  Database:  %s", ATHENA_DATABASE or "(no definida — se pasa por tool)")
    logger.info("  Workgroup: %s", ATHENA_WORKGROUP)
    logger.info("  Output S3: %s", ATHENA_OUTPUT_S3 or "(workgroup default)")
    logger.info("  Timeout:   %ds", ATHENA_QUERY_TIMEOUT)
    logger.info("  Cache TTL: %ds", _CACHE_TTL_S)
    logger.info("  SSL Verify Disabled: %s", _SKIP_SSL)
    logger.info("=" * 50)

    mcp.run()
