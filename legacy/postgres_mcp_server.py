#!/usr/bin/env python3
"""
PostgreSQL MCP Server
=====================
MCP server multi-instancia para PostgreSQL vía psycopg2.

Variables de entorno:
  PG_PASSWORD            - Password compartido (requerido)
  PG_USER                - Usuario compartido (default: postgres)
  PG_CONNECTIONS_FILE    - Ruta al JSON de conexiones (default: postgres_connections.json)
  PG_READONLY            - Solo lectura (default: true)
  PG_QUERY_TIMEOUT       - Timeout por query en seg (default: 120)
  PG_CACHE_TTL             - TTL del cache de queries en seg (default: 0 = off)
  PG_SSLMODE               - prefer | require | disable (default: prefer)

Archivo de conexiones (postgres_connections.json):
  {
    "connections": {
      "mi_app": {
        "host": "10.0.0.5",
        "port": 5432,
        "database": "mydb",
        "description": "opcional"
      }
    }
  }

Uso:
  python postgres_mcp_server.py              # MCP server
  python postgres_mcp_server.py --test       # probar todas las conexiones
  python postgres_mcp_server.py --test mi_app  # probar una conexión
"""

import argparse
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

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from psycopg2 import sql

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

try:
    import sqlparse
    HAS_SQLPARSE = True
except ImportError:
    HAS_SQLPARSE = False

from mcp.server.fastmcp import FastMCP

# ============================================================
# Configuración global
# ============================================================


@dataclass
class GlobalConfig:
    user: str = field(default_factory=lambda: os.environ.get("PG_USER", "postgres"))
    password: str = field(default_factory=lambda: os.environ.get("PG_PASSWORD", ""))
    connections_file: str = field(
        default_factory=lambda: os.environ.get(
            "PG_CONNECTIONS_FILE",
            str(BASE_DIR / "postgres_connections.json"),
        )
    )
    readonly: bool = field(
        default_factory=lambda: os.environ.get("PG_READONLY", "true").lower() == "true"
    )
    query_timeout: int = field(
        default_factory=lambda: int(os.environ.get("PG_QUERY_TIMEOUT", "120"))
    )
    cache_ttl: int = field(
        default_factory=lambda: int(os.environ.get("PG_CACHE_TTL", "0"))
    )
    sslmode: str = field(default_factory=lambda: os.environ.get("PG_SSLMODE", "prefer"))

    def validate(self) -> None:
        if not self.password:
            raise RuntimeError("Variable de entorno faltante: PG_PASSWORD")
        path = Path(self.connections_file)
        if not path.is_file():
            raise RuntimeError(
                f"Archivo de conexiones no encontrado: {path}. "
                f"Copia postgres_connections.example.json → postgres_connections.json"
            )


@dataclass
class PgConnection:
    name: str
    host: str
    database: str
    port: int = 5432
    user: Optional[str] = None
    password: Optional[str] = None
    description: str = ""

    @property
    def effective_user(self) -> str:
        return self.user or CFG.user

    @property
    def effective_password(self) -> str:
        return self.password or CFG.password


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, PgConnection] = {}

    def load(self, path: str) -> None:
        file_path = Path(path)
        with file_path.open(encoding="utf-8") as f:
            data = json.load(f)

        raw = data.get("connections", data)
        if not isinstance(raw, dict) or not raw:
            raise RuntimeError(
                f"Formato inválido en {file_path}: se espera objeto 'connections' con al menos una entrada"
            )

        self._connections.clear()
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                raise RuntimeError(f"Conexión '{name}': debe ser un objeto JSON")
            host = (entry.get("host") or "").strip()
            database = (entry.get("database") or "").strip()
            if not host:
                raise RuntimeError(f"Conexión '{name}': falta 'host'")
            if not database:
                raise RuntimeError(f"Conexión '{name}': falta 'database'")
            self._connections[name] = PgConnection(
                name=name,
                host=host,
                database=database,
                port=int(entry.get("port", 5432)),
                user=entry.get("user"),
                password=entry.get("password"),
                description=(entry.get("description") or "").strip(),
            )

    def get(self, name: str) -> PgConnection:
        conn = self._connections.get(name)
        if not conn:
            available = ", ".join(sorted(self._connections))
            raise KeyError(
                f"Conexión '{name}' no encontrada. Disponibles: {available or '(ninguna)'}"
            )
        return conn

    def names(self) -> list[str]:
        return sorted(self._connections)

    def all(self) -> list[PgConnection]:
        return [self._connections[n] for n in self.names()]


