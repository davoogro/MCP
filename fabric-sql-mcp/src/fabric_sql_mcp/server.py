"""Servidor MCP para Microsoft Fabric SQL (warehouses y SQL endpoints de lakehouses).

Unifica fabric_mcp_server.py y fabric_mcp_server2.py en un solo servidor multi-conexión:
cada tool acepta ``connection`` (alias de la conexión) y ``database`` (warehouse/lakehouse
dentro del mismo endpoint). Si se omiten se usa la conexión y base por defecto.

Uso:
  python -m fabric_sql_mcp               # ejecutar como MCP server (stdio)
  python -m fabric_sql_mcp --list        # listar conexiones configuradas (sin secretos)
  python -m fabric_sql_mcp --test [name] # probar conexión(es)
  python -m fabric_sql_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

from fabric_sql_mcp import __version__
from fabric_sql_mcp.config import Connection, Settings, discover_connections, load_env_files

LOADED_ENV_FILES = load_env_files()

import pyodbc  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from fabric_sql_mcp import db  # noqa: E402
from fabric_sql_mcp import export as export_mod  # noqa: E402
from fabric_sql_mcp import query_history as qh  # noqa: E402
from fabric_sql_mcp.safety import HAS_SQLPARSE, quote_ident, validate_readonly  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fabric-sql-mcp")

CONNECTIONS: dict[str, Connection] = discover_connections()
SETTINGS = Settings.from_env()
pyodbc.pooling = False

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE_QUERY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
_ANY_WRITABLE = any(not c.readonly for c in CONNECTIONS.values())


def _instructions() -> str:
    names = ", ".join(
        f"'{c.name}' ({c.server}, db por defecto {c.database}, "
        f"{'solo lectura' if c.readonly else 'lectura/escritura'})"
        for c in CONNECTIONS.values()
    ) or "ninguna configurada"
    return (
        "Acceso SQL (T-SQL) a Microsoft Fabric Data Warehouse / SQL analytics endpoints. "
        f"Conexiones: {names}. Conexión por defecto: '{_default_name() or '-'}'. "
        "Flujo recomendado: list_databases -> list_schemas -> list_tables -> describe_table -> "
        "sample_data / query. El dialecto es T-SQL de Fabric (usa TOP en vez de LIMIT). "
        "query devuelve next_cursor y fetch_more(cursor) para paginar (TTL 10 min, máx 5 abiertos). "
        "export_query escribe CSV/Parquet en FABRIC_EXPORT_DIR o s3://. "
        "list_procedures, get_object_definition, column_stats, query_history. "
        "Usa el parámetro 'database' para consultar otro warehouse/lakehouse del mismo endpoint."
    )


def _default_name() -> Optional[str]:
    if SETTINGS.default_connection in CONNECTIONS:
        return SETTINGS.default_connection
    return next(iter(CONNECTIONS), None)


def _resolve(connection: Optional[str]) -> Connection:
    if not CONNECTIONS:
        raise LookupError(
            "No hay conexiones Fabric configuradas. Define FABRIC_SERVER/FABRIC_USER/"
            "FABRIC_PASSWORD (y FABRIC2_..., FABRIC3_... para más) en el .env."
        )
    name = connection or _default_name()
    if name not in CONNECTIONS:
        raise LookupError(f"Conexión '{name}' no existe. Disponibles: {sorted(CONNECTIONS)}")
    return CONNECTIONS[name]


def _err(msg: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **extra}


def _ok(payload: dict) -> dict[str, Any]:
    return {"ok": True, **payload}


def _clamp(value: Optional[int], default: int, cap: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, cap))


def _run(fn, *, sql: str = "", **ctx):
    """Ejecuta fn() y traduce errores a respuestas {ok: false} legibles para el modelo."""
    try:
        return _ok(fn())
    except (LookupError, ValueError) as e:
        return _err(str(e), **ctx)
    except pyodbc.Error as e:
        logger.exception("Error pyodbc")
        return _err(f"pyodbc.Error: {e}", **({"sql": sql[:300]} if sql else {}), **ctx)
    except Exception as e:
        logger.exception("Error inesperado")
        return _err(f"{type(e).__name__}: {e}", **({"sql": sql[:300]} if sql else {}), **ctx)


def _meta(conn: Connection, sql: str, params: tuple = (), database: Optional[str] = None,
          max_rows: int = 1000) -> dict:
    return db.execute(conn, sql, params, database=database, max_rows=max_rows,
                      max_chars=SETTINGS.max_cell_chars, kind="metadata")


mcp = MCPServer(
    "fabric-sql",
    title="Microsoft Fabric SQL",
    version=__version__,
    instructions=_instructions(),
)


# ============================================================
# Tools
# ============================================================


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def list_connections() -> dict[str, Any]:
    """
    Lista las conexiones Fabric configuradas (alias, servidor, base por defecto, modo
    readonly). No expone contraseñas. Úsala para saber qué valor pasar en `connection`.
    """
    return _ok({
        "default": _default_name(),
        "connections": [c.public_info() for c in CONNECTIONS.values()],
        "env_files": [str(p) for p in LOADED_ENV_FILES],
    })


@mcp.tool(annotations=READ_ONLY)
def test_connection(connection: Optional[str] = None, database: Optional[str] = None) -> dict[str, Any]:
    """
    Prueba la conexión a un endpoint de Fabric: usuario autenticado, base, versión y hora.

    Args:
        connection: Alias de la conexión (default: la conexión por defecto).
        database: Warehouse/lakehouse a usar (default: el configurado en la conexión).
    """
    def run():
        conn = _resolve(connection)
        sql = (
            "SELECT SYSTEM_USER AS authenticated_user, DB_NAME() AS [database], "
            "@@VERSION AS server_version, GETDATE() AS server_time"
        )
        res = db.execute(conn, sql, database=database, max_rows=1, use_cache=False)
        info = res["rows"][0] if res["rows"] else {}
        return {"status": "connected", **conn.public_info(), **info,
                "execution_time_ms": db.get_stats(res["query_execution_id"])["execution_time_ms"]}
    return _run(run, connection=connection or _default_name())


@mcp.tool(annotations=READ_ONLY)
def list_databases(connection: Optional[str] = None) -> dict[str, Any]:
    """
    Lista los warehouses / SQL endpoints de lakehouses (bases de datos) visibles en el
    endpoint. Pasa el nombre en el parámetro `database` de las demás tools.

    Args:
        connection: Alias de la conexión (default: la conexión por defecto).
    """
    sql = (
        "SELECT name AS [database], database_id, create_date "
        "FROM sys.databases WHERE name NOT IN ('master','tempdb','model','msdb') ORDER BY name"
    )
    return _run(lambda: _meta(_resolve(connection), sql))


@mcp.tool(annotations=READ_ONLY)
def list_schemas(connection: Optional[str] = None, database: Optional[str] = None,
                 include_system: bool = False) -> dict[str, Any]:
    """
    Lista los esquemas de una base. Por defecto excluye sys, INFORMATION_SCHEMA, db_*, etc.

    Args:
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
        include_system: Incluir esquemas del sistema.
    """
    sql = "SELECT SCHEMA_NAME AS [schema] FROM INFORMATION_SCHEMA.SCHEMATA"
    if not include_system:
        sql += (
            " WHERE SCHEMA_NAME NOT IN ('sys','INFORMATION_SCHEMA','guest','queryinsights')"
            " AND SCHEMA_NAME NOT LIKE 'db[_]%'"
        )
    sql += " ORDER BY SCHEMA_NAME"
    return _run(lambda: _meta(_resolve(connection), sql, database=database))


@mcp.tool(annotations=READ_ONLY)
def list_tables(connection: Optional[str] = None, database: Optional[str] = None,
                schema: Optional[str] = None, pattern: Optional[str] = None) -> dict[str, Any]:
    """
    Lista tablas y vistas. Opcionalmente filtra por esquema y por nombre (LIKE).

    Args:
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
        schema: Esquema exacto (ej. 'dbo'). Si se omite, todos los esquemas no-sistema.
        pattern: Filtro por nombre de tabla, ej. 'venta' o 'fact_%'.
    """
    sql = (
        "SELECT TABLE_SCHEMA AS [schema], TABLE_NAME AS table_name, TABLE_TYPE AS [type] "
        "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA NOT IN ('sys','INFORMATION_SCHEMA','queryinsights')"
    )
    params: list = []
    if schema:
        sql += " AND TABLE_SCHEMA = ?"
        params.append(schema)
    if pattern:
        sql += " AND TABLE_NAME LIKE ?"
        params.append(pattern if any(c in pattern for c in "%_") else f"%{pattern}%")
    sql += " ORDER BY TABLE_SCHEMA, TABLE_NAME"
    return _run(lambda: _meta(_resolve(connection), sql, tuple(params), database=database))


@mcp.tool(annotations=READ_ONLY)
def describe_table(table_name: str, schema: str = "dbo", connection: Optional[str] = None,
                   database: Optional[str] = None) -> dict[str, Any]:
    """
    Describe las columnas de una tabla o vista: tipo, longitud, precisión, nullability.

    Args:
        table_name: Nombre de la tabla o vista.
        schema: Esquema (default: dbo).
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
    """
    sql = (
        "SELECT COLUMN_NAME AS [column], DATA_TYPE AS [type], "
        "CHARACTER_MAXIMUM_LENGTH AS max_length, NUMERIC_PRECISION AS [precision], "
        "NUMERIC_SCALE AS scale, IS_NULLABLE AS nullable, ORDINAL_POSITION AS position "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? AND TABLE_SCHEMA = ? "
        "ORDER BY ORDINAL_POSITION"
    )

    def run():
        res = _meta(_resolve(connection), sql, (table_name, schema), database=database)
        if not res["rows"]:
            raise LookupError(
                f"No se encontró {schema}.{table_name} (o no tiene columnas visibles). "
                "Usa list_tables con pattern para buscarla."
            )
        return {"table": f"{schema}.{table_name}", **res}
    return _run(run)


@mcp.tool(annotations=READ_ONLY)
def sample_data(table_name: str, schema: str = "dbo", rows: int = 10,
                connection: Optional[str] = None, database: Optional[str] = None) -> dict[str, Any]:
    """
    Devuelve una muestra de filas de una tabla o vista (SELECT TOP n *).

    Args:
        table_name: Nombre de la tabla o vista.
        schema: Esquema (default: dbo).
        rows: Número de filas (default: 10, max: 100).
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
    """
    def run():
        n = max(1, min(int(rows), 100))
        sql = f"SELECT TOP ({n}) * FROM {quote_ident(schema)}.{quote_ident(table_name)}"
        res = db.execute(_resolve(connection), sql, database=database, max_rows=n,
                         use_cache=False, max_chars=SETTINGS.max_cell_chars)
        return {"table": f"{schema}.{table_name}", **res}
    return _run(run)


@mcp.tool(annotations=READ_ONLY)
def table_row_count(table_name: str, schema: str = "dbo", exact: bool = False,
                    connection: Optional[str] = None, database: Optional[str] = None) -> dict[str, Any]:
    """
    Cuenta filas de una tabla. exact=False (default) usa sys.partitions (rápido, puede no
    reflejar Delta/lakehouse); exact=True usa COUNT_BIG(*) (fiable pero más lento).

    Args:
        table_name: Nombre de la tabla.
        schema: Esquema (default: dbo).
        exact: COUNT_BIG(*) exacto o aproximado vía metadatos (default: aproximado).
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
    """
    def run():
        conn = _resolve(connection)
        if exact:
            sql = f"SELECT COUNT_BIG(*) AS row_count FROM {quote_ident(schema)}.{quote_ident(table_name)}"
            res = db.execute(conn, sql, database=database, max_rows=1)
        else:
            sql = (
                "SELECT SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END) AS row_count "
                "FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN sys.partitions p ON t.object_id = p.object_id "
                "WHERE t.name = ? AND s.name = ?"
            )
            res = _meta(conn, sql, (table_name, schema), database=database, max_rows=1)
        count = res["rows"][0]["row_count"] if res["rows"] else None
        return {"table": f"{schema}.{table_name}", "exact": exact, "row_count": count,
                "query_execution_id": res["query_execution_id"]}
    return _run(run)


@mcp.tool(annotations=READ_ONLY)
def search_columns(pattern: str, schema: Optional[str] = None, connection: Optional[str] = None,
                   database: Optional[str] = None) -> dict[str, Any]:
    """
    Busca columnas por nombre en todas las tablas/vistas (LIKE). Útil para encontrar
    dónde vive un dato (ej. 'cliente', '%_id', 'fecha%').

    Args:
        pattern: Texto o patrón LIKE. Sin comodines se busca como '%texto%'.
        schema: Filtrar por esquema (opcional).
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
    """
    like = pattern if any(c in pattern for c in "%_") else f"%{pattern}%"
    sql = (
        "SELECT TABLE_SCHEMA AS [schema], TABLE_NAME AS [table], COLUMN_NAME AS [column], "
        "DATA_TYPE AS [type] FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME LIKE ?"
    )
    params: list = [like]
    if schema:
        sql += " AND TABLE_SCHEMA = ?"
        params.append(schema)
    sql += " ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
    return _run(lambda: {"pattern": like,
                         **_meta(_resolve(connection), sql, tuple(params), database=database)})


@mcp.tool(annotations=READ_ONLY)
def get_view_definition(view_name: str, schema: str = "dbo", connection: Optional[str] = None,
                        database: Optional[str] = None) -> dict[str, Any]:
    """
    Devuelve el código T-SQL de una vista, función o procedimiento.

    Args:
        view_name: Nombre del objeto.
        schema: Esquema (default: dbo).
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
    """
    sql = (
        "SELECT o.type_desc AS object_type, m.definition FROM sys.sql_modules m "
        "JOIN sys.objects o ON o.object_id = m.object_id "
        "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.name = ? AND s.name = ?"
    )

    def run():
        res = db.execute(_resolve(connection), sql, (view_name, schema), database=database,
                         max_rows=1, max_chars=0, kind="metadata")
        if not res["rows"]:
            raise LookupError(f"No se encontró definición para {schema}.{view_name}")
        return {"object": f"{schema}.{view_name}", **res["rows"][0],
                "query_execution_id": res["query_execution_id"]}
    return _run(run)


@mcp.tool(annotations=READ_ONLY if not _ANY_WRITABLE else WRITE_QUERY)
def query(sql: str, connection: Optional[str] = None, database: Optional[str] = None,
          max_rows: Optional[int] = None, page_size: Optional[int] = None,
          cursor: Optional[str] = None, max_cell_chars: Optional[int] = None,
          page: int = 1) -> dict[str, Any]:
    """
    Ejecuta un query T-SQL en Microsoft Fabric. En conexiones readonly (default) solo se
    permite un único SELECT / WITH (CTE) o EXEC sp_help/sp_columns/sp_tables/sp_helptext.
    Paginación con cursor: devuelve next_cursor (TTL 10 min, máx 5 abiertos); usa
    fetch_more(cursor) para la siguiente página. Usa TOP, no LIMIT.

    Args:
        sql: Query T-SQL (ignorado si se pasa cursor).
        connection: Alias de la conexión (ver list_connections).
        database: Warehouse/lakehouse (default: el de la conexión).
        max_rows: Filas por página (default FABRIC_MCP_MAX_ROWS=5000, max 5000).
        page_size: Alias de max_rows por página.
        cursor: Continúa una consulta previa (valor de next_cursor).
        max_cell_chars: Truncado de celdas (default FABRIC_MCP_MAX_CELL_CHARS, max 10000).
        page: Obsoleto (solo compatibilidad); usa next_cursor/fetch_more en su lugar.
    """
    def run():
        if page > 1 and not (cursor and cursor.strip()):
            raise ValueError(
                "El parámetro page>1 ya no se admite; usa next_cursor y fetch_more para paginar."
            )
        conn = _resolve(connection)
        db_name = database or conn.database
        n = _clamp(max_rows, SETTINGS.max_rows, db.MAX_PAGE_ROWS)
        ps = _clamp(page_size, n, db.MAX_PAGE_ROWS) if page_size is not None else n
        mc = _clamp(max_cell_chars, SETTINGS.max_cell_chars, 10_000)
        if cursor and cursor.strip():
            out = db.fetch_query_page(cursor.strip(), ps)
        else:
            if conn.readonly:
                err = validate_readonly(sql)
                if err:
                    raise ValueError(f"{err}. (Para escritura configura {conn.prefix}READONLY=false)")
            out = db.start_query_cursor(
                conn, database, sql, ps, mc, SETTINGS.max_response_chars,
                response_envelope={"ok": True, "connection": conn.name, "database": db_name},
                cursor_max_rows=SETTINGS.cursor_max_rows,
            )
        qid = out.get("query_execution_id", "")
        if qid:
            db.store_stats(qid, {
                "query_execution_id": qid,
                "state": "SUCCEEDED",
                "connection": conn.name,
                "database": db_name,
                "type": out.get("type", "query"),
                "execution_time_ms": out.get("elapsed_ms", 0),
                "row_count": out.get("row_count", 0),
                "has_more": out.get("has_more"),
                "query_preview": (sql or f"[cursor {cursor}]")[:300],
            })
        qh.record(
            tool="query", sql=sql or f"[cursor {cursor}]", connection=conn.name, database=db_name,
            row_count=out.get("row_count"), elapsed_ms=out.get("elapsed_ms"),
        )
        out.pop("_response_rows_fitted", None)
        return out
    return _run(run, sql=sql)


@mcp.tool(annotations=READ_ONLY)
def fetch_more(cursor: str, page_size: Optional[int] = None,
               connection: Optional[str] = None) -> dict[str, Any]:
    """
    Devuelve la siguiente página de una consulta paginada (next_cursor devuelto por query).

    Args:
        cursor: Identificador next_cursor.
        page_size: Filas a traer (default FABRIC_MCP_MAX_ROWS).
        connection: Informativo; el cursor ya está ligado a la conexión/base originales.
    """
    def run():
        ps = _clamp(page_size, SETTINGS.max_rows, db.MAX_PAGE_ROWS)
        out = db.fetch_query_page(cursor.strip(), ps)
        qh.record(
            tool="fetch_more", sql=f"[cursor {cursor}]", connection=connection,
            row_count=out.get("row_count"), elapsed_ms=out.get("elapsed_ms"),
        )
        out.pop("_response_rows_fitted", None)
        return out
    return _run(run, connection=connection)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def query_history(limit: int = 100) -> dict[str, Any]:
    """Últimas consultas ejecutadas por este MCP en la sesión del proceso (sql recortada, filas, duración)."""
    lim = _clamp(limit, 100, 100)
    items = qh.list_history(lim)
    return _ok({"count": len(items), "queries": items})


@mcp.tool(annotations=READ_ONLY)
def export_query(
    sql: str,
    connection: Optional[str] = None,
    database: Optional[str] = None,
    format: str = "csv",
    destination: Optional[str] = None,
) -> dict[str, Any]:
    """
    Exporta el resultado completo de un SELECT a CSV o Parquet en streaming (lotes 50k filas),
    sin pasar por el LLM. Destino local: FABRIC_EXPORT_DIR (default C:\\DVB\\exports\\fabric) o ruta;
    s3://bucket/clave/... sube con boto3 (credenciales del .env compartido C:\\DVB\\mcp).
    Tope FABRIC_EXPORT_MAX_ROWS (default 50 millones). Mismo validador de solo lectura que query.
    """
    def run():
        conn = _resolve(connection)
        if conn.readonly:
            err = validate_readonly(sql)
            if err:
                raise ValueError(err)
        db_name = database or conn.database
        out = export_mod.export_query_result(
            SETTINGS, conn, sql, database=database, fmt=format, destination=destination,
        )
        qh.record(
            tool="export_query", sql=sql, connection=conn.name, database=db_name,
            row_count=out.get("rows"), elapsed_ms=out.get("elapsed_ms"),
        )
        return {"connection": conn.name, "database": db_name, **out}
    return _run(run, sql=sql)


@mcp.tool(annotations=READ_ONLY)
def list_procedures(connection: Optional[str] = None, database: Optional[str] = None,
                    schema: Optional[str] = None) -> dict[str, Any]:
    """
    Lista procedimientos almacenados de un warehouse/lakehouse, opcionalmente filtrados por esquema.

    Args:
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
        schema: Esquema exacto (opcional).
    """
    sql = (
        "SELECT SCHEMA_NAME(o.schema_id) AS [schema], o.name AS procedure_name, "
        "o.create_date, o.modify_date FROM sys.objects o WHERE o.type = 'P' AND o.is_ms_shipped = 0"
    )
    params: list = []
    if schema:
        sql += " AND SCHEMA_NAME(o.schema_id) = ?"
        params.append(schema)
    sql += " ORDER BY 1, 2"
    return _run(lambda: _meta(_resolve(connection), sql, tuple(params), database=database))


@mcp.tool(annotations=READ_ONLY)
def get_object_definition(name: str, connection: Optional[str] = None, database: Optional[str] = None,
                          schema: str = "dbo") -> dict[str, Any]:
    """
    Devuelve el código T-SQL de una vista, función, procedimiento u otro objeto con módulo SQL.

    Args:
        name: Nombre del objeto (sin esquema).
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
        schema: Esquema (default: dbo).
    """
    sql = (
        "SELECT o.type_desc AS object_type, m.definition FROM sys.sql_modules m "
        "JOIN sys.objects o ON o.object_id = m.object_id "
        "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.name = ? AND s.name = ?"
    )

    def run():
        res = db.execute(_resolve(connection), sql, (name, schema), database=database,
                         max_rows=1, max_chars=0, kind="metadata")
        if not res["rows"]:
            raise LookupError(f"No se encontró definición para {schema}.{name}")
        row = res["rows"][0]
        definition = row.get("definition")
        out = {"object": f"{schema}.{name}", **row, "query_execution_id": res["query_execution_id"]}
        if definition is not None and len(definition) > 100_000:
            out["definition_chars"] = len(definition)
            out["definition"] = definition[:100_000] + "\n…[truncado]"
        return out
    return _run(run)


@mcp.tool(annotations=READ_ONLY)
def column_stats(connection: Optional[str], database: Optional[str], schema: str, table: str,
                 columns: Optional[list[str]] = None) -> dict[str, Any]:
    """
    Estadísticas por columna: mínimo, máximo, nulos y distintos aproximados (APPROX_COUNT_DISTINCT).
    Si columns se omite, usa todas las columnas de la tabla (máximo 30).

    Args:
        connection: Alias de la conexión.
        database: Warehouse/lakehouse.
        schema: Esquema de la tabla.
        table: Nombre de la tabla.
        columns: Lista de columnas a analizar (opcional, máx 30).
    """
    def run():
        conn = _resolve(connection)
        db_name = database or conn.database
        truncated_cols = False
        if columns:
            cols = columns[:30]
        else:
            desc_sql = (
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
            )
            desc = db.execute(conn, desc_sql, (schema, table), database=database, max_rows=31,
                            use_cache=False, kind="metadata")
            all_cols = [r["COLUMN_NAME"] for r in desc["rows"]]
            cols = all_cols[:30]
            truncated_cols = len(all_cols) > 30
        if not cols:
            raise LookupError(f"No hay columnas en {schema}.{table}")
        ref = f"{quote_ident(schema)}.{quote_ident(table)}"
        parts = []
        for col in cols:
            qcol = quote_ident(col)
            parts.append(
                f"SELECT ? AS column_name, MIN({qcol}) AS min_value, MAX({qcol}) AS max_value, "
                f"SUM(CASE WHEN {qcol} IS NULL THEN 1 ELSE 0 END) AS null_count, "
                f"APPROX_COUNT_DISTINCT({qcol}) AS approx_distinct FROM {ref}"
            )
        sql = " UNION ALL ".join(parts)
        params = tuple(cols)
        res = db.execute(conn, sql, params, database=database, max_rows=len(cols), use_cache=False)
        out = {"connection": conn.name, "database": db_name, "table": f"{schema}.{table}",
               "column_count": len(cols), **res}
        if truncated_cols:
            out["warning"] = "Tabla con más de 30 columnas; solo se analizaron las primeras 30."
        return out
    return _run(run, connection=connection, database=database)


if _ANY_WRITABLE:

    @mcp.tool(annotations=WRITE_QUERY)
    def execute(sql: str, connection: Optional[str] = None, database: Optional[str] = None) -> dict[str, Any]:
        """
        Ejecuta SQL de escritura (INSERT/UPDATE/DELETE/DDL) solo en conexiones con READONLY=false.
        No valida como solo lectura. Usar con precaución.

        Args:
            sql: Sentencia T-SQL.
            connection: Alias de la conexión (debe tener readonly=false).
            database: Warehouse/lakehouse (default: el de la conexión).
        """
        def run():
            conn = _resolve(connection)
            if conn.readonly:
                raise ValueError(
                    f"La conexión '{conn.name}' es readonly. Configura {conn.prefix}READONLY=false."
                )
            return db.execute(conn, sql, database=database, max_rows=1, use_cache=False, max_chars=0)
        return _run(run, sql=sql)



@mcp.tool(annotations=READ_ONLY)
def explain_query(sql: str, connection: Optional[str] = None,
                  database: Optional[str] = None) -> dict[str, Any]:
    """
    Plan de ejecución estimado (SHOWPLAN_XML) sin ejecutar el query.

    Args:
        sql: Query a analizar.
        connection: Alias de la conexión.
        database: Warehouse/lakehouse (default: el de la conexión).
    """
    def run():
        conn = _resolve(connection)
        if conn.readonly:
            err = validate_readonly(sql)
            if err:
                raise ValueError(err)
        return db.explain(conn, sql, database=database)
    return _run(run, sql=sql)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_query_stats(query_execution_id: str) -> dict[str, Any]:
    """
    Estadísticas de una ejecución previa (tiempo, filas, conexión) a partir del
    query_execution_id devuelto por las demás tools.
    """
    stats = db.get_stats(query_execution_id)
    if not stats:
        return _err(f"No se encontró query_execution_id={query_execution_id}",
                    hint=f"Solo se conservan las últimas {db.STATS_MAX} ejecuciones.")
    return _ok(stats)


# ============================================================
# CLI
# ============================================================


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cli_test(names: list[str]) -> int:
    targets = names or list(CONNECTIONS)
    if not targets:
        _print(list_connections())
        return 2
    rc = 0
    for name in targets:
        out = test_connection(connection=name)
        _print(out)
        if not out.get("ok"):
            rc = 1
    return rc


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        _print({"ok": True, "tool_count": len(names), "tools": names})
        res = await client.call_tool("list_connections", {})
        _print(res.structured_content)
        return 0 if names else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Microsoft Fabric SQL MCP server")
    parser.add_argument("--list", action="store_true", help="Lista conexiones y sale")
    parser.add_argument("--test", nargs="*", metavar="CONN", help="Prueba conexión(es) y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía cliente MCP")
    args = parser.parse_args()

    if args.list:
        _print(list_connections())
        return 0
    if args.test is not None:
        return cli_test(args.test)
    if args.handshake:
        return asyncio.run(_handshake())

    logger.info(f"Fabric MCP v{__version__} — env: {[str(p) for p in LOADED_ENV_FILES]}")
    for c in CONNECTIONS.values():
        state = "OK" if not c.problems() else f"INCOMPLETA ({', '.join(c.problems())})"
        logger.info(f"  [{c.name}] {c.server} db={c.database} auth={c.auth} "
                    f"readonly={c.readonly} -> {state}")
    if not CONNECTIONS:
        logger.warning("Sin conexiones configuradas; las tools devolverán error hasta configurar .env")
    logger.info(f"  sqlparse: {'sí' if HAS_SQLPARSE else 'no'}")
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
