"""Conexiones Oracle (python-oracledb thin) de solo lectura contra collectors OSS FM."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import oracledb

from oracle_alarms_readonly_mcp.chunking import ChunkingError, plan_rc8_chunks
from oracle_alarms_readonly_mcp.config import (
    ALL_RCS,
    NETWORK_TZ_LABEL,
    RC_PORTS,
    Settings,
    dsn_for,
    get_settings,
    resolve_rcs,
)
from oracle_alarms_readonly_mcp.pool import acquire_connection, release_connection
from oracle_alarms_readonly_mcp.formatting import format_network_datetime, rows_as_dicts, serialize_value
from oracle_alarms_readonly_mcp.safety import (
    SqlSafetyError,
    ensure_row_limit,
    validate_readonly_sql,
)

logger = logging.getLogger("oracle-alarms-readonly-mcp")

# CLOB/NCLOB como str y BLOB como bytes (en vez de objetos LOB no serializables).
oracledb.defaults.fetch_lobs = False

# Tope de espera para abrir el socket TCP (evita bloqueos largos con RCs caídos).
_TCP_CONNECT_TIMEOUT_S = 10.0


@dataclass
class QueryResult:
    """Resultado de una consulta a un RC (éxito o fallo)."""

    rc: str
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    execution_time_s: float = 0.0
    truncated: bool = False
    error: str | None = None
    error_kind: str | None = None  # "connection" | "query" | "safety" | "chunking"
    host: str | None = None
    port: int | None = None
    dsn: str | None = None
    max_rows: int | None = None
    chunked: bool = False
    chunk_count: int = 0
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Dict compacto: metadatos primero, filas al final; detalles de red solo si falla."""
        d: dict[str, Any] = {
            "ok": self.ok,
            "rc": self.rc,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "execution_time_s": round(self.execution_time_s, 3),
        }
        if self.max_rows is not None:
            d["max_rows"] = self.max_rows
        if self.chunked:
            d["chunked"] = True
            d["chunk_count"] = self.chunk_count
        if self.message:
            d["message"] = self.message
        if not self.ok:
            d["error"] = self.error
            d["error_kind"] = self.error_kind
            d["dsn"] = self.dsn
            return d
        d["columns"] = self.columns
        d["rows"] = self.rows
        return d


@contextmanager
def connect_readonly(
    rc: str,
    settings: Settings,
    timeout_s: int | None = None,
) -> Iterator[oracledb.Connection]:
    """Conexión del pool por RC, SET TRANSACTION READ ONLY, nunca hace commit."""
    settings.validate_credentials()
    name = resolve_rcs(rc)[0]
    timeout = settings.effective_timeout(timeout_s)
    conn: oracledb.Connection | None = None
    try:
        conn = acquire_connection(name, settings)
        timeout_ms = timeout * 1000
        try:
            conn.call_timeout = timeout_ms
        except Exception:
            conn.callTimeout = timeout_ms  # type: ignore[attr-defined]

        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                release_connection(name, settings, conn)
            except Exception as close_err:
                logger.debug("Error devolviendo conexión %s: %s", name, close_err)


def _meta(rc: str, settings: Settings) -> tuple[str, int, str]:
    name = resolve_rcs(rc)[0]
    port = RC_PORTS[name]
    return settings.host, port, dsn_for(name, settings)