CFG = GlobalConfig()
REGISTRY = ConnectionRegistry()

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("postgres-mcp")

# ============================================================
# Conexión y serialización
# ============================================================


@contextmanager
def get_connection(connection_name: str):
    conn_cfg = REGISTRY.get(connection_name)
    conn = None
    try:
        conn = psycopg2.connect(
            host=conn_cfg.host,
            port=conn_cfg.port,
            dbname=conn_cfg.database,
            user=conn_cfg.effective_user,
            password=conn_cfg.effective_password,
            connect_timeout=30,
            sslmode=CFG.sslmode,
            options=f"-c statement_timeout={CFG.query_timeout * 1000}",
        )
        yield conn, conn_cfg
    except psycopg2.Error as e:
        logger.error(f"Error conectando a '{connection_name}' ({conn_cfg.host}/{conn_cfg.database}): {e}")
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception as close_err:
                logger.debug(f"Error cerrando conexión: {close_err}")


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
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val).hex()
    if isinstance(val, list):
        return [serialize_value(v) for v in val]
    if isinstance(val, dict):
        return {k: serialize_value(v) for k, v in val.items()}
    return str(val)


# ============================================================
# Validación read-only
# ============================================================

_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "DROP", "CREATE", "ALTER", "GRANT", "REVOKE",
    "COPY", "VACUUM", "REINDEX", "CLUSTER",
    "CALL", "DO", "LOCK",
}

_ALLOWED_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN", "TABLE")


def strip_sql_comments(sql_text: str) -> str:
    sql_text = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.DOTALL)
    sql_text = re.sub(r"--[^\n]*", " ", sql_text)
    return sql_text


def validate_readonly(sql_text: str) -> Optional[str]:
    if not sql_text or not sql_text.strip():
        return "Query vacío"

    cleaned = strip_sql_comments(sql_text).strip()
    upper = cleaned.upper()

    statements_part = upper.rstrip().rstrip(";")
    if ";" in statements_part:
        return "No se permiten múltiples statements separados por ';' en modo readonly"

    if not any(upper.startswith(p) for p in _ALLOWED_PREFIXES):
        first = upper.split()[0] if upper.split() else ""
        return (
            f"Solo se permiten queries que empiecen con: {_ALLOWED_PREFIXES}. "
            f"Recibido: '{first}'"
        )

    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            if kw == "COPY" and upper.startswith("EXPLAIN"):
                continue
            return f"Palabra clave '{kw}' no permitida en modo readonly"

    if HAS_SQLPARSE:
        parsed = sqlparse.parse(sql_text)
        non_empty = [s for s in parsed if s.tokens and str(s).strip()]
        if len(non_empty) > 1:
            return "sqlparse detectó múltiples statements; en readonly solo se permite uno"

    return None


# ============================================================
# Cache y stats
# ============================================================


class QueryCache:
    def __init__(self, ttl_seconds: int = 0):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, dict]] = {}

    def _key(self, connection: str, sql_text: str, max_rows: int, page: int) -> str:
        raw = f"{connection}|{sql_text}|{max_rows}|{page}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, connection: str, sql_text: str, max_rows: int, page: int) -> Optional[dict]:
        if self.ttl <= 0:
            return None
        key = self._key(connection, sql_text, max_rows, page)
        entry = self._store.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, connection: str, sql_text: str, max_rows: int, page: int, value: dict) -> None:
        if self.ttl <= 0:
            return
        key = self._key(connection, sql_text, max_rows, page)
        self._store[key] = (time.time(), value)


CACHE = QueryCache(ttl_seconds=CFG.cache_ttl)
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
# Ejecutor
# ============================================================


