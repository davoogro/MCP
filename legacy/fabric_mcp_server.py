#!/usr/bin/env python3
"""
Fabric SQL MCP Server
=====================
MCP server para Microsoft Fabric SQL Endpoint vía pyodbc + Azure AD.

Variables de entorno requeridas:
  FABRIC_SERVER        - SQL endpoint de Fabric (*.datawarehouse.fabric.microsoft.com)
  FABRIC_DATABASE      - Nombre de la base de datos
  FABRIC_USER          - Correo corporativo (Azure AD)
  FABRIC_PASSWORD      - Contraseña de Azure AD

Opcionales:
  FABRIC_PORT          - Puerto (default: 1433)
  FABRIC_DRIVER        - ODBC Driver (default: auto-detect)
  FABRIC_READONLY      - Solo lectura (default: true)
  FABRIC_QUERY_TIMEOUT - Timeout por query en seg (default: 120)
  FABRIC_CACHE_TTL     - TTL del cache de queries en seg (default: 0 = off)
  FABRIC_POOL          - Habilitar connection pooling (default: true)

Requiere: mcp>=2.0, pyodbc, python-dotenv

Uso:
  python fabric_mcp_server.py              # ejecutar como MCP server
  python fabric_mcp_server.py --test       # probar conexión
  python fabric_mcp_server.py --handshake  # listar tools vía SDK in-process
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pyodbc
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

try:
    import sqlparse
    HAS_SQLPARSE = True
except ImportError:
    HAS_SQLPARSE = False

from mcp.server import MCPServer

# ============================================================
# Configuración — usa prefijo FABRIC_ en lugar de FABRIC_
# ============================================================

_FABRIC_SQL_SUFFIX = ".datawarehouse.fabric.microsoft.com"


def normalize_fabric_server(server: str) -> str:
    """Normaliza hosts de Fabric pegados desde portales o connection strings."""
    value = (server or "").strip()
    if value.lower().startswith("tcp:"):
        value = value[4:]
    if "," in value:
        value = value.split(",", 1)[0].strip()
    return re.sub(
        rf"(?i)({_FABRIC_SQL_SUFFIX})+$",
        _FABRIC_SQL_SUFFIX,
        value,
    )


@dataclass
class Config:
    server: str = field(default_factory=lambda: os.environ.get("FABRIC_SERVER", ""))
    database: str = field(default_factory=lambda: os.environ.get("FABRIC_DATABASE", "master"))
    port: str = field(default_factory=lambda: os.environ.get("FABRIC_PORT", "1433"))
    username: str = field(default_factory=lambda: os.environ.get("FABRIC_USER", ""))
    password: str = field(default_factory=lambda: os.environ.get("FABRIC_PASSWORD", ""))
    odbc_driver: str = field(default_factory=lambda: os.environ.get("FABRIC_DRIVER", ""))
    readonly: bool = field(
        default_factory=lambda: os.environ.get("FABRIC_READONLY", "true").lower() == "true"
    )
    query_timeout: int = field(
        default_factory=lambda: int(os.environ.get("FABRIC_QUERY_TIMEOUT", "120"))
    )
    cache_ttl: int = field(
        default_factory=lambda: int(os.environ.get("FABRIC_CACHE_TTL", "0"))
    )
    pooling: bool = field(
        default_factory=lambda: os.environ.get("FABRIC_POOL", "true").lower() == "true"
    )

    def validate(self) -> None:
        self.server = normalize_fabric_server(self.server)
        missing = []
        if not self.server:
            missing.append("FABRIC_SERVER")
        if not self.username:
            missing.append("FABRIC_USER")
        if not self.password:
            missing.append("FABRIC_PASSWORD")
        if missing:
            raise RuntimeError(f"Variables de entorno faltantes: {', '.join(missing)}")


CFG = Config()

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fabric-mcp")

pyodbc.pooling = CFG.pooling

# ============================================================
# Detección de ODBC Driver
# ============================================================


def get_odbc_driver() -> str:
    if CFG.odbc_driver:
        return CFG.odbc_driver
    try:
        drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        if not drivers:
            raise RuntimeError(
                "No se encontró ODBC Driver para SQL Server. "
                "Instala 'ODBC Driver 18 for SQL Server' desde: "
                "https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server"
            )
        driver = sorted(drivers, reverse=True)[0]
        logger.info(f"ODBC Driver detectado: {driver}")
        return driver
    except Exception as e:
        logger.error(f"Error detectando ODBC drivers: {e}")
        return "ODBC Driver 18 for SQL Server"


def build_connection_string() -> str:
    driver = get_odbc_driver()
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={CFG.server};"
        f"DATABASE={CFG.database};"
        f"UID={CFG.username};"
        f"PWD={CFG.password};"
        f"Authentication=ActiveDirectoryPassword;"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"LoginTimeout=60;"
        f"ConnectRetryCount=2;"
        f"ConnectRetryInterval=5;"
    )
    safe_str = conn_str.replace(CFG.password, "***") if CFG.password else conn_str
    logger.debug(f"Connection string: {safe_str}")
    return conn_str


@contextmanager
def get_connection():
    conn_str = build_connection_string()
    conn = None
    try:
        conn = pyodbc.connect(conn_str)
        conn.timeout = CFG.query_timeout
        yield conn
    except pyodbc.Error as e:
        logger.error(f"Error abriendo conexión a Fabric: {e}")
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception as close_err:
                logger.debug(f"Error cerrando conexión: {close_err}")


# ============================================================
# Serialización de tipos
# ============================================================


def serialize_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float, str)):
        return val
    if isinstance(val, Decimal):
        try:
            return float(val)
        except Exception:
            return str(val)
    if isinstance(val, (datetime, date, dtime)):
        return val.isoformat()
    if isinstance(val, timedelta):
        return val.total_seconds()
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    return str(val)


# ============================================================
# Validación read-only
# ============================================================

_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "DROP", "CREATE", "ALTER", "GRANT", "REVOKE", "DENY",
    "EXEC", "EXECUTE", "CALL",
    "BACKUP", "RESTORE",
    "BULK", "OPENROWSET", "OPENDATASOURCE",
}

_ALLOWED_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE")
_ALLOWED_EXEC_SPS = {"SP_HELP", "SP_COLUMNS", "SP_TABLES", "SP_HELPTEXT"}


def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_readonly(sql: str) -> Optional[str]:
    if not sql or not sql.strip():
        return "Query vacío"

    cleaned = strip_sql_comments(sql).strip()
    upper = cleaned.upper()

    statements_part = upper.rstrip().rstrip(";")
    if ";" in statements_part:
        return "No se permiten múltiples statements separados por ';' en modo readonly"

    starts_ok = any(upper.startswith(p) for p in _ALLOWED_PREFIXES)
    starts_with_exec = upper.startswith("EXEC ") or upper.startswith("EXECUTE ")

    if starts_with_exec:
        match = re.match(r"^(EXEC|EXECUTE)\s+(\[?\w+\]?\.?)*(\w+)", upper)
        if not match:
            return "Formato EXEC no reconocido"
        proc_name = match.group(3)
        if proc_name not in _ALLOWED_EXEC_SPS:
            return (
                f"EXEC {proc_name} no está permitido en modo readonly. "
                f"Permitidos: {sorted(_ALLOWED_EXEC_SPS)}"
            )
    elif not starts_ok:
        return (
            f"Solo se permiten queries que empiecen con: {_ALLOWED_PREFIXES} o EXEC <sp_*>. "
            f"Recibido: '{upper.split()[0] if upper.split() else ''}'"
        )

    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            if kw in ("EXEC", "EXECUTE") and starts_with_exec:
                continue
            return f"Palabra clave '{kw}' no permitida en modo readonly"

    if HAS_SQLPARSE:
        parsed = sqlparse.parse(sql)
        non_empty = [s for s in parsed if s.tokens and str(s).strip()]
        if len(non_empty) > 1:
            return "sqlparse detectó múltiples statements; en readonly solo se permite uno"

    return None


# ============================================================
# Cache de queries
# ============================================================


class QueryCache:
    def __init__(self, ttl_seconds: int = 0):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, dict]] = {}

    def _key(self, sql: str, max_rows: int, page: int) -> str:
        return hashlib.sha256(f"{sql}|{max_rows}|{page}".encode()).hexdigest()

    def get(self, sql: str, max_rows: int, page: int) -> Optional[dict]:
        if self.ttl <= 0:
            return None
        key = self._key(sql, max_rows, page)
        entry = self._store.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, sql: str, max_rows: int, page: int, value: dict) -> None:
        if self.ttl <= 0:
            return
        key = self._key(sql, max_rows, page)
        self._store[key] = (time.time(), value)


CACHE = QueryCache(ttl_seconds=CFG.cache_ttl)

# ============================================================
# Stats store
# ============================================================

_STATS_STORE: dict[str, dict] = {}
_STATS_ORDER: list[str] = []
_STATS_MAX = 100


def store_stats(qid: str, stats: dict) -> None:
    _STATS_STORE[qid] = stats
    _STATS_ORDER.append(qid)
    while len(_STATS_ORDER) > _STATS_MAX:
        old = _STATS_ORDER.pop(0)
        _STATS_STORE.pop(old, None)


# ============================================================
# Ejecutor principal
# ============================================================


def execute_query(sql: str, max_rows: int = 100, page: int = 1, skip_cache: bool = False) -> dict:
    max_rows = max(1, min(int(max_rows), 1000))
    page = max(1, int(page))

    if not skip_cache:
        cached = CACHE.get(sql, max_rows, page)
        if cached:
            cached_copy = dict(cached)
            cached_copy["from_cache"] = True
            return cached_copy

    qid = str(uuid.uuid4())
    started = time.time()

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)

        if cursor.description is None:
            elapsed = time.time() - started
            rows_affected = cursor.rowcount
            try:
                conn.commit()
            except Exception:
                pass
            stats = {
                "query_execution_id": qid,
                "state": "SUCCEEDED",
                "execution_time_ms": int(elapsed * 1000),
                "execution_time_s": round(elapsed, 3),
                "rows_affected": rows_affected,
                "row_count": 0,
                "type": "execute",
                "query_preview": sql[:200],
                "database": CFG.database,
            }
            store_stats(qid, stats)
            logger.info(f"[{qid[:8]}] EXECUTE ok ({elapsed:.2f}s, rows_affected={rows_affected}): {sql[:80]}")
            return {
                "type": "execute",
                "rows_affected": rows_affected,
                "message": f"{rows_affected} filas afectadas",
                "query_execution_id": qid,
            }

        columns = [col[0] for col in cursor.description]

        offset = (page - 1) * max_rows
        if offset > 0:
            skipped = 0
            while skipped < offset:
                chunk = cursor.fetchmany(min(1000, offset - skipped))
                if not chunk:
                    break
                skipped += len(chunk)

        rows = cursor.fetchmany(max_rows)
        peek = cursor.fetchone()
        has_more = peek is not None

        result_rows = [
            {col: serialize_value(val) for col, val in zip(columns, row)} for row in rows
        ]

        elapsed = time.time() - started
        stats = {
            "query_execution_id": qid,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "execution_time_s": round(elapsed, 3),
            "row_count": len(result_rows),
            "page": page,
            "max_rows": max_rows,
            "has_more": has_more,
            "type": "query",
            "query_preview": sql[:200],
            "database": CFG.database,
        }
        store_stats(qid, stats)
        logger.info(
            f"[{qid[:8]}] QUERY ok ({elapsed:.2f}s, rows={len(result_rows)}, "
            f"page={page}, has_more={has_more}): {sql[:80]}"
        )

        result = {
            "type": "query",
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "page": page,
            "max_rows": max_rows,
            "has_more": has_more,
            "query_execution_id": qid,
        }

        if not skip_cache:
            CACHE.set(sql, max_rows, page, result)

        return result


def execute_parametrized(sql: str, params: tuple, max_rows: int = 500) -> dict:
    qid = str(uuid.uuid4())
    started = time.time()

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)

        if cursor.description is None:
            return {"type": "execute", "rows_affected": cursor.rowcount, "query_execution_id": qid}

        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchmany(max_rows)
        peek = cursor.fetchone()
        has_more = peek is not None

        result_rows = [
            {col: serialize_value(val) for col, val in zip(columns, row)} for row in rows
        ]
        elapsed = time.time() - started

        stats = {
            "query_execution_id": qid,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "execution_time_s": round(elapsed, 3),
            "row_count": len(result_rows),
            "type": "query",
            "query_preview": sql[:200],
            "database": CFG.database,
        }
        store_stats(qid, stats)
        logger.info(f"[{qid[:8]}] METADATA ok ({elapsed:.2f}s, rows={len(result_rows)}): {sql[:80]}")

        return {
            "type": "query",
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "has_more": has_more,
            "query_execution_id": qid,
        }


# ============================================================
# MCP Server + Tools
# ============================================================

mcp = MCPServer("Fabric SQL Server", version="2.0.0")


def _err(msg: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **extra}


def _ok(payload: dict) -> dict[str, Any]:
    if "ok" not in payload:
        payload = {"ok": True, **payload}
    return payload


@mcp.tool()
def query(sql: str, max_rows: int = 100, page: int = 1) -> dict[str, Any]:
    """
    Ejecuta un query SQL contra Microsoft Fabric SQL Endpoint.

    Args:
        sql: Query SQL a ejecutar. Si READONLY=true, solo SELECT/WITH/SHOW/DESCRIBE
             y stored procedures whitelisted (sp_help, sp_columns, sp_tables, sp_helptext).
        max_rows: Máximo de filas por página (default: 100, max: 1000).
        page: Número de página (default: 1). Usa has_more para iterar.

    Returns:
        JSON con columnas, filas, page, has_more y query_execution_id.
    """
    if CFG.readonly:
        err = validate_readonly(sql)
        if err:
            return _err(err, hint="Para habilitar escritura, configura FABRIC_READONLY=false")

    try:
        result = execute_query(sql, max_rows=max_rows, page=page)
        return _ok(result)
    except pyodbc.Error as e:
        logger.exception("Error pyodbc en query")
        return _err(f"pyodbc.Error: {e}", sql=sql[:200])
    except Exception as e:
        logger.exception("Error inesperado en query")
        return _err(str(e), sql=sql[:200])


@mcp.tool()
def get_query_stats(query_execution_id: str) -> dict[str, Any]:
    """
    Retorna estadísticas de ejecución de una query previa.

    Args:
        query_execution_id: ID retornado por query() u otra tool.

    Returns:
        JSON con execution_time_ms, row_count, state y más.
    """
    stats = _STATS_STORE.get(query_execution_id)
    if not stats:
        return _err(
            f"No se encontró query_execution_id={query_execution_id}",
            hint=f"Solo se conservan las últimas {_STATS_MAX} queries.",
        )
    return _ok(stats)


@mcp.tool()
def list_tables(schema: str = "dbo") -> dict[str, Any]:
    """
    Lista todas las tablas disponibles en un esquema.

    Args:
        schema: Esquema a consultar (default: dbo).

    Returns:
        JSON con esquemas, nombres y tipos de tabla.
    """
    sql = """
    SELECT
        TABLE_SCHEMA AS [schema],
        TABLE_NAME   AS [table_name],
        TABLE_TYPE   AS [type]
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = ?
    ORDER BY TABLE_SCHEMA, TABLE_NAME
    """
    try:
        return _ok(execute_parametrized(sql, (schema,), max_rows=1000))
    except Exception as e:
        logger.exception("Error en list_tables")
        return _err(str(e))


@mcp.tool()
def list_schemas(include_system: bool = False) -> dict[str, Any]:
    """
    Lista todos los esquemas disponibles. Por defecto excluye los del sistema.

    Args:
        include_system: Si True, incluye esquemas sys, INFORMATION_SCHEMA, db_*, etc.

    Returns:
        JSON con la lista de esquemas.
    """
    if include_system:
        sql = "SELECT SCHEMA_NAME AS [schema] FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME"
        params: tuple = ()
    else:
        sql = """
        SELECT SCHEMA_NAME AS [schema]
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE SCHEMA_NAME NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest',
                                  'db_owner', 'db_accessadmin', 'db_securityadmin',
                                  'db_ddladmin', 'db_backupoperator', 'db_datareader',
                                  'db_datawriter', 'db_denydatareader', 'db_denydatawriter')
        ORDER BY SCHEMA_NAME
        """
        params = ()
    try:
        return _ok(execute_parametrized(sql, params, max_rows=500))
    except Exception as e:
        logger.exception("Error en list_schemas")
        return _err(str(e))


@mcp.tool()
def describe_table(table_name: str, schema: str = "dbo") -> dict[str, Any]:
    """
    Describe la estructura de una tabla (columnas, tipos, nullability).

    Args:
        table_name: Nombre de la tabla.
        schema: Esquema (default: dbo).

    Returns:
        JSON con columnas, tipos de datos y restricciones.
    """
    sql = """
    SELECT
        c.COLUMN_NAME              AS [column],
        c.DATA_TYPE                AS [type],
        c.CHARACTER_MAXIMUM_LENGTH AS [max_length],
        c.NUMERIC_PRECISION        AS [precision],
        c.NUMERIC_SCALE            AS [scale],
        c.IS_NULLABLE              AS [nullable],
        c.COLUMN_DEFAULT           AS [default_value],
        c.ORDINAL_POSITION         AS [position]
    FROM INFORMATION_SCHEMA.COLUMNS c
    WHERE c.TABLE_NAME = ?
      AND c.TABLE_SCHEMA = ?
    ORDER BY c.ORDINAL_POSITION
    """
    try:
        result = execute_parametrized(sql, (table_name, schema), max_rows=500)
        result["table"] = f"{schema}.{table_name}"
        return _ok(result)
    except Exception as e:
        logger.exception("Error en describe_table")
        return _err(str(e))


def _safe_ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$", name):
        raise ValueError(f"Identificador inválido: {name!r}")
    return f"[{name}]"


@mcp.tool()
def sample_data(table_name: str, schema: str = "dbo", rows: int = 10) -> dict[str, Any]:
    """
    Obtiene una muestra de filas de una tabla.

    Args:
        table_name: Nombre de la tabla.
        schema: Esquema (default: dbo).
        rows: Número de filas (default: 10, max: 100).

    Returns:
        JSON con la muestra de datos.
    """
    rows = max(1, min(int(rows), 100))
    try:
        s_ident = _safe_ident(schema)
        t_ident = _safe_ident(table_name)
        sql = f"SELECT TOP {rows} * FROM {s_ident}.{t_ident}"
        result = execute_query(sql, max_rows=rows, page=1, skip_cache=True)
        result["table"] = f"{schema}.{table_name}"
        result["sample_size"] = rows
        return _ok(result)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en sample_data")
        return _err(str(e))


@mcp.tool()
def table_row_count(table_name: str, schema: str = "dbo") -> dict[str, Any]:
    """
    Obtiene el conteo aproximado de filas de una tabla (vía sys.partitions).

    Args:
        table_name: Nombre de la tabla.
        schema: Esquema (default: dbo).

    Returns:
        JSON con el conteo de filas.
    """
    sql = """
    SELECT
        s.name AS [schema],
        t.name AS [table],
        SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END) AS [row_count]
    FROM sys.tables t
    INNER JOIN sys.schemas s    ON t.schema_id = s.schema_id
    INNER JOIN sys.partitions p ON t.object_id = p.object_id
    WHERE t.name = ? AND s.name = ?
    GROUP BY s.name, t.name
    """
    try:
        return _ok(execute_parametrized(sql, (table_name, schema), max_rows=10))
    except Exception as e:
        logger.exception("Error en table_row_count")
        return _err(str(e))


@mcp.tool()
def search_columns(pattern: str, schema: Optional[str] = None) -> dict[str, Any]:
    """
    Busca columnas por nombre en todas las tablas (usa LIKE).

    Args:
        pattern: Patrón de búsqueda (ej. '%user%', 'email%').
        schema: Filtrar por esquema (opcional).

    Returns:
        JSON con tablas y columnas que coinciden.
    """
    if "%" not in pattern and "_" not in pattern:
        pattern = f"%{pattern}%"

    if schema:
        sql = """
        SELECT
            TABLE_SCHEMA AS [schema],
            TABLE_NAME   AS [table],
            COLUMN_NAME  AS [column],
            DATA_TYPE    AS [type]
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME LIKE ? AND TABLE_SCHEMA = ?
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """
        params: tuple = (pattern, schema)
    else:
        sql = """
        SELECT
            TABLE_SCHEMA AS [schema],
            TABLE_NAME   AS [table],
            COLUMN_NAME  AS [column],
            DATA_TYPE    AS [type]
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME LIKE ?
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
        """
        params = (pattern,)

    try:
        result = execute_parametrized(sql, params, max_rows=1000)
        result["pattern"] = pattern
        return _ok(result)
    except Exception as e:
        logger.exception("Error en search_columns")
        return _err(str(e))


@mcp.tool()
def explain_query(sql: str) -> dict[str, Any]:
    """
    Obtiene el plan de ejecución estimado de un query (SHOWPLAN_XML).

    Args:
        sql: Query a analizar (sin ejecutarlo realmente).

    Returns:
        JSON con el plan XML estimado.
    """
    if CFG.readonly:
        err = validate_readonly(sql)
        if err:
            return _err(err)

    qid = str(uuid.uuid4())
    started = time.time()
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET SHOWPLAN_XML ON")
            try:
                cursor.execute(sql)
                rows = cursor.fetchall()
                plans = [serialize_value(r[0]) for r in rows] if rows else []
            finally:
                cursor.execute("SET SHOWPLAN_XML OFF")

        elapsed = time.time() - started
        stats = {
            "query_execution_id": qid,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "type": "explain",
            "query_preview": sql[:200],
        }
        store_stats(qid, stats)
        return _ok({
            "query_execution_id": qid,
            "type": "explain",
            "plan_count": len(plans),
            "plans": plans,
        })
    except Exception as e:
        logger.exception("Error en explain_query")
        return _err(str(e), sql=sql[:200])


@mcp.tool()
def test_connection() -> dict[str, Any]:
    """
    Prueba la conexión al endpoint de Fabric.

    Returns:
        JSON con estado, usuario autenticado, versión del servidor y tiempo.
    """
    sql = """
    SELECT
        SYSTEM_USER AS [authenticated_user],
        DB_NAME()   AS [database],
        @@VERSION   AS [server_version],
        GETDATE()   AS [server_time]
    """
    try:
        result = execute_query(sql, max_rows=1, skip_cache=True)
        result["status"] = "connected"
        result["server"] = CFG.server
        result["auth_method"] = "ActiveDirectoryPassword"
        result["readonly"] = CFG.readonly
        result["pooling"] = CFG.pooling
        result["cache_ttl"] = CFG.cache_ttl
        result["instance"] = "fabric-sql"
        return _ok(result)
    except Exception as e:
        logger.exception("Error en test_connection")
        return _err(
            str(e),
            status="error",
            server=CFG.server,
            database=CFG.database,
            instance="fabric-sql",
        )


# ============================================================
# Entry point + modo CLI
# ============================================================


def cli_test() -> int:
    try:
        CFG.validate()
    except Exception as e:
        print(f"ERROR: Config invalida: {e}", file=sys.stderr)
        return 2

    print(f"Probando conexión a {CFG.server} / db={CFG.database} como {CFG.username}...")
    output = test_connection()
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    if output.get("status") == "connected" and output.get("ok") is not False:
        print("OK: Conexion OK")
        return 0
    print("ERROR: Conexion fallo")
    return 1


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        tools_result = await client.list_tools()
        names = sorted(t.name for t in tools_result.tools)
        print(json.dumps({"ok": True, "tool_count": len(names), "tools": names}, indent=2))
        if not names:
            return 1
        ping = await client.call_tool("test_connection", {})
        sc = ping.structured_content
        print("test_connection:", json.dumps(sc, ensure_ascii=False, indent=2, default=str)[:2000])
        if isinstance(sc, dict) and sc.get("status") == "connected":
            return 0
        return 1


def cli_handshake() -> int:
    try:
        CFG.validate()
    except Exception as e:
        print(f"ERROR: Config invalida: {e}", file=sys.stderr)
        return 2
    print("Handshake MCP in-process...")
    return asyncio.run(_handshake())


def main() -> int:
    parser = argparse.ArgumentParser(description="Fabric SQL MCP Server")
    parser.add_argument("--test", action="store_true", help="Prueba la conexión y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía SDK y sale")
    args = parser.parse_args()

    if args.test:
        return cli_test()
    if args.handshake:
        return cli_handshake()

    try:
        CFG.validate()
    except Exception as e:
        logger.error(str(e))
        return 1

    logger.info("Iniciando Fabric SQL MCP Server")
    logger.info(f"  Server:        {CFG.server}")
    logger.info(f"  Database:      {CFG.database}")
    logger.info(f"  User:          {CFG.username}")
    logger.info(f"  Readonly:      {CFG.readonly}")
    logger.info(f"  Pooling:       {CFG.pooling}")
    logger.info(f"  Query timeout: {CFG.query_timeout}s")
    logger.info(f"  Cache TTL:     {CFG.cache_ttl}s")
    logger.info(f"  sqlparse:      {'sí' if HAS_SQLPARSE else 'no (recomendado: pip install sqlparse)'}")

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