def _unique_columns(names: list[str]) -> list[str]:
    """Evita que columnas homónimas (a.ID, b.ID) se pisen al convertir filas a dict."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name in seen:
            seen[name] += 1
            out.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 1
            out.append(name)
    return out


def _fetch_limited(
    conn: oracledb.Connection,
    sql_text: str,
    max_rows: int,
) -> tuple[list[str], list[tuple], bool]:
    """Ejecuta SQL y trae hasta max_rows+1 para saber si hubo truncado."""
    with conn.cursor() as cur:
        cur.execute(sql_text)
        if cur.description is None:
            return [], [], False
        columns = _unique_columns([d[0] for d in cur.description])
        raw = cur.fetchmany(max_rows + 1)
        truncated = len(raw) > max_rows
        if truncated:
            raw = raw[:max_rows]
        return columns, raw, truncated


def test_connection(rc: str, settings: Settings | None = None) -> dict[str, Any]:
    """Prueba un RC: versión Oracle + hora del servidor (sin conversión de zona)."""
    settings = settings or get_settings()
    name = resolve_rcs(rc)[0]
    host, port, dsn = _meta(name, settings)
    started = time.perf_counter()
    try:
        with connect_readonly(name, settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT banner FROM v$version WHERE banner LIKE 'Oracle%'"
                )
                row = cur.fetchone()
                version = row[0] if row else None
                if not version:
                    cur.execute(
                        "SELECT version_full FROM product_component_version "
                        "WHERE product LIKE 'Oracle%'"
                    )
                    row = cur.fetchone()
                    version = row[0] if row else "desconocida"
                cur.execute("SELECT SYSDATE FROM dual")
                sysdate = cur.fetchone()[0]
        elapsed = time.perf_counter() - started
        return {
            "ok": True,
            "status": "connected",
            "rc": name,
            "host": host,
            "port": port,
            "dsn": dsn,
            "oracle_version": serialize_value(version) if isinstance(version, str) else str(version),
            "server_time": format_network_datetime(sysdate)
            if hasattr(sysdate, "strftime")
            else serialize_value(sysdate),
            "network_tz": NETWORK_TZ_LABEL,
            "execution_time_s": round(elapsed, 3),
        }
    except Exception as e:
        elapsed = time.perf_counter() - started
        logger.warning("test_connection falló para %s: %s", name, e)
        return {
            "ok": False,
            "status": "error",
            "rc": name,
            "host": host,
            "port": port,
            "dsn": dsn,
            "error": str(e),
            "error_kind": "connection",
            "execution_time_s": round(elapsed, 3),
        }


def test_connections(
    rcs: list[str] | str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    names = resolve_rcs(rcs)
    # En paralelo: un RC caído no debe sumar su timeout al de los demás.
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        results = list(pool.map(lambda rc: test_connection(rc, settings), names))
    ok_n = sum(1 for r in results if r.get("ok"))
    return {
        "summary": f"{ok_n}/{len(results)} RCs conectados",
        "ok_count": ok_n,
        "total": len(results),
        "results": results,
    }


def describe_table(
    rc: str,
    owner: str,
    table: str,
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> QueryResult:
    """Columnas vía ALL_TAB_COLUMNS (fallback USER_TAB_COLUMNS)."""
    settings = settings or get_settings()
    name = resolve_rcs(rc)[0]
    host, port, dsn = _meta(name, settings)
    owner_u = owner.strip().upper()
    table_u = table.strip().upper()
    started = time.perf_counter()

    sql_all = (
        "SELECT COLUMN_ID, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, "
        "DATA_SCALE, NULLABLE, DATA_DEFAULT "
        "FROM ALL_TAB_COLUMNS "
        "WHERE OWNER = :owner AND TABLE_NAME = :tbl "
        "ORDER BY COLUMN_ID"
    )
    sql_user = (
        "SELECT COLUMN_ID, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, "
        "DATA_SCALE, NULLABLE, DATA_DEFAULT "
        "FROM USER_TAB_COLUMNS "
        "WHERE TABLE_NAME = :tbl "
        "ORDER BY COLUMN_ID"
    )

    try:
        with connect_readonly(name, settings, timeout_s=timeout_s) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_all, {"owner": owner_u, "tbl": table_u})
                raw = cur.fetchall()
                columns = [d[0] for d in cur.description] if cur.description else []
                source = "ALL_TAB_COLUMNS"
                if not raw:
                    cur.execute(sql_user, {"tbl": table_u})
                    raw = cur.fetchall()
                    columns = [d[0] for d in cur.description] if cur.description else []
                    source = "USER_TAB_COLUMNS"
        elapsed = time.perf_counter() - started
        rows = rows_as_dicts(columns, list(raw))
        msg = None
        if not rows:
            msg = (
                f"Tabla {owner_u}.{table_u} no encontrada en {source} "
                f"(0 columnas). Verificá owner/table o privilegios."
            )
        return QueryResult(
            rc=name,
            ok=True,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            execution_time_s=elapsed,
            truncated=False,
            host=host,
            port=port,
            dsn=dsn,
            message=msg,
        )
    except Exception as e:
        elapsed = time.perf_counter() - started
        logger.warning("describe_table falló %s.%s @ %s: %s", owner_u, table_u, name, e)
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=elapsed,
            error=str(e),
            error_kind="query",
            host=host,
            port=port,
            dsn=dsn,
        )


def _execute_one_sql(
    conn: oracledb.Connection,
    sql_text: str,
    max_rows: int,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    validated = validate_readonly_sql(sql_text)
    executable, wrapped = ensure_row_limit(
        validated.cleaned, max_rows, validated.has_row_limit
    )
    try:
        columns, raw, truncated = _fetch_limited(conn, executable, max_rows)
    except oracledb.DatabaseError as e:
        # SELECT * sobre una vista inline con columnas duplicadas (p.ej. joins con
        # a.ID, b.ID) da ORA-00918; reintentar sin envoltorio (fetchmany sigue topando).
        if not wrapped or "ORA-00918" not in str(e):
            raise
        unwrapped = validated.cleaned.strip().rstrip(";").strip()
        columns, raw, truncated = _fetch_limited(conn, unwrapped, max_rows)
    rows = rows_as_dicts(columns, raw)
    return columns, rows, truncated


def execute_query(
    rc: str,
    sql: str,
    max_rows: int | None = None,
    timeout_s: int | None = None,
    settings: Settings | None = None,
) -> QueryResult:
    """Ejecuta SELECT/WITH validado en un RC. Distingue 0 filas vs fallo."""
    settings = settings or get_settings()
    name = resolve_rcs(rc)[0]
    host, port, dsn = _meta(name, settings)
    limit = settings.effective_rows(max_rows)
    started = time.perf_counter()

    try:
        validated = validate_readonly_sql(sql)
    except SqlSafetyError as e:
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=time.perf_counter() - started,
            error=str(e),
            error_kind="safety",
            host=host,
            port=port,
            dsn=dsn,
            max_rows=limit,
        )

    try:
        chunk_plan = plan_rc8_chunks(validated.cleaned, name, settings.rc8_chunk_days)
    except ChunkingError as e:
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=time.perf_counter() - started,
            error=str(e),
            error_kind="chunking",
            host=host,
            port=port,
            dsn=dsn,
            max_rows=limit,
        )

    try:
        with connect_readonly(name, settings, timeout_s=timeout_s) as conn:
            if not chunk_plan:
                columns, rows, truncated = _execute_one_sql(
                    conn, validated.cleaned, limit
                )
                elapsed = time.perf_counter() - started
                msg = "Consultado OK, 0 filas" if not rows else None
                return QueryResult(
                    rc=name,
                    ok=True,
                    columns=columns,
                    rows=rows,
                    row_count=len(rows),
                    execution_time_s=elapsed,
                    truncated=truncated,
                    host=host,
                    port=port,
                    dsn=dsn,
                    max_rows=limit,
                    chunked=False,
                    chunk_count=0,
                    message=msg,
                )

            # Troceo rc8: concatenar hasta max_rows
            all_rows: list[dict[str, Any]] = []
            columns: list[str] = []
            truncated = False
            for chunk in chunk_plan:
                remaining = limit - len(all_rows)
                if remaining <= 0:
                    truncated = True
                    break
                cols, rows, chunk_trunc = _execute_one_sql(conn, chunk.sql, remaining)
                if cols and not columns:
                    columns = cols
                all_rows.extend(rows)
                if chunk_trunc or len(all_rows) >= limit:
                    truncated = True
                    all_rows = all_rows[:limit]
                    break

            elapsed = time.perf_counter() - started
            msg = (
                f"rc8: ventana ALARM_TIME troceada en {len(chunk_plan)} tramos "
                f"de ~{settings.rc8_chunk_days} día(s)"
            )
            if not all_rows:
                msg += "; 0 filas en total"
            return QueryResult(
                rc=name,
                ok=True,
                columns=columns,
                rows=all_rows,
                row_count=len(all_rows),
                execution_time_s=elapsed,
                truncated=truncated,
                host=host,
                port=port,
                dsn=dsn,
                max_rows=limit,
                chunked=True,
                chunk_count=len(chunk_plan),
                message=msg,
            )
    except oracledb.Error as e:
        elapsed = time.perf_counter() - started
        logger.warning("Error Oracle en query @ %s: %s", name, e)
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=elapsed,
            error=f"Oracle error: {e}",
            error_kind="query",
            host=host,
            port=port,
            dsn=dsn,
            max_rows=limit,
        )
    except Exception as e:
        elapsed = time.perf_counter() - started
        # Fallos de red / auth suelen aparecer antes de ejecutar
        kind = "connection" if _looks_like_connection_error(e) else "query"
        logger.warning("Fallo %s en query @ %s: %s", kind, name, e)
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=elapsed,
            error=str(e),
            error_kind=kind,
            host=host,
            port=port,
            dsn=dsn,
            max_rows=limit,
        )


def _looks_like_connection_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "could not connect",
        "failed to connect",
        "timed out",
        "timeout",
        "listener",
        "tns",
        "network",
        "refused",
        "ora-12170",
        "ora-12541",
        "ora-12514",
        "ora-01017",
        "ora-28000",
    )
    return any(n in text for n in needles)


def execute_query_many(
    rcs: list[str] | str,
    sql: str,
    max_rows: int | None = None,
    timeout_s: int | None = None,
    settings: Settings | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Consulta concurrente a varios RCs. Un fallo no aborta a los hermanos."""
    settings = settings or get_settings()
    names = resolve_rcs(rcs)
    workers = max(1, min(len(names), max_workers or len(names)))

    results: dict[str, QueryResult] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                execute_query, rc, sql, max_rows, timeout_s, settings
            ): rc
            for rc in names
        }
        for fut in as_completed(futures):
            rc = futures[fut]
            try:
                results[rc] = fut.result()
            except Exception as e:
                host, port, dsn = _meta(rc, settings)
                results[rc] = QueryResult(
                    rc=rc,
                    ok=False,
                    error=str(e),
                    error_kind="query",
                    host=host,
                    port=port,
                    dsn=dsn,
                )

    ordered = [results[rc] for rc in names]
    ok_list = [r for r in ordered if r.ok]
    zero = [r.rc for r in ok_list if r.row_count == 0]
    failed = [r.rc for r in ordered if not r.ok]
    with_rows = [r.rc for r in ok_list if r.row_count > 0]

    return {
        "ok": len(ok_list) > 0,
        "sql_preview": sql.strip()[:240],
        "rcs_requested": names,
        "ok_count": len(ok_list),
        "failed_count": len(failed),
        "rcs_with_rows": with_rows,
        "rcs_zero_rows": zero,
        "rcs_failed": failed,
        "results": [r.to_dict() for r in ordered],
    }