def execute_query(
    connection: str,
    sql_text: str,
    max_rows: int = 100,
    page: int = 1,
    skip_cache: bool = False,
) -> dict:
    max_rows = max(1, min(int(max_rows), 1000))
    page = max(1, int(page))

    if not skip_cache:
        cached = CACHE.get(connection, sql_text, max_rows, page)
        if cached:
            cached_copy = dict(cached)
            cached_copy["from_cache"] = True
            return cached_copy

    qid = str(uuid.uuid4())
    started = time.time()
    conn_cfg = REGISTRY.get(connection)

    with get_connection(connection) as (conn, _):
        with conn.cursor() as cursor:
            cursor.execute(sql_text)

            if cursor.description is None:
                elapsed = time.time() - started
                rows_affected = cursor.rowcount
                conn.commit()
                stats = {
                    "query_execution_id": qid,
                    "connection": connection,
                    "state": "SUCCEEDED",
                    "execution_time_ms": int(elapsed * 1000),
                    "execution_time_s": round(elapsed, 3),
                    "rows_affected": rows_affected,
                    "row_count": 0,
                    "type": "execute",
                    "query_preview": sql_text[:200],
                    "database": conn_cfg.database,
                    "host": conn_cfg.host,
                }
                store_stats(qid, stats)
                return {
                    "type": "execute",
                    "connection": connection,
                    "rows_affected": rows_affected,
                    "message": f"{rows_affected} filas afectadas",
                    "query_execution_id": qid,
                }

            columns = [desc[0] for desc in cursor.description]
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
            "connection": connection,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "execution_time_s": round(elapsed, 3),
            "row_count": len(result_rows),
            "page": page,
            "max_rows": max_rows,
            "has_more": has_more,
            "type": "query",
            "query_preview": sql_text[:200],
            "database": conn_cfg.database,
            "host": conn_cfg.host,
        }
        store_stats(qid, stats)

        result = {
            "type": "query",
            "connection": connection,
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "page": page,
            "max_rows": max_rows,
            "has_more": has_more,
            "query_execution_id": qid,
        }
        if not skip_cache:
            CACHE.set(connection, sql_text, max_rows, page, result)
        return result


def execute_parametrized(
    connection: str,
    sql_text: str,
    params: tuple | list,
    max_rows: int = 500,
) -> dict:
    qid = str(uuid.uuid4())
    started = time.time()
    conn_cfg = REGISTRY.get(connection)

    with get_connection(connection) as (conn, _):
        with conn.cursor() as cursor:
            cursor.execute(sql_text, params)

            if cursor.description is None:
                return {
                    "type": "execute",
                    "connection": connection,
                    "rows_affected": cursor.rowcount,
                    "query_execution_id": qid,
                }

            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(max_rows)
            peek = cursor.fetchone()
            has_more = peek is not None
            result_rows = [
                {col: serialize_value(val) for col, val in zip(columns, row)} for row in rows
            ]

        elapsed = time.time() - started
        stats = {
            "query_execution_id": qid,
            "connection": connection,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "execution_time_s": round(elapsed, 3),
            "row_count": len(result_rows),
            "type": "query",
            "query_preview": sql_text[:200],
            "database": conn_cfg.database,
            "host": conn_cfg.host,
        }
        store_stats(qid, stats)

        return {
            "type": "query",
            "connection": connection,
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "has_more": has_more,
            "query_execution_id": qid,
        }


def resolve_connection(connection: str) -> PgConnection:
    return REGISTRY.get(connection)


# ============================================================
# MCP Server + Tools
# ============================================================

mcp = FastMCP("PostgreSQL Server")


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra}, ensure_ascii=False)


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def list_connections() -> str:
    """
    Lista todas las conexiones PostgreSQL configuradas.

    Returns:
        JSON con nombres, host, database y descripción de cada conexión.
    """
    items = []
    for conn in REGISTRY.all():
        items.append({
            "connection": conn.name,
            "host": conn.host,
            "port": conn.port,
            "database": conn.database,
            "user": conn.effective_user,
            "description": conn.description or None,
        })
    return _ok({"connections": items, "count": len(items)})


