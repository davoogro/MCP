"""Servidor MCP de SOLO LECTURA para los SQL Server OPSDB (sql01 / sql05).

Uso:
  python -m sqlserver_readonly_mcp               # MCP server (stdio)
  python -m sqlserver_readonly_mcp --list        # conexiones configuradas (sin secretos)
  python -m sqlserver_readonly_mcp --test [alias]# probar conexión(es)
  python -m sqlserver_readonly_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from sqlserver_readonly_mcp import __version__
from sqlserver_readonly_mcp.config import Server, Settings, load_env_files

LOADED_ENV_FILES = load_env_files()

import pyodbc  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from sqlserver_readonly_mcp import db  # noqa: E402
from sqlserver_readonly_mcp import export as export_mod  # noqa: E402
from sqlserver_readonly_mcp import query_history as qh  # noqa: E402
from sqlserver_readonly_mcp.safety import (  # noqa: E402
    like_pattern,
    quote_ident,
    validate_fragment,
    validate_readonly,
)


class PermissionDeniedError(PermissionError):
    def __init__(self, message: str, missing_permission: str):
        super().__init__(message)
        self.missing_permission = missing_permission

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sqlserver-readonly-mcp")

SETTINGS = Settings.from_env()
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

KNOWN_DATABASES = {
    "sql01": "OPSDB, CM_TELCOVENDORA_4G_STAGING",
    "sql05": "ANALYTICS_4G, ANALYTICS_DW, KPIDB",
}

OBJECT_TYPES = {
    "table": ("U",),
    "view": ("V",),
    "procedure": ("P", "PC"),
    "function": ("FN", "IF", "TF", "FS", "FT"),
    "trigger": ("TR",),
    "synonym": ("SN",),
}


def _instructions() -> str:
    servers = "; ".join(
        f"'{s.alias}' = {s.address}" + (f" (bases: {KNOWN_DATABASES[s.alias]})" if s.alias in KNOWN_DATABASES else "")
        for s in SETTINGS.servers.values()
    )
    return (
        "Acceso de SOLO LECTURA a los SQL Server 2022 de OPSDB. Servidores (parámetro 'connection'): "
        f"{servers}. Default: '{SETTINGS.default_connection}'. Usa 'database' para elegir la base; "
        "sin 'database' se usa la base por defecto del login (sql01: OPSDB, sql05: KPIDB); también puedes usar "
        "nombres de 3 partes [db].[schema].[tabla]. "
        "Flujo recomendado: test_connection -> list_databases -> list_schemas -> list_tables -> "
        "describe_table -> sample_data / query. Para encontrar cosas: search_columns, search_objects "
        "(también dentro del código de vistas/procs con search_definitions=true), get_object_definition, "
        "get_dependencies. query_all ejecuta el mismo SELECT en todos los servidores. "
        "query devuelve next_cursor, rows_returned, rows_buffered y has_more para paginar; "
        "fetch_more(cursor) trae la siguiente página (incluye filas buffered si el JSON se recortó). "
        "export_query escribe CSV/Parquet en MSSQL_EXPORT_DIR o s3:// sin pasar por el LLM. "
        "table_stats, list_indexes, table_freshness, compare_row_counts, query_history. "
        "Dialecto T-SQL: usa TOP (n) u ORDER BY ... OFFSET n ROWS FETCH NEXT m ROWS ONLY (no LIMIT). "
        "Solo se acepta UNA sentencia SELECT/WITH; se rechazan DML/DDL, EXEC, SELECT INTO, SET/DECLARE, "
        "OPENROWSET/OPENQUERY, WAITFOR, xp_*, hints de bloqueo. Todo corre en una transacción que siempre "
        "se deshace. Prefiere agregaciones (COUNT/GROUP BY) a volcar filas; si truncated=true, usa cursor."
    )


def _resolve(connection: Optional[str]) -> Server:
    name = (connection or SETTINGS.default_connection or "").strip()
    if name not in SETTINGS.servers:
        raise LookupError(f"Conexión '{name}' no existe. Disponibles: {sorted(SETTINGS.servers)}")
    return SETTINGS.servers[name]


def _err(msg: str, *, error_code: Optional[str] = None, missing_permission: Optional[str] = None,
         **extra: Any) -> dict[str, Any]:
    payload = {"ok": False, "error": msg, **{k: v for k, v in extra.items() if v is not None}}
    if error_code:
        payload["error_code"] = error_code
    if missing_permission:
        payload["missing_permission"] = missing_permission
    return payload


def _run(fn, *, sql: str = "", connection: Optional[str] = None, database: Optional[str] = None):
    ctx = {"connection": connection, "database": database, "sql": sql[:500] if sql else None}
    try:
        out = {"ok": True, **fn()}
        return db.fit_response(out, SETTINGS.max_response_chars)
    except PermissionDeniedError as e:
        return _err(str(e), error_code="PermissionDenied", missing_permission=e.missing_permission, **ctx)
    except (LookupError, ValueError, PermissionError) as e:
        return _err(str(e), **ctx)
    except pyodbc.Error as e:
        logger.warning(f"pyodbc: {e}")
        msg = str(e.args[1]) if len(e.args) > 1 else str(e)
        return _err(f"Error SQL Server: {msg}", **ctx)
    except Exception as e:
        logger.exception("Error inesperado")
        return _err(f"{type(e).__name__}: {e}", **ctx)


def _db(server: Server, database: Optional[str]) -> Optional[str]:
    return db.resolve_database(SETTINGS, server, database)


def _object_ref(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def _split_name(name: str, schema: str) -> tuple[str, str]:
    """Permite pasar 'esquema.tabla' en el nombre."""
    n = name.strip()
    if "." in n and not (n.startswith("[") and n.endswith("]") and n.count("[") == 1):
        sch, _, obj = n.rpartition(".")
        return sch.strip().strip("[]") or schema, obj.strip()
    return schema, n


def _object_id(s: db.Session, schema: str, name: str) -> int:
    oid = s.scalar("SELECT OBJECT_ID(?)", (_object_ref(schema, name),))
    if oid is None:
        similar = s.dicts(
            "SELECT TOP 10 SCHEMA_NAME(schema_id) AS schema_name, name, type_desc FROM sys.objects "
            "WHERE name LIKE ? AND is_ms_shipped = 0 ORDER BY name", (like_pattern(name),))
        raise LookupError(f"No existe el objeto {schema}.{name} en esta base (o no tienes permiso). "
                          f"Parecidos: {similar}")
    return int(oid)


def _format_type(r: dict) -> str:
    t = r["type"]
    ml, prec, scale = r.get("max_length"), r.get("precision"), r.get("scale")
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if ml == -1 else ml})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if ml == -1 else ml // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({prec},{scale})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({scale})"
    return t


def _clamp(n: Optional[int], default: int, hi: int) -> int:
    try:
        n = int(n) if n is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hi))


def _databases_for(server: Server, database: Optional[str]) -> list[str]:
    if database:
        return [_db(server, database)]
    return [d for d in db.accessible_databases(SETTINGS, server) if d not in ("master", "tempdb", "model", "msdb")]


mcp = MCPServer("sqlserver-readonly", title="OPSDB SQL Server (solo lectura)", version=__version__,
                instructions=_instructions())


# ============================================================
# Conexión y servidor
# ============================================================


@mcp.tool(annotations=LOCAL_ONLY)
def list_connections() -> dict[str, Any]:
    """Lista los servidores SQL Server configurados (alias, host, puerto, usuario enmascarado)."""
    return {
        "ok": True,
        "default": SETTINGS.default_connection,
        "readonly": True,
        "user": SETTINGS.masked_user(),
        "missing_config": SETTINGS.problems(),
        "connections": [
            {"alias": s.alias, "host": s.host, "port": s.port, "known_databases": KNOWN_DATABASES.get(s.alias)}
            for s in SETTINGS.servers.values()
        ],
    }


def _test_one(server: Server) -> dict:
    started = time.time()
    try:
        with db.session(SETTINGS, server) as s:
            info = s.dicts(
                "SELECT @@SERVERNAME AS server_name, CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) "
                "AS product_version, CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(50)) AS product_level, "
                "CAST(SERVERPROPERTY('Edition') AS nvarchar(200)) AS edition, "
                "CAST(SERVERPROPERTY('Collation') AS nvarchar(200)) AS collation, "
                "SUSER_SNAME() AS login_name, IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin, "
                "(SELECT sqlserver_start_time FROM sys.dm_os_sys_info) AS start_time")[0] \
                if _has_server_state(s) else s.dicts(
                "SELECT @@SERVERNAME AS server_name, CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) "
                "AS product_version, CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(50)) AS product_level, "
                "CAST(SERVERPROPERTY('Edition') AS nvarchar(200)) AS edition, "
                "CAST(SERVERPROPERTY('Collation') AS nvarchar(200)) AS collation, "
                "SUSER_SNAME() AS login_name, IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin")[0]
            perms = [r["permission_name"] for r in s.dicts("SELECT permission_name FROM fn_my_permissions(NULL, 'SERVER')")]
            dbs = [r["name"] for r in s.dicts(
                "SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 AND state = 0 ORDER BY name")]
        return {"alias": server.alias, "address": server.address, "ok": True,
                "latency_ms": int((time.time() - started) * 1000), **info,
                "server_permissions": perms, "databases": dbs}
    except pyodbc.Error as e:
        msg = str(e.args[1]) if len(e.args) > 1 else str(e)
        return {"alias": server.alias, "address": server.address, "ok": False, "error": msg,
                "latency_ms": int((time.time() - started) * 1000)}


def _has_server_state(s: db.Session) -> bool:
    # SQL Server 2022 exige VIEW SERVER PERFORMANCE STATE para las DMVs de rendimiento.
    return bool(s.scalar("SELECT COALESCE(HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER PERFORMANCE STATE'), "
                         "HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE'))"))


@mcp.tool(annotations=READ_ONLY)
def test_connection(connection: Optional[str] = None) -> dict[str, Any]:
    """
    Prueba la conexión: nombre del servidor, versión, edición, login, permisos de servidor y bases
    accesibles. Sin 'connection' prueba TODOS los servidores en paralelo.
    """
    def run():
        servers = [_resolve(connection)] if connection else list(SETTINGS.servers.values())
        with ThreadPoolExecutor(max_workers=len(servers) or 1) as ex:
            results = list(ex.map(_test_one, servers))
        return {"all_ok": all(r["ok"] for r in results), "results": results}
    return _run(run, connection=connection)


@mcp.tool(annotations=READ_ONLY)
def list_databases(connection: Optional[str] = None) -> dict[str, Any]:
    """Bases de datos accesibles del servidor: estado, recovery, compatibilidad, collation y tamaño (MB)."""
    def run():
        server = _resolve(connection)
        with db.session(SETTINGS, server) as s:
            rows = s.dicts(
                "SELECT d.name, d.state_desc, d.recovery_model_desc, d.compatibility_level, d.collation_name, "
                "d.create_date, d.is_read_only, "
                "CAST(SUM(CAST(mf.size AS bigint)) * 8 / 1024.0 AS decimal(18,1)) AS size_mb "
                "FROM sys.databases d LEFT JOIN sys.master_files mf ON mf.database_id = d.database_id "
                "WHERE HAS_DBACCESS(d.name) = 1 "
                "GROUP BY d.name, d.state_desc, d.recovery_model_desc, d.compatibility_level, "
                "d.collation_name, d.create_date, d.is_read_only ORDER BY d.name")
        return {"connection": server.alias, "count": len(rows), "databases": rows}
    return _run(run, connection=connection)


# ============================================================
# Exploración
# ============================================================


@mcp.tool(annotations=READ_ONLY)
def list_schemas(database: str, connection: Optional[str] = None) -> dict[str, Any]:
    """Esquemas de una base con el número de tablas, vistas, procedimientos y funciones de cada uno."""
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        with db.session(SETTINGS, server, dbn) as s:
            rows = s.dicts(
                "SELECT s.name AS schema_name, p.name AS owner, "
                "SUM(CASE WHEN o.type = 'U' THEN 1 ELSE 0 END) AS tables, "
                "SUM(CASE WHEN o.type = 'V' THEN 1 ELSE 0 END) AS views, "
                "SUM(CASE WHEN o.type IN ('P','PC') THEN 1 ELSE 0 END) AS procedures, "
                "SUM(CASE WHEN o.type IN ('FN','IF','TF','FS','FT') THEN 1 ELSE 0 END) AS functions "
                "FROM sys.schemas s LEFT JOIN sys.database_principals p ON p.principal_id = s.principal_id "
                "LEFT JOIN sys.objects o ON o.schema_id = s.schema_id AND o.is_ms_shipped = 0 "
                "GROUP BY s.name, p.name HAVING COUNT(o.object_id) > 0 OR s.name = 'dbo' ORDER BY s.name")
        return {"connection": server.alias, "database": dbn, "schemas": rows}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def list_tables(database: str, schema: Optional[str] = None, pattern: Optional[str] = None,
                include_views: bool = True, limit: int = 500,
                connection: Optional[str] = None) -> dict[str, Any]:
    """
    Tablas (y vistas) de una base con filas aproximadas, tamaño reservado (MB) y fechas.

    Args:
        database: Base de datos (ver list_databases).
        schema: Filtra por esquema.
        pattern: Filtro por nombre (LIKE; 'cell' => '%cell%', admite * y %).
        include_views: Incluir vistas (default true).
        limit: Máximo de objetos (default 500, max 5000).
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        lim = _clamp(limit, 500, 5000)
        where, params = ["o.is_ms_shipped = 0", "o.type IN ('U','V')" if include_views else "o.type = 'U'"], []
        if schema:
            where.append("s.name = ?")
            params.append(schema)
        if pattern:
            where.append("o.name LIKE ?")
            params.append(like_pattern(pattern))
        sql = (
            f"SELECT TOP ({lim + 1}) s.name AS schema_name, o.name, "
            "CASE o.type WHEN 'U' THEN 'TABLE' ELSE 'VIEW' END AS type, "
            "(SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0,1)) "
            "AS approx_rows, "
            "(SELECT CAST(SUM(a.total_pages) * 8 / 1024.0 AS decimal(18,1)) FROM sys.partitions p "
            "JOIN sys.allocation_units a ON a.container_id = p.partition_id WHERE p.object_id = o.object_id) "
            "AS reserved_mb, o.create_date, o.modify_date "
            "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
            f"WHERE {' AND '.join(where)} ORDER BY s.name, o.name")
        with db.session(SETTINGS, server, dbn) as s:
            rows = s.dicts(sql, tuple(params))
        return {"connection": server.alias, "database": dbn, "count": min(len(rows), lim),
                "truncated": len(rows) > lim, "tables": rows[:lim]}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str, database: str, schema: str = "dbo",
                   connection: Optional[str] = None) -> dict[str, Any]:
    """
    Estructura completa de una tabla o vista: columnas (tipo, nullable, default, identity, computed,
    descripción), PK, índices, FKs salientes y entrantes, checks, triggers y filas aproximadas.
    'table' acepta 'esquema.tabla'.
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, name = _split_name(table, schema)
        with db.session(SETTINGS, server, dbn) as s:
            oid = _object_id(s, sch, name)
            obj = s.dicts(
                "SELECT SCHEMA_NAME(o.schema_id) AS schema_name, o.name, o.type_desc, o.create_date, o.modify_date, "
                "CAST(ep.value AS nvarchar(4000)) AS description, "
                "(SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0,1)) "
                "AS approx_rows FROM sys.objects o LEFT JOIN sys.extended_properties ep ON ep.major_id = o.object_id "
                "AND ep.minor_id = 0 AND ep.class = 1 AND ep.name = 'MS_Description' WHERE o.object_id = ?",
                (oid,))[0]
            cols = s.dicts(
                "SELECT c.column_id, c.name, t.name AS type, c.max_length, c.precision, c.scale, c.is_nullable, "
                "c.is_identity, c.is_computed, c.collation_name, dc.definition AS default_value, "
                "cc.definition AS computed_definition, CAST(ep.value AS nvarchar(4000)) AS description "
                "FROM sys.columns c JOIN sys.types t ON t.user_type_id = c.user_type_id "
                "LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id "
                "LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id "
                "LEFT JOIN sys.extended_properties ep ON ep.major_id = c.object_id AND ep.minor_id = c.column_id "
                "AND ep.class = 1 AND ep.name = 'MS_Description' WHERE c.object_id = ? ORDER BY c.column_id",
                (oid,))
            columns = []
            for c in cols:
                item = {"name": c["name"], "type": _format_type(c), "nullable": bool(c["is_nullable"])}
                for k in ("is_identity", "is_computed"):
                    if c[k]:
                        item[k.removeprefix("is_")] = True
                for k in ("default_value", "computed_definition", "description"):
                    if c[k] is not None:
                        item[k] = c[k]
                columns.append(item)
            agg = ("STUFF((SELECT ', ' + QUOTENAME(c.name) {desc} FROM sys.index_columns ic "
                   "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                   "WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = {inc} "
                   "ORDER BY ic.key_ordinal, ic.index_column_id FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '')")
            indexes = s.dicts(
                "SELECT i.name, i.type_desc, i.is_unique, i.is_primary_key, i.is_unique_constraint, i.is_disabled, "
                "i.filter_definition, "
                + agg.format(desc="+ CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END", inc=0)
                + " AS key_columns, " + agg.format(desc="", inc=1) + " AS included_columns "
                "FROM sys.indexes i WHERE i.object_id = ? AND i.type > 0 ORDER BY i.index_id", (oid,))
            fks = s.dicts(
                "SELECT fk.name, OBJECT_SCHEMA_NAME(fk.parent_object_id) AS from_schema, "
                "OBJECT_NAME(fk.parent_object_id) AS from_table, OBJECT_SCHEMA_NAME(fk.referenced_object_id) "
                "AS to_schema, OBJECT_NAME(fk.referenced_object_id) AS to_table, "
                "STUFF((SELECT ', ' + pc.name + ' -> ' + rc.name FROM sys.foreign_key_columns fkc "
                "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id "
                "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
                "AND rc.column_id = fkc.referenced_column_id WHERE fkc.constraint_object_id = fk.object_id "
                "ORDER BY fkc.constraint_column_id FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') "
                "AS columns, fk.delete_referential_action_desc AS on_delete, "
                "fk.update_referential_action_desc AS on_update, fk.is_disabled, fk.parent_object_id "
                "FROM sys.foreign_keys fk WHERE fk.parent_object_id = ? OR fk.referenced_object_id = ?",
                (oid, oid))
            checks = s.dicts("SELECT name, definition, is_disabled FROM sys.check_constraints "
                             "WHERE parent_object_id = ?", (oid,))
            triggers = s.dicts("SELECT name, is_disabled, is_instead_of_trigger FROM sys.triggers "
                               "WHERE parent_id = ?", (oid,))
        pk = next((i for i in indexes if i["is_primary_key"]), None)
        outgoing = [{k: v for k, v in f.items() if k != "parent_object_id"} for f in fks if f["parent_object_id"] == oid]
        incoming = [{k: v for k, v in f.items() if k != "parent_object_id"} for f in fks if f["parent_object_id"] != oid]
        return {
            "connection": server.alias, "database": dbn, **obj,
            "column_count": len(columns), "columns": columns,
            "primary_key": {"name": pk["name"], "columns": pk["key_columns"]} if pk else None,
            "indexes": indexes, "foreign_keys": outgoing, "referenced_by": incoming,
            "check_constraints": checks, "triggers": triggers,
        }
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def sample_data(table: str, database: str, schema: str = "dbo", rows: int = 10,
                columns: Optional[list[str]] = None, where: Optional[str] = None,
                order_by: Optional[str] = None, connection: Optional[str] = None) -> dict[str, Any]:
    """
    Muestra filas de una tabla/vista: SELECT TOP (rows) [columns] FROM schema.table [WHERE] [ORDER BY].

    Args:
        rows: Filas (default 10, max 1000).
        columns: Lista de columnas (default todas).
        where: Condición WHERE sin la palabra WHERE, p. ej. "fecha >= '2026-10-01'".
        order_by: Expresión ORDER BY sin las palabras ORDER BY, p. ej. "fecha DESC".
    """
    sql = ""

    def run():
        nonlocal sql
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, name = _split_name(table, schema)
        n = _clamp(rows, 10, 1000)
        cols = ", ".join(quote_ident(c) for c in columns) if columns else "*"
        for frag, what in ((where, "where"), (order_by, "order_by")):
            err = validate_fragment(frag, what)
            if err:
                raise ValueError(err)
        sql = f"SELECT TOP ({n}) {cols} FROM {_object_ref(sch, name)}"
        if where and where.strip():
            sql += f" WHERE {where}"
        if order_by and order_by.strip():
            sql += f" ORDER BY {order_by}"
        err = validate_readonly(sql)
        if err:
            raise ValueError(err)
        with db.session(SETTINGS, server, dbn) as s:
            out = s.table(sql, max_rows=n)
        return {"connection": server.alias, "database": dbn, "sql": sql, **out}
    return _run(run, sql=sql, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def table_row_count(table: str, database: str, schema: str = "dbo", exact: bool = False,
                    connection: Optional[str] = None) -> dict[str, Any]:
    """Número de filas: aproximado e instantáneo (sys.partitions) o exacto con COUNT_BIG(*) si exact=true."""
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, name = _split_name(table, schema)
        with db.session(SETTINGS, server, dbn) as s:
            oid = _object_id(s, sch, name)
            approx = s.scalar("SELECT SUM(rows) FROM sys.partitions WHERE object_id = ? AND index_id IN (0,1)", (oid,))
            out = {"connection": server.alias, "database": dbn, "table": f"{sch}.{name}", "approx_rows": approx}
            if exact:
                started = time.time()
                out["exact_rows"] = s.scalar(f"SELECT COUNT_BIG(*) FROM {_object_ref(sch, name)}")
                out["elapsed_ms"] = int((time.time() - started) * 1000)
        return out
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def table_stats(
    database: str,
    connection: Optional[str] = None,
    schema: Optional[str] = None,
    table: Optional[str] = None,
    top: int = 50,
) -> dict[str, Any]:
    """
    Estadísticas de tablas: filas (sys.partitions), tamaño reservado/usado, particiones y
    última modificación de datos (sys.dm_db_index_usage_stats.last_user_update si hay permiso).
    Ordenado por tamaño reservado. Filtra por schema/table opcionales.
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        lim = _clamp(top, 50, 500)
        where, params = ["o.type = 'U'", "o.is_ms_shipped = 0"], []
        if schema:
            where.append("s.name = ?")
            params.append(schema)
        if table:
            where.append("o.name = ?")
            params.append(table.strip().split(".")[-1])
        with db.session(SETTINGS, server, dbn) as s:
            has_usage = bool(s.scalar(
                "SELECT COALESCE(HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE'), "
                "HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW DATABASE STATE'))"))
            last_col = "MAX(us.last_user_update) AS last_user_update" if has_usage else "CAST(NULL AS datetime) AS last_user_update"
            usage_join = (
                "LEFT JOIN sys.dm_db_index_usage_stats us ON us.object_id = o.object_id AND us.database_id = DB_ID() "
                if has_usage else ""
            )
            sql = (
                f"SELECT TOP ({lim}) s.name AS schema_name, o.name AS table_name, "
                "SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows ELSE 0 END) AS row_count, "
                "COUNT(DISTINCT p.partition_id) AS partition_count, "
                "CAST(SUM(a.total_pages) * 8 / 1024.0 AS decimal(18,2)) AS reserved_mb, "
                "CAST(SUM(a.used_pages) * 8 / 1024.0 AS decimal(18,2)) AS used_mb, "
                f"{last_col} "
                "FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
                "JOIN sys.partitions p ON p.object_id = o.object_id "
                "JOIN sys.allocation_units a ON a.container_id = p.partition_id "
                f"{usage_join}"
                f"WHERE {' AND '.join(where)} "
                "GROUP BY s.name, o.name ORDER BY reserved_mb DESC"
            )
            rows = s.dicts(sql, tuple(params))
        return {"connection": server.alias, "database": dbn, "count": len(rows), "tables": rows}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def list_indexes(
    database: str,
    schema: str,
    table: str,
    connection: Optional[str] = None,
) -> dict[str, Any]:
    """Índices de una tabla: tipo, único, PK, columnas clave/incluidas, filtro y estado."""
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, name = _split_name(table, schema)
        with db.session(SETTINGS, server, dbn) as s:
            oid = _object_id(s, sch, name)
            agg = ("STUFF((SELECT ', ' + QUOTENAME(c.name) {desc} FROM sys.index_columns ic "
                   "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
                   "WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = {inc} "
                   "ORDER BY ic.key_ordinal, ic.index_column_id FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '')")
            indexes = s.dicts(
                "SELECT i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key, i.is_unique_constraint, "
                "i.is_disabled, i.filter_definition, "
                + agg.format(desc="+ CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END", inc=0)
                + " AS key_columns, " + agg.format(desc="", inc=1) + " AS included_columns "
                "FROM sys.indexes i WHERE i.object_id = ? AND i.type > 0 ORDER BY i.index_id",
                (oid,),
            )
        return {
            "connection": server.alias, "database": dbn, "table": f"{sch}.{name}",
            "count": len(indexes), "indexes": indexes,
        }
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def table_freshness(
    database: str,
    table: str,
    date_column: str,
    connection: Optional[str] = None,
    schema: str = "dbo",
    group_by: Optional[str] = None,
) -> dict[str, Any]:
    """
    Frescura de datos: MIN/MAX de una columna fecha y conteo por día de los últimos 14 días
    (detectar retención o huecos). group_by opcional añade desglose por otra columna (top 20 valores).
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, name = _split_name(table, schema)
        col = quote_ident(date_column.strip())
        ref = _object_ref(sch, name)
        with db.session(SETTINGS, server, dbn) as s:
            bounds = s.dicts(
                f"SELECT MIN({col}) AS min_date, MAX({col}) AS max_date, COUNT_BIG(*) AS total_rows FROM {ref}")[0]
            daily = s.dicts(
                f"SELECT CAST({col} AS date) AS day, COUNT_BIG(*) AS rows "
                f"FROM {ref} WHERE {col} >= CAST(DATEADD(day, -14, GETDATE()) AS date) "
                f"GROUP BY CAST({col} AS date) ORDER BY day"
            )
            breakdown = None
            if group_by and group_by.strip():
                gb = quote_ident(group_by.strip())
                breakdown = s.dicts(
                    f"SELECT TOP (20) {gb} AS group_value, MIN({col}) AS min_date, MAX({col}) AS max_date, "
                    f"COUNT_BIG(*) AS rows FROM {ref} GROUP BY {gb} ORDER BY rows DESC"
                )
        return {
            "connection": server.alias, "database": dbn, "table": f"{sch}.{name}",
            "date_column": date_column, "bounds": bounds, "daily_last_14_days": daily,
            "group_by_breakdown": breakdown,
        }
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def compare_row_counts(
    database: str,
    tables: list[str],
    connection: Optional[str] = None,
) -> dict[str, Any]:
    """Conteo aproximado (sys.partitions) de varias tablas en una sola llamada. Cada entrada: 'tabla' o 'esquema.tabla'."""
    def run():
        if not tables:
            raise ValueError("tables no puede estar vacío")
        server = _resolve(connection)
        dbn = _db(server, database)
        results = []
        with db.session(SETTINGS, server, dbn) as s:
            for raw in tables:
                sch, name = _split_name(raw, "dbo")
                try:
                    oid = _object_id(s, sch, name)
                    approx = s.scalar(
                        "SELECT SUM(rows) FROM sys.partitions WHERE object_id = ? AND index_id IN (0,1)", (oid,))
                    results.append({"table": f"{sch}.{name}", "approx_rows": approx, "ok": True})
                except LookupError as e:
                    results.append({"table": raw, "ok": False, "error": str(e)})
        return {"connection": server.alias, "database": dbn, "results": results}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=LOCAL_ONLY)
def query_history(limit: int = 100) -> dict[str, Any]:
    """Últimas consultas ejecutadas por este MCP en la sesión del proceso (sql recortada, filas, duración, error)."""
    lim = _clamp(limit, 100, 100)
    items = qh.list_history(lim)
    return {"ok": True, "count": len(items), "queries": items}


@mcp.tool(annotations=READ_ONLY)
def search_columns(pattern: str, database: Optional[str] = None, schema: Optional[str] = None,
                   limit: int = 200, connection: Optional[str] = None) -> dict[str, Any]:
    """
    Busca columnas por nombre (LIKE; 'cell' => '%cell%') en tablas y vistas. Sin 'database' busca en
    todas las bases de usuario accesibles del servidor.
    """
    def run():
        server = _resolve(connection)
        lim = _clamp(limit, 200, 2000)
        like = like_pattern(pattern)
        found, errors = [], {}
        with db.session(SETTINGS, server) as s:
            for dbn in _databases_for(server, database):
                q = quote_ident(dbn)
                sql = (f"SELECT TOP ({lim + 1 - len(found)}) ? AS database_name, sc.name AS schema_name, "
                       "o.name AS object_name, o.type_desc AS object_type, c.name AS column_name, t.name AS type "
                       f"FROM {q}.sys.columns c JOIN {q}.sys.objects o ON o.object_id = c.object_id "
                       f"JOIN {q}.sys.schemas sc ON sc.schema_id = o.schema_id "
                       f"JOIN {q}.sys.types t ON t.user_type_id = c.user_type_id "
                       "WHERE o.is_ms_shipped = 0 AND o.type IN ('U','V') AND c.name LIKE ?"
                       + (" AND sc.name = ?" if schema else "") + " ORDER BY sc.name, o.name, c.column_id")
                params = (dbn, like) + ((schema,) if schema else ())
                try:
                    found += s.dicts(sql, params)
                except pyodbc.Error as e:
                    errors[dbn] = str(e.args[1]) if len(e.args) > 1 else str(e)
                if len(found) > lim:
                    break
        return {"connection": server.alias, "pattern": like, "count": min(len(found), lim),
                "truncated": len(found) > lim, "columns": found[:lim], "errors": errors or None}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def search_objects(pattern: str, database: Optional[str] = None, object_types: Optional[list[str]] = None,
                   search_definitions: bool = False, limit: int = 200,
                   connection: Optional[str] = None) -> dict[str, Any]:
    """
    Busca objetos por nombre (LIKE) y, con search_definitions=true, también dentro del código SQL de
    vistas, procedimientos, funciones y triggers. Sin 'database' busca en todas las bases de usuario.

    Args:
        object_types: Subconjunto de table, view, procedure, function, trigger, synonym (default todos).
    """
    def run():
        server = _resolve(connection)
        lim = _clamp(limit, 200, 2000)
        like = like_pattern(pattern)
        kinds = object_types or list(OBJECT_TYPES)
        bad = [k for k in kinds if k not in OBJECT_TYPES]
        if bad:
            raise ValueError(f"object_types inválidos: {bad}. Usa {list(OBJECT_TYPES)}")
        codes = ",".join(f"'{c}'" for k in kinds for c in OBJECT_TYPES[k])
        found, errors = [], {}
        with db.session(SETTINGS, server) as s:
            for dbn in _databases_for(server, database):
                q = quote_ident(dbn)
                cond = "o.name LIKE ?" + (" OR m.definition LIKE ?" if search_definitions else "")
                sql = (f"SELECT TOP ({lim + 1 - len(found)}) ? AS database_name, sc.name AS schema_name, o.name, "
                       "o.type_desc, o.modify_date, CASE WHEN o.name LIKE ? THEN 'name' ELSE 'definition' END "
                       f"AS matched_on FROM {q}.sys.objects o JOIN {q}.sys.schemas sc ON sc.schema_id = o.schema_id "
                       f"LEFT JOIN {q}.sys.sql_modules m ON m.object_id = o.object_id "
                       f"WHERE o.is_ms_shipped = 0 AND o.type IN ({codes}) AND ({cond}) ORDER BY sc.name, o.name")
                params = (dbn, like, like) + ((like,) if search_definitions else ())
                try:
                    found += s.dicts(sql, params)
                except pyodbc.Error as e:
                    errors[dbn] = str(e.args[1]) if len(e.args) > 1 else str(e)
                if len(found) > lim:
                    break
        return {"connection": server.alias, "pattern": like, "count": min(len(found), lim),
                "truncated": len(found) > lim, "objects": found[:lim], "errors": errors or None}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def list_routines(database: str, schema: Optional[str] = None, routine_type: Optional[str] = None,
                  pattern: Optional[str] = None, connection: Optional[str] = None) -> dict[str, Any]:
    """Procedimientos y funciones de una base con sus parámetros. routine_type: 'procedure' o 'function'."""
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        if routine_type and routine_type not in ("procedure", "function"):
            raise ValueError("routine_type debe ser 'procedure' o 'function'")
        kinds = [routine_type] if routine_type else ["procedure", "function"]
        codes = ",".join(f"'{c}'" for k in kinds for c in OBJECT_TYPES[k])
        where, params = [f"o.type IN ({codes})", "o.is_ms_shipped = 0"], []
        if schema:
            where.append("SCHEMA_NAME(o.schema_id) = ?")
            params.append(schema)
        if pattern:
            where.append("o.name LIKE ?")
            params.append(like_pattern(pattern))
        sql = ("SELECT TOP (2001) SCHEMA_NAME(o.schema_id) AS schema_name, o.name, o.type_desc, o.create_date, "
               "o.modify_date, STUFF((SELECT ', ' + p.name + ' ' + TYPE_NAME(p.user_type_id) "
               "+ CASE WHEN p.is_output = 1 THEN ' OUTPUT' ELSE '' END FROM sys.parameters p "
               "WHERE p.object_id = o.object_id AND p.parameter_id > 0 ORDER BY p.parameter_id "
               "FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS parameters "
               f"FROM sys.objects o WHERE {' AND '.join(where)} ORDER BY schema_name, o.name")
        with db.session(SETTINGS, server, dbn) as s:
            rows = s.dicts(sql, tuple(params))
        return {"connection": server.alias, "database": dbn, "count": min(len(rows), 2000),
                "truncated": len(rows) > 2000, "routines": rows[:2000]}
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def get_object_definition(name: str, database: str, schema: str = "dbo",
                          connection: Optional[str] = None) -> dict[str, Any]:
    """Código SQL (CREATE ...) de una vista, procedimiento, función o trigger, con sus parámetros."""
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, obj = _split_name(name, schema)
        with db.session(SETTINGS, server, dbn) as s:
            oid = _object_id(s, sch, obj)
            info = s.dicts("SELECT type_desc, create_date, modify_date FROM sys.objects WHERE object_id = ?", (oid,))[0]
            cur = s.cnx.cursor()
            cur.execute("SELECT OBJECT_DEFINITION(?)", (oid,))
            definition = cur.fetchone()[0]
            params = s.dicts("SELECT p.name, TYPE_NAME(p.user_type_id) AS type, p.max_length, p.is_output, "
                             "p.has_default_value FROM sys.parameters p WHERE p.object_id = ? AND p.parameter_id > 0 "
                             "ORDER BY p.parameter_id", (oid,))
        out = {"connection": server.alias, "database": dbn, "object": f"{sch}.{obj}", **info, "parameters": params}
        if definition is None:
            out["definition"] = None
            out["note"] = ("Sin definición: el objeto es una tabla, está cifrado (WITH ENCRYPTION) o falta el "
                           "permiso VIEW DEFINITION. Para tablas usa describe_table.")
        else:
            limit = 100_000
            out["definition_chars"] = len(definition)
            out["definition"] = definition[:limit] + ("\n…[truncado]" if len(definition) > limit else "")
        return out
    return _run(run, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def get_dependencies(name: str, database: str, schema: str = "dbo",
                     connection: Optional[str] = None) -> dict[str, Any]:
    """Dependencias de un objeto: qué objetos usa (uses) y qué objetos lo usan (used_by)."""
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        sch, obj = _split_name(name, schema)
        with db.session(SETTINGS, server, dbn) as s:
            oid = _object_id(s, sch, obj)
            uses = s.dicts(
                "SELECT DISTINCT d.referenced_server_name, d.referenced_database_name, "
                "COALESCE(d.referenced_schema_name, OBJECT_SCHEMA_NAME(d.referenced_id)) AS referenced_schema, "
                "d.referenced_entity_name, o.type_desc FROM sys.sql_expression_dependencies d "
                "LEFT JOIN sys.objects o ON o.object_id = d.referenced_id WHERE d.referencing_id = ?", (oid,))
            used_by = s.dicts(
                "SELECT DISTINCT OBJECT_SCHEMA_NAME(d.referencing_id) AS schema_name, "
                "OBJECT_NAME(d.referencing_id) AS name, o.type_desc FROM sys.sql_expression_dependencies d "
                "JOIN sys.objects o ON o.object_id = d.referencing_id WHERE d.referenced_id = ?", (oid,))
            fk_refs = s.dicts(
                "SELECT fk.name AS fk_name, OBJECT_SCHEMA_NAME(fk.parent_object_id) AS schema_name, "
                "OBJECT_NAME(fk.parent_object_id) AS table_name FROM sys.foreign_keys fk "
                "WHERE fk.referenced_object_id = ?", (oid,))
        return {"connection": server.alias, "database": dbn, "object": f"{sch}.{obj}",
                "uses": uses, "used_by": used_by, "referenced_by_foreign_keys": fk_refs}
    return _run(run, connection=connection, database=database)


# ============================================================
# Consultas
# ============================================================


def _checked(sql: str) -> str:
    err = validate_readonly(sql)
    if err:
        raise PermissionError(err)
    return sql


@mcp.tool(annotations=READ_ONLY)
def query(sql: str, database: Optional[str] = None, max_rows: Optional[int] = None,
          page_size: Optional[int] = None, cursor: Optional[str] = None,
          max_cell_chars: Optional[int] = None,
          connection: Optional[str] = None) -> dict[str, Any]:
    """
    Ejecuta UNA consulta T-SQL de solo lectura (SELECT o WITH ... SELECT) y devuelve columnas + filas.
    Paginación con cursor de servidor: si hay más filas, devuelve next_cursor (TTL 10 min, máx 5 abiertos).
    Si la respuesta supera MSSQL_MAX_RESPONSE_CHARS, el sobrante queda en rows_buffered (no se pierden filas).
    Campos: rows_returned, rows_buffered, has_more (y truncated/next_cursor por compatibilidad).
    Usa TOP (n) u OFFSET/FETCH, no LIMIT. Admite FOR JSON/FOR XML, CTEs, funciones de ventana,
    DMVs y nombres de 3 partes [db].[schema].[tabla].

    Args:
        sql: Consulta (ignorado si se pasa cursor).
        database: Base en la que se ejecuta (default del login).
        max_rows: Tope de filas por página (default y tope MSSQL_MAX_ROWS, default 5000).
        page_size: Filas por página (default = max_rows).
        cursor: Continúa una consulta previa (valor de next_cursor).
        max_cell_chars: Recorte de celdas (default MSSQL_MAX_CELL_CHARS=4000, max 20000).
        connection: Alias del servidor (sql01 / sql05).
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        n = _clamp(max_rows, SETTINGS.max_rows, SETTINGS.max_rows)
        ps = _clamp(page_size, n, SETTINGS.max_rows) if page_size is not None else n
        mc = _clamp(max_cell_chars, SETTINGS.max_cell_chars, 20_000)
        db_label = dbn or "(default del login)"
        envelope = {"ok": True, "connection": server.alias, "database": db_label}
        if cursor and cursor.strip():
            out = db.fetch_query_page(cursor.strip(), ps)
        else:
            _checked(sql)
            out = db.start_query_cursor(
                SETTINGS, server, dbn, sql, ps, mc, SETTINGS.max_response_chars,
                response_envelope=envelope,
                cursor_max_rows=SETTINGS.cursor_max_rows,
            )
        logger.info(
            f"{server.alias}/{dbn} query ok ({out['elapsed_ms']} ms, rows={out['row_count']}, "
            f"more={bool(out.get('next_cursor'))})")
        qh.record(
            tool="query", sql=sql or f"[cursor {cursor}]", connection=server.alias, database=dbn,
            row_count=out.get("row_count"), elapsed_ms=out.get("elapsed_ms"),
        )
        return {"connection": server.alias, "database": dbn or "(default del login)", **out}
    return _run(run, sql=sql, connection=connection, database=database)