def list_tables(
    rc: str,
    owner: str | None = None,
    search: str | None = None,
    settings: Settings | None = None,
    timeout_s: int | None = None,
    max_rows: int | None = None,
) -> QueryResult:
    """Tablas y vistas accesibles (ALL_TABLES ∪ ALL_VIEWS)."""
    settings = settings or get_settings()
    name = resolve_rcs(rc)[0]
    host, port, dsn = _meta(name, settings)
    limit = settings.effective_rows(max_rows)
    started = time.perf_counter()

    owner_u = owner.strip().upper() if owner and owner.strip() else None
    search_u = search.strip().upper() if search and search.strip() else None
    owner_sql = f" AND OWNER = '{owner_u}'" if owner_u else ""
    if search_u:
        pat = _escape_sql_literal(search_u)
        tbl_search = f" AND TABLE_NAME LIKE '%{pat}%' ESCAPE '\\'"
        view_search = f" AND VIEW_NAME LIKE '%{pat}%' ESCAPE '\\'"
    else:
        tbl_search = ""
        view_search = ""

    sql = (
        "SELECT * FROM ("
        "SELECT OWNER, TABLE_NAME, 'TABLE' AS OBJECT_TYPE FROM ALL_TABLES WHERE 1=1"
        f"{owner_sql}{tbl_search} "
        "UNION ALL "
        "SELECT OWNER, VIEW_NAME AS TABLE_NAME, 'VIEW' AS OBJECT_TYPE FROM ALL_VIEWS WHERE 1=1"
        f"{owner_sql}{view_search}"
        ") ORDER BY OWNER, TABLE_NAME"
    )
    wrapped = f"SELECT * FROM ({sql}) q WHERE ROWNUM <= {limit + 1}"

    try:
        with connect_readonly(name, settings, timeout_s=timeout_s) as conn:
            with conn.cursor() as cur:
                cur.execute(wrapped)
                raw = cur.fetchall()
                columns = [d[0] for d in cur.description] if cur.description else []
        truncated = len(raw) > limit
        if truncated:
            raw = raw[:limit]
        rows = rows_as_dicts(columns, list(raw))
        elapsed = time.perf_counter() - started
        return QueryResult(
            rc=name,
            ok=True,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            execution_time_s=elapsed,
            truncated=truncated,
            host=host,
            port=port,
            dsn=dsn,
            max_rows=limit,
        )
    except Exception as e:
        elapsed = time.perf_counter() - started
        logger.warning("list_tables falló @ %s: %s", name, e)
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=elapsed,
            error=str(e),
            error_kind="query",
            host=host,
            port=port,
            dsn=dsn,
            max_rows=limit,
        )