@mcp.tool()
def query(connection: str, sql: str, max_rows: int = 100, page: int = 1) -> str:
    """
    Ejecuta un query SQL contra una conexión PostgreSQL.

    Args:
        connection: Nombre de la conexión (ver list_connections).
        sql: Query SQL. Si READONLY=true, solo SELECT/WITH/EXPLAIN/SHOW/TABLE.
        max_rows: Máximo de filas por página (default: 100, max: 1000).
        page: Número de página (default: 1). Usa has_more para iterar.

    Returns:
        JSON con columnas, filas, page, has_more y query_execution_id.
    """
    try:
        resolve_connection(connection)
    except KeyError as e:
        return _err(str(e))

    if CFG.readonly:
        err = validate_readonly(sql)
        if err:
            return _err(err, hint="Para habilitar escritura, configura PG_READONLY=false")

    try:
        result = execute_query(connection, sql, max_rows=max_rows, page=page)
        return _ok(result)
    except psycopg2.Error as e:
        logger.exception("Error psycopg2 en query")
        return _err(f"psycopg2.Error: {e}", connection=connection, sql=sql[:200])
    except Exception as e:
        logger.exception("Error inesperado en query")
        return _err(str(e), connection=connection, sql=sql[:200])


@mcp.tool()
def get_query_stats(query_execution_id: str) -> str:
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
def list_tables(connection: str, schema: str = "public") -> str:
    """
    Lista tablas y vistas de un esquema.

    Args:
        connection: Nombre de la conexión.
        schema: Esquema (default: public).

    Returns:
        JSON con esquemas, nombres y tipos.
    """
    sql_text = """
    SELECT
        table_schema AS schema,
        table_name,
        table_type AS type
    FROM information_schema.tables
    WHERE table_schema = %s
    ORDER BY table_schema, table_name
    """
    try:
        resolve_connection(connection)
        return _ok(execute_parametrized(connection, sql_text, (schema,), max_rows=2000))
    except KeyError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en list_tables")
        return _err(str(e), connection=connection)


@mcp.tool()
def list_schemas(connection: str, include_system: bool = False) -> str:
    """
    Lista esquemas disponibles. Por defecto excluye los del sistema.

    Args:
        connection: Nombre de la conexión.
        include_system: Si True, incluye pg_catalog, information_schema, etc.

    Returns:
        JSON con la lista de esquemas.
    """
    if include_system:
        sql_text = """
        SELECT schema_name AS schema
        FROM information_schema.schemata
        ORDER BY schema_name
        """
        params: tuple = ()
    else:
        sql_text = """
        SELECT schema_name AS schema
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
          AND schema_name NOT LIKE 'pg\\_%%' ESCAPE '\\'
        ORDER BY schema_name
        """
        params = ()

    try:
        resolve_connection(connection)
        return _ok(execute_parametrized(connection, sql_text, params, max_rows=500))
    except KeyError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en list_schemas")
        return _err(str(e), connection=connection)


@mcp.tool()
def describe_table(connection: str, table_name: str, schema: str = "public") -> str:
    """
    Describe columnas de una tabla.

    Args:
        connection: Nombre de la conexión.
        table_name: Nombre de la tabla.
        schema: Esquema (default: public).

    Returns:
        JSON con columnas, tipos y nullability.
    """
    sql_text = """
    SELECT
        c.column_name AS column,
        c.data_type AS type,
        c.character_maximum_length AS max_length,
        c.numeric_precision AS precision,
        c.numeric_scale AS scale,
        c.is_nullable AS nullable,
        c.column_default AS default_value,
        c.ordinal_position AS position
    FROM information_schema.columns c
    WHERE c.table_name = %s
      AND c.table_schema = %s
    ORDER BY c.ordinal_position
    """
    try:
        resolve_connection(connection)
        result = execute_parametrized(connection, sql_text, (table_name, schema), max_rows=500)
        result["table"] = f"{schema}.{table_name}"
        return _ok(result)
    except KeyError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en describe_table")
        return _err(str(e), connection=connection)