@mcp.tool(annotations=READ_ONLY)
def fetch_more(cursor: str, page_size: Optional[int] = None,
               connection: Optional[str] = None) -> dict[str, Any]:
    """
    Devuelve la siguiente página de una consulta paginada (next_cursor devuelto por query).

    Args:
        cursor: Identificador next_cursor.
        page_size: Filas a traer (default MSSQL_MAX_ROWS).
        connection: Informativo; el cursor ya está ligado al servidor/base originales.
    """
    def run():
        ps = _clamp(page_size, SETTINGS.max_rows, SETTINGS.max_rows)
        out = db.fetch_query_page(cursor.strip(), ps)
        qh.record(
            tool="fetch_more", sql=f"[cursor {cursor}]", connection=connection,
            row_count=out.get("row_count"), elapsed_ms=out.get("elapsed_ms"),
        )
        return {"connection": connection, **out}
    return _run(run, connection=connection)


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
    sin pasar por el LLM. Destino local: MSSQL_EXPORT_DIR con timestamp, o ruta indicada;
    s3://bucket/clave/... sube con boto3 (credenciales access_key/secret_key del .env compartido).
    Tope MSSQL_EXPORT_MAX_ROWS (default 50 millones). Mismo validador de solo lectura que query.
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        _checked(sql)
        cnx = export_mod.open_export_connection(SETTINGS, server, dbn)
        try:
            out = export_mod.export_query_result(SETTINGS, cnx, sql, fmt=format, destination=destination)
        finally:
            try:
                cnx.rollback()
            except Exception:
                pass
            try:
                cnx.close()
            except Exception:
                pass
        qh.record(
            tool="export_query", sql=sql, connection=server.alias, database=dbn,
            row_count=out.get("rows"), elapsed_ms=out.get("elapsed_ms"),
        )
        return {"connection": server.alias, "database": dbn or "(default del login)", **out}
    return _run(run, sql=sql, connection=connection, database=database)