def _escape_sql_literal(text: str) -> str:
    return text.replace("'", "''").replace("\\", "\\\\")


def explain_query(
    rc: str,
    sql: str,
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> QueryResult:
    """EXPLAIN PLAN + DBMS_XPLAN.DISPLAY resumido (solo lectura)."""
    settings = settings or get_settings()
    name = resolve_rcs(rc)[0]
    host, port, dsn = _meta(name, settings)
    started = time.perf_counter()
    try:
        validated = validate_readonly_sql(sql)
    except SqlSafetyError as e:
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=time.perf_counter() - started,
            error=str(e),
            error_kind="safety",
            host=host,
            port=port,
            dsn=dsn,
        )

    inner = validated.cleaned.strip().rstrip(";").strip()
    plan_stmt = f"EXPLAIN PLAN FOR {inner}"
    display_sql = (
        "SELECT PLAN_TABLE_OUTPUT FROM TABLE("
        "DBMS_XPLAN.DISPLAY(NULL, NULL, 'TYPICAL +PEEKED_BINDS'))"
    )
    lines: list[str] = []
    try:
        # EXPLAIN PLAN escribe en PLAN_TABLE: no usar SET TRANSACTION READ ONLY.
        conn = acquire_connection(name, settings)
        timeout = settings.effective_timeout(timeout_s)
        timeout_ms = timeout * 1000
        try:
            conn.call_timeout = timeout_ms
        except Exception:
            conn.callTimeout = timeout_ms  # type: ignore[attr-defined]
        try:
            with conn.cursor() as cur:
                cur.execute(plan_stmt)
                cur.execute(display_sql)
                for row in cur.fetchall():
                    if row and row[0]:
                        lines.append(str(row[0]).rstrip())
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            release_connection(name, settings, conn)
        elapsed = time.perf_counter() - started
        # Resumen: primeras ~40 líneas + conteo total
        max_lines = 40
        summary = lines[:max_lines]
        truncated_plan = len(lines) > max_lines
        rows = [{"LINE_NO": i + 1, "PLAN_LINE": ln} for i, ln in enumerate(summary)]
        if truncated_plan:
            rows.append(
                {
                    "LINE_NO": max_lines + 1,
                    "PLAN_LINE": f"... [{len(lines) - max_lines} líneas más omitidas]",
                }
            )
        return QueryResult(
            rc=name,
            ok=True,
            columns=["LINE_NO", "PLAN_LINE"],
            rows=rows,
            row_count=len(rows),
            execution_time_s=elapsed,
            truncated=truncated_plan,
            host=host,
            port=port,
            dsn=dsn,
            message=f"Plan con {len(lines)} líneas (mostradas {len(summary)})",
        )
    except Exception as e:
        elapsed = time.perf_counter() - started
        logger.warning("explain_query falló @ %s: %s", name, e)
        return QueryResult(
            rc=name,
            ok=False,
            execution_time_s=elapsed,
            error=str(e),
            error_kind="query",
            host=host,
            port=port,
            dsn=dsn,
        )


def list_rc_info(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    items = []
    for name in ALL_RCS:
        items.append(
            {
                "rc": name,
                "host": settings.host,
                "port": RC_PORTS[name],
                "service": settings.service,
                "dsn": dsn_for(name, settings),
            }
        )
    return items