@mcp.tool()
def sample_data(connection: str, table_name: str, schema: str = "public", rows: int = 10) -> str:
    """
    Obtiene una muestra de filas de una tabla.

    Args:
        connection: Nombre de la conexión.
        table_name: Nombre de la tabla.
        schema: Esquema (default: public).
        rows: Número de filas (default: 10, max: 100).

    Returns:
        JSON con la muestra de datos.
    """
    rows = max(1, min(int(rows), 100))
    try:
        resolve_connection(connection)
        qid = str(uuid.uuid4())
        started = time.time()
        query_sql = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
            sql.Identifier(schema),
            sql.Identifier(table_name),
        )
        with get_connection(connection) as (conn, conn_cfg):
            with conn.cursor() as cursor:
                cursor.execute(query_sql, (rows,))
                columns = [desc[0] for desc in cursor.description]
                fetched = cursor.fetchall()
        result_rows = [
            {col: serialize_value(val) for col, val in zip(columns, row)} for row in fetched
        ]
        elapsed = time.time() - started
        result = {
            "type": "query",
            "connection": connection,
            "columns": columns,
            "rows": result_rows,
            "row_count": len(result_rows),
            "page": 1,
            "max_rows": rows,
            "has_more": False,
            "query_execution_id": qid,
            "table": f"{schema}.{table_name}",
            "sample_size": rows,
        }
        store_stats(qid, {
            "query_execution_id": qid,
            "connection": connection,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "row_count": len(result_rows),
            "type": "query",
            "database": conn_cfg.database,
            "host": conn_cfg.host,
        })
        return _ok(result)
    except KeyError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en sample_data")
        return _err(str(e), connection=connection)


@mcp.tool()
def table_row_count(connection: str, table_name: str, schema: str = "public") -> str:
    """
    Obtiene conteo aproximado de filas (pg_class.reltuples).

    Args:
        connection: Nombre de la conexión.
        table_name: Nombre de la tabla.
        schema: Esquema (default: public).

    Returns:
        JSON con el conteo estimado.
    """
    sql_text = """
    SELECT
        n.nspname AS schema,
        c.relname AS table,
        GREATEST(c.reltuples::bigint, 0) AS row_count,
        'approximate' AS count_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname = %s
      AND c.relname = %s
    """
    try:
        resolve_connection(connection)
        return _ok(execute_parametrized(connection, sql_text, (schema, table_name), max_rows=10))
    except KeyError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en table_row_count")
        return _err(str(e), connection=connection)


@mcp.tool()
def search_columns(connection: str, pattern: str, schema: Optional[str] = None) -> str:
    """
    Busca columnas por nombre (ILIKE).

    Args:
        connection: Nombre de la conexión.
        pattern: Patrón (ej. '%user%', 'email%').
        schema: Filtrar por esquema (opcional).

    Returns:
        JSON con tablas y columnas que coinciden.
    """
    if "%" not in pattern and "_" not in pattern:
        pattern = f"%{pattern}%"

    if schema:
        sql_text = """
        SELECT
            table_schema AS schema,
            table_name AS table,
            column_name AS column,
            data_type AS type
        FROM information_schema.columns
        WHERE column_name ILIKE %s
          AND table_schema = %s
        ORDER BY table_schema, table_name, ordinal_position
        """
        params: tuple = (pattern, schema)
    else:
        sql_text = """
        SELECT
            table_schema AS schema,
            table_name AS table,
            column_name AS column,
            data_type AS type
        FROM information_schema.columns
        WHERE column_name ILIKE %s
        ORDER BY table_schema, table_name, ordinal_position
        """
        params = (pattern,)

    try:
        resolve_connection(connection)
        result = execute_parametrized(connection, sql_text, params, max_rows=1000)
        result["pattern"] = pattern
        return _ok(result)
    except KeyError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("Error en search_columns")
        return _err(str(e), connection=connection)