def _query_one(server: Server, sql: str, database: Optional[str], n: int) -> dict:
    try:
        dbn = _db(server, database)
        with db.session(SETTINGS, server, dbn) as s:
            return {"ok": True, "database": dbn or "(default del login)", **s.table(sql, max_rows=n)}
    except pyodbc.Error as e:
        return {"ok": False, "error": str(e.args[1]) if len(e.args) > 1 else str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool(annotations=READ_ONLY)
def query_all(sql: str, database: Optional[str] = None, max_rows: int = 200,
              connections: Optional[list[str]] = None) -> dict[str, Any]:
    """
    Ejecuta el mismo SELECT en varios servidores en paralelo (default: todos). Resultados por alias;
    un servidor caído o sin la base indicada no aborta a los demás.
    """
    def run():
        _checked(sql)
        servers = [_resolve(c) for c in connections] if connections else list(SETTINGS.servers.values())
        n = _clamp(max_rows, 200, SETTINGS.max_rows)
        with ThreadPoolExecutor(max_workers=len(servers) or 1) as ex:
            futures = {s.alias: ex.submit(_query_one, s, sql, database, n) for s in servers}
            results = {alias: f.result() for alias, f in futures.items()}
        return {"all_ok": all(r["ok"] for r in results.values()), "results": results}
    return _run(run, sql=sql, database=database)


_SP = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"


def summarize_plan(xml_text: str, top: int = 15) -> dict:
    root = ET.fromstring(xml_text)
    stmts = []
    for st in root.iter(f"{_SP}StmtSimple"):
        stmts.append({
            "statement": (st.get("StatementText") or "")[:300],
            "estimated_cost": float(st.get("StatementSubTreeCost") or 0),
            "estimated_rows": float(st.get("StatementEstRows") or 0),
            "optimization_level": st.get("StatementOptmLevel"),
        })
    ops = []
    for op in root.iter(f"{_SP}RelOp"):
        obj = op.find(f".//{_SP}Object")
        target = None
        if obj is not None:
            target = ".".join(p for p in (obj.get("Database"), obj.get("Schema"), obj.get("Table"),
                                          obj.get("Index")) if p)
        ops.append({
            "node_id": int(op.get("NodeId") or 0),
            "physical_op": op.get("PhysicalOp"), "logical_op": op.get("LogicalOp"),
            "estimated_subtree_cost": float(op.get("EstimatedTotalSubtreeCost") or 0),
            "estimated_rows": float(op.get("EstimateRows") or 0),
            "object": target,
        })
    ops.sort(key=lambda o: o["estimated_subtree_cost"], reverse=True)
    missing = []
    for grp in root.iter(f"{_SP}MissingIndexGroup"):
        for mi in grp.iter(f"{_SP}MissingIndex"):
            cols = {cg.get("Usage"): [c.get("Name") for c in cg.iter(f"{_SP}Column")]
                    for cg in mi.iter(f"{_SP}ColumnGroup")}
            missing.append({"impact": float(grp.get("Impact") or 0),
                            "table": ".".join(p for p in (mi.get("Database"), mi.get("Schema"), mi.get("Table")) if p),
                            **{k.lower(): v for k, v in cols.items()}})
    warnings = sorted({w.tag.replace(_SP, "") for ws in root.iter(f"{_SP}Warnings") for w in ws})
    return {"statements": stmts, "top_operators": ops[:top], "operator_count": len(ops),
            "missing_indexes": missing, "warnings": warnings}


@mcp.tool(annotations=READ_ONLY)
def explain_query(sql: str, database: Optional[str] = None, include_xml: bool = False,
                  connection: Optional[str] = None) -> dict[str, Any]:
    """
    Plan de ejecución ESTIMADO (SHOWPLAN_XML) sin ejecutar la consulta: coste, filas estimadas,
    operadores más caros, índices faltantes y warnings. include_xml=true añade el XML (recortado).
    Requiere permiso SHOWPLAN en la base.
    """
    def run():
        server = _resolve(connection)
        dbn = _db(server, database)
        _checked(sql)
        with db.session(SETTINGS, server, dbn) as s:
            cur = s.cnx.cursor()
            cur.execute("SET SHOWPLAN_XML ON")
            try:
                cur.execute(sql)
                plans = []
                while True:
                    if cur.description is not None:
                        plans += [r[0] for r in cur.fetchall()]
                    if not cur.nextset():
                        break
            finally:
                cur.execute("SET SHOWPLAN_XML OFF")
        if not plans:
            raise RuntimeError("SQL Server no devolvió plan")
        summary = summarize_plan(plans[0])
        out = {
            "connection": server.alias,
            "database": dbn or "(default del login)",
            **summary,
            "plan_summary": {
                "statements": summary.get("statements"),
                "top_operators": summary.get("top_operators"),
                "missing_indexes": summary.get("missing_indexes"),
                "warnings": summary.get("warnings"),
            },
            "plan_xml": plans[0][:50_000],
        }
        if include_xml:
            out["plan_xml_full_requested"] = True
        return out
    return _run(run, sql=sql, connection=connection, database=database)


# ============================================================
# Estado del servidor
# ============================================================


@mcp.tool(annotations=READ_ONLY)
def server_activity(connection: Optional[str] = None, include_sleeping: bool = False) -> dict[str, Any]:
    """
    Peticiones en ejecución (SQL, duración, CPU, waits, bloqueos) y resumen de sesiones por login/base.
    Requiere VIEW SERVER PERFORMANCE STATE (si falta, devuelve ok=false explicándolo).
    """
    def run():
        server = _resolve(connection)
        with db.session(SETTINGS, server) as s:
            has_state = _has_server_state(s)
            if not has_state:
                raise PermissionDeniedError(
                    f"El login no tiene VIEW SERVER PERFORMANCE STATE en '{server.alias}': no se pueden leer "
                    "sys.dm_exec_requests/sessions. Pídelo al DBA (GRANT VIEW SERVER PERFORMANCE STATE).",
                    "VIEW SERVER PERFORMANCE STATE",
                )
            requests = s.dicts(
                "SELECT r.session_id, s.login_name, s.host_name, s.program_name, DB_NAME(r.database_id) AS database_name, "
                "r.status, r.command, r.start_time, r.total_elapsed_time / 1000.0 AS elapsed_s, r.cpu_time AS cpu_ms, "
                "r.logical_reads, r.wait_type, r.wait_time AS wait_ms, r.blocking_session_id, "
                "SUBSTRING(t.text, r.statement_start_offset / 2 + 1, (CASE WHEN r.statement_end_offset = -1 "
                "THEN DATALENGTH(t.text) ELSE r.statement_end_offset END - r.statement_start_offset) / 2 + 1) "
                "AS statement FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id "
                "OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t WHERE r.session_id <> @@SPID AND s.is_user_process = 1 "
                + ("" if include_sleeping else "AND r.status <> 'sleeping' ") + "ORDER BY r.total_elapsed_time DESC")
            sessions = s.dicts(
                "SELECT s.login_name, DB_NAME(s.database_id) AS database_name, s.status, COUNT(*) AS sessions "
                "FROM sys.dm_exec_sessions s WHERE s.is_user_process = 1 "
                "GROUP BY s.login_name, DB_NAME(s.database_id), s.status ORDER BY sessions DESC")
        blocked = [r for r in requests if r.get("blocking_session_id")]
        return {"connection": server.alias, "request_count": len(requests), "blocked_count": len(blocked),
                "requests": requests, "sessions_summary": sessions}
    return _run(run, connection=connection)


_JOB_STATUS = {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "In progress"}


def _agent_dt(d: Any, t: Any) -> Optional[str]:
    try:
        d, t = int(d or 0), int(t or 0)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}T{t // 10000:02d}:{t // 100 % 100:02d}:{t % 100:02d}"


@mcp.tool(annotations=READ_ONLY)
def list_jobs(connection: Optional[str] = None, pattern: Optional[str] = None) -> dict[str, Any]:
    """Jobs del SQL Agent: habilitado, categoría, último resultado/duración y próxima ejecución (requiere permisos en msdb)."""
    def run():
        server = _resolve(connection)
        where, params = "", ()
        if pattern:
            where, params = "WHERE j.name LIKE ? ", (like_pattern(pattern),)
        with db.session(SETTINGS, server) as s:
            try:
                rows = s.dicts(
                    "SELECT j.name, j.enabled, c.name AS category, j.date_modified, h.run_status, h.run_date, "
                    "h.run_time, h.run_duration, LEFT(h.message, 500) AS last_message, "
                    "(SELECT MIN(CAST(js.next_run_date AS bigint) * 1000000 + js.next_run_time) "
                    " FROM msdb.dbo.sysjobschedules js WHERE js.job_id = j.job_id AND js.next_run_date > 0) AS next_run "
                    "FROM msdb.dbo.sysjobs j LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id "
                    "OUTER APPLY (SELECT TOP 1 * FROM msdb.dbo.sysjobhistory hh WHERE hh.job_id = j.job_id "
                    "AND hh.step_id = 0 ORDER BY hh.instance_id DESC) h " + where + "ORDER BY j.name", params)
            except pyodbc.Error as e:
                msg = str(e.args[1]) if len(e.args) > 1 else str(e)
                raise PermissionDeniedError(
                    f"Sin acceso a los jobs de msdb (se necesita SQLAgentReaderRole): {msg}",
                    "msdb SQLAgentReaderRole",
                )
        jobs = []
        for r in rows:
            nr = r.pop("next_run")
            dur = int(r.pop("run_duration") or 0)
            jobs.append({
                **{k: v for k, v in r.items() if k not in ("run_status", "run_date", "run_time")},
                "enabled": bool(r["enabled"]),
                "last_status": _JOB_STATUS.get(r["run_status"]) if r["run_status"] is not None else None,
                "last_run": _agent_dt(r["run_date"], r["run_time"]),
                "last_duration_s": dur // 10000 * 3600 + dur // 100 % 100 * 60 + dur % 100,
                "next_run": _agent_dt(int(nr) // 1000000, int(nr) % 1000000) if nr else None,
            })
        return {"connection": server.alias, "count": len(jobs), "jobs": jobs}
    return _run(run, connection=connection)


# ============================================================
# CLI
# ============================================================


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        _print({"ok": True, "tool_count": len(names), "tools": names})
        return 0 if names else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="OPSDB SQL Server MCP (solo lectura)")
    parser.add_argument("--list", action="store_true", help="Lista conexiones y sale")
    parser.add_argument("--test", nargs="*", metavar="ALIAS", help="Prueba conexión(es) y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía cliente MCP")
    args = parser.parse_args()

    if args.list:
        _print(list_connections())
        return 0
    if args.test is not None:
        if args.test:
            outs = [test_connection(connection=a) for a in args.test]
        else:
            outs = [test_connection()]
        _print(outs)
        return 0 if all(o.get("ok") and o.get("all_ok") for o in outs) else 1
    if args.handshake:
        return asyncio.run(_handshake())

    logger.info(f"sqlserver-readonly-mcp v{__version__} — env: {[str(p) for p in LOADED_ENV_FILES]}")
    for s in SETTINGS.servers.values():
        logger.info(f"  [{s.alias}] {s.address}")
    if SETTINGS.problems():
        logger.warning(f"Configuración incompleta: faltan {SETTINGS.problems()}")
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