@mcp.tool()
def explain_query(connection: str, sql: str) -> str:
    """
    Plan de ejecución estimado (EXPLAIN FORMAT JSON).

    Args:
        connection: Nombre de la conexión.
        sql: Query a analizar.

    Returns:
        JSON con el plan de ejecución.
    """
    try:
        resolve_connection(connection)
    except KeyError as e:
        return _err(str(e))

    if CFG.readonly:
        err = validate_readonly(sql)
        if err:
            return _err(err)

    qid = str(uuid.uuid4())
    started = time.time()
    explain_sql = f"EXPLAIN (FORMAT JSON) {sql.strip().rstrip(';')}"

    try:
        with get_connection(connection) as (conn, conn_cfg):
            with conn.cursor() as cursor:
                cursor.execute(explain_sql)
                row = cursor.fetchone()
                plan = serialize_value(row[0]) if row else None

        elapsed = time.time() - started
        stats = {
            "query_execution_id": qid,
            "connection": connection,
            "state": "SUCCEEDED",
            "execution_time_ms": int(elapsed * 1000),
            "type": "explain",
            "query_preview": sql[:200],
            "database": conn_cfg.database,
        }
        store_stats(qid, stats)
        return _ok({
            "query_execution_id": qid,
            "connection": connection,
            "type": "explain",
            "plan": plan,
        })
    except Exception as e:
        logger.exception("Error en explain_query")
        return _err(str(e), connection=connection, sql=sql[:200])


@mcp.tool()
def test_connection(connection: Optional[str] = None) -> str:
    """
    Prueba una conexión o todas las configuradas.

    Args:
        connection: Nombre de la conexión. Si se omite, prueba todas.

    Returns:
        JSON con estado, versión del servidor y tiempo.
    """
    targets = [connection] if connection else REGISTRY.names()
    if connection:
        try:
            resolve_connection(connection)
        except KeyError as e:
            return _err(str(e))

    results = []
    for name in targets:
        conn_cfg = REGISTRY.get(name)
        sql_text = """
        SELECT
            current_user AS authenticated_user,
            current_database() AS database,
            version() AS server_version,
            now() AS server_time
        """
        try:
            result = execute_query(name, sql_text, max_rows=1, skip_cache=True)
            result["status"] = "connected"
            result["connection"] = name
            result["host"] = conn_cfg.host
            result["port"] = conn_cfg.port
            result["readonly"] = CFG.readonly
            result["cache_ttl"] = CFG.cache_ttl
            results.append(result)
        except Exception as e:
            results.append({
                "status": "error",
                "connection": name,
                "host": conn_cfg.host,
                "database": conn_cfg.database,
                "error": str(e),
            })

    if connection:
        payload = results[0]
        if payload.get("status") == "connected":
            return _ok(payload)
        return _err(payload.get("error", "connection failed"), **{k: v for k, v in payload.items() if k != "error"})

    connected = sum(1 for r in results if r.get("status") == "connected")
    return _ok({
        "summary": f"{connected}/{len(results)} conexiones OK",
        "results": results,
    })


# ============================================================
# Entry point
# ============================================================


def bootstrap() -> None:
    CFG.validate()
    REGISTRY.load(CFG.connections_file)


def cli_test(connection: Optional[str] = None) -> int:
    try:
        bootstrap()
    except Exception as e:
        print(f"ERROR: Config inválida: {e}", file=sys.stderr)
        return 2

    output = test_connection(connection)
    print(output)
    try:
        parsed = json.loads(output)
        if connection:
            ok = parsed.get("status") == "connected"
        else:
            results = parsed.get("results", [])
            connected = sum(1 for r in results if r.get("status") == "connected")
            ok = len(results) > 0 and connected == len(results)
        if ok:
            print("OK: Conexión(es) OK")
            return 0
        print("ERROR: Una o más conexiones fallaron")
        return 1
    except json.JSONDecodeError as e:
        print(f"ERROR: Respuesta JSON inválida: {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server")
    parser.add_argument("--test", nargs="?", const="__all__", metavar="CONNECTION",
                        help="Prueba conexión(es) y sale")
    args = parser.parse_args()

    if args.test is not None:
        conn = None if args.test == "__all__" else args.test
        return cli_test(conn)

    try:
        bootstrap()
    except Exception as e:
        logger.error(str(e))
        return 1

    logger.info("Iniciando PostgreSQL MCP Server")
    logger.info(f"  Connections file: {CFG.connections_file}")
    logger.info(f"  User:             {CFG.user}")
    logger.info(f"  Connections:      {', '.join(REGISTRY.names())}")
    logger.info(f"  Readonly:         {CFG.readonly}")
    logger.info(f"  Query timeout:    {CFG.query_timeout}s")
    logger.info(f"  SSL mode:         {CFG.sslmode}")
    logger.info(f"  sqlparse:         {'sí' if HAS_SQLPARSE else 'no'}")

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
