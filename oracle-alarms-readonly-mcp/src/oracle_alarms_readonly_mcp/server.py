"""Servidor MCP de solo lectura para Oracle FM (OSS) — 6 collectors regionales."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from oracle_alarms_readonly_mcp import __version__
from oracle_alarms_readonly_mcp.alarms import (
    active_alarms_summary as alarms_active_summary,
)
from oracle_alarms_readonly_mcp.alarms import (
    alarm_history as alarms_history,
)
from oracle_alarms_readonly_mcp.alarms import (
    alarm_timeline as alarms_timeline,
)
from oracle_alarms_readonly_mcp.alarms import (
    top_alarms as alarms_top,
)
from oracle_alarms_readonly_mcp.config import (
    ALL_RCS,
    ENV_FILE,
    NETWORK_TZ_LABEL,
    PER_CALL_MAX_ROWS,
    PER_CALL_MAX_TIMEOUT_S,
    RC_PORTS,
    Settings,
    get_settings,
    resolve_rcs,
)
from oracle_alarms_readonly_mcp.export import export_query as oracle_export_query
from oracle_alarms_readonly_mcp.formatting import fit_payload, to_compact_json
from oracle_alarms_readonly_mcp.oracle import (
    describe_table as oracle_describe_table,
)
from oracle_alarms_readonly_mcp.oracle import (
    execute_query,
    execute_query_many,
    explain_query as oracle_explain_query,
    list_rc_info,
    list_tables as oracle_list_tables,
    test_connections,
)
from oracle_alarms_readonly_mcp.oracle import (
    test_connection as oracle_test_connection,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("oracle-alarms-readonly-mcp")

INSTRUCTIONS = (
    "Acceso de SOLO LECTURA a las bases Oracle de alarmas OSS FM (TelcoVendorB) de 6 collectors "
    f"regionales: {', '.join(ALL_RCS)} (mismo host, distinto puerto, servicio OSS). "
    "Tabla principal: ALARMS.ALARM_LOG (52 columnas; claves: DN = objeto gestionado, "
    "ALARM_NUMBER, TEXT, SUPPLEMENTARY_INFO, ALARM_TIME = inicio, CANCEL_TIME IS NULL = "
    "alarma activa, ACK_STATUS, SEVERITY 1=critical 2=major 3=minor 4=warning; las columnas "
    "SEVERITY reciben un <col>_LABEL). Evita SELECT *: elige columnas. "
    f"Fechas en hora de red {NETWORK_TZ_LABEL}, sin conversión. "
    "Flujo recomendado: list_rcs (o test_connection) -> describe_table(rc, 'ALARMS', 'ALARM_LOG') "
    "-> query(rc, sql) para uno/varios RCs o query_all(sql) para los 6 en paralelo "
    "(resultados etiquetados por RC; un RC caído no aborta a los demás). "
    "Atajos FM: active_alarms_summary, alarm_history, top_alarms, alarm_timeline (rcs=null = los 6). "
    "list_tables(rc) descubre esquemas/tablas; explain_query(rc, sql) muestra plan resumido; "
    "export_query escribe CSV/Parquet en ORACLE_FM_EXPORT_DIR sin pasar por el LLM. "
    "Solo se acepta UNA sentencia SELECT/WITH en dialecto Oracle: usa ROWNUM o "
    "FETCH FIRST n ROWS ONLY (no LIMIT) y TO_DATE('2024-01-31 00:00:00','YYYY-MM-DD HH24:MI:SS'). "
    "Se rechazan DML/DDL/PL-SQL, SELECT ... FOR UPDATE y paquetes DBMS_/UTL_ con efectos. "
    "Default 500 filas por RC (max_rows hasta 20000; timeout_s hasta 300). "
    "Prefiere agregaciones (COUNT/GROUP BY) a volcar filas: la respuesta se recorta por tamaño; "
    "si truncated=true, refina con WHERE/GROUP BY. "
    "rc8 está detrás de un proxy con timeout ~11 s: rangos largos de ALARM_TIME se trocean "
    "automáticamente; aun así acota la ventana temporal."
)

mcp = MCPServer(
    "oracle-alarms-readonly",
    title="OSS FM Oracle (read-only)",
    instructions=INSTRUCTIONS,
    version=__version__,
)

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def _result(payload: dict[str, Any], settings: Settings | None = None) -> CallToolResult:
    """JSON compacto y acotado en tamaño; isError si el resultado principal falló."""
    max_chars = (settings or get_settings()).max_response_chars
    return CallToolResult(
        content=[TextContent(type="text", text=fit_payload(payload, max_chars))],
        is_error=payload.get("ok") is False,
    )


def _err(msg: str, **extra: Any) -> CallToolResult:
    text = to_compact_json({"ok": False, "error": msg, **extra})
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def list_rcs(probe: bool = True) -> CallToolResult:
    """Lista los 6 collectors regionales (RC) con host, puerto y DSN (sin credenciales).

    Punto de partida recomendado. Con probe=true (default) además prueba la conectividad
    de cada RC en paralelo (~1-10 s) y marca ok/status por RC; también informa los
    límites efectivos (filas por defecto, techo, timeout).

    Args:
        probe: Si true, prueba conexión a cada RC. Usa false para un listado instantáneo.
    """
    try:
        settings = get_settings()
        items = list_rc_info(settings)
        connectivity: dict[str, Any] | None = None
        if probe:
            try:
                settings.validate_credentials()
                probe_result = test_connections(list(ALL_RCS), settings)
                by_rc = {r["rc"]: r for r in probe_result["results"]}
                for item in items:
                    st = by_rc.get(item["rc"], {})
                    item["status"] = st.get("status", "unknown")
                    item["ok"] = st.get("ok", False)
                    if not st.get("ok"):
                        item["error"] = st.get("error")
                connectivity = {
                    "summary": probe_result["summary"],
                    "ok_count": probe_result["ok_count"],
                    "total": probe_result["total"],
                }
            except RuntimeError as e:
                connectivity = {"error": str(e)}
                for item in items:
                    item["status"] = "credentials_missing"
                    item["ok"] = None

        return _result(
            {
                "ok": True,
                "rcs": items,
                "count": len(items),
                "service": settings.service,
                "host": settings.host,
                "network_tz": NETWORK_TZ_LABEL,
                "connectivity": connectivity,
                "limits": {
                    "default_rows": settings.default_rows,
                    "max_rows": settings.max_rows,
                    "query_timeout_s": settings.query_timeout_s,
                    "max_response_chars": settings.max_response_chars,
                },
                "version": __version__,
            },
            settings,
        )
    except Exception as e:
        logger.exception("list_rcs")
        return _err(str(e))


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def test_connection(rc: str = "all") -> CallToolResult:
    """Verifica conectividad a uno o varios collectors: versión Oracle y hora del servidor.

    Args:
        rc: Un RC (rc1, rc2, rc7, rc8, rc9, rc10), lista separada por comas ("rc1,rc8"),
            o "all" (default; se prueban en paralelo).
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        names = resolve_rcs(rc)
        if len(names) == 1:
            return _result(oracle_test_connection(names[0], settings), settings)
        payload = test_connections(names, settings)
        payload["ok"] = payload["ok_count"] > 0
        return _result(payload, settings)
    except Exception as e:
        logger.exception("test_connection")
        return _err(str(e), rc=rc)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def describe_table(rc: str, owner: str, table: str) -> CallToolResult:
    """Describe las columnas de una tabla o vista Oracle (sin devolver datos).

    Usa ALL_TAB_COLUMNS (fallback USER_TAB_COLUMNS). El esquema es el mismo en todos
    los RCs, así que basta describir en uno (p.ej. rc1).

    Args:
        rc: Un solo collector (rc1, rc2, rc7, rc8, rc9, rc10).
        owner: Esquema Oracle (ej. "ALARMS").
        table: Nombre de tabla o vista (ej. "ALARM_LOG").
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        names = resolve_rcs(rc)
        if len(names) != 1:
            return _err("describe_table requiere un solo RC (no 'all' ni lista).", rc=rc)
        result = oracle_describe_table(names[0], owner, table, settings)
        return _result(result.to_dict(), settings)
    except Exception as e:
        logger.exception("describe_table")
        return _err(str(e), rc=rc, owner=owner, table=table)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def query(
    rc: str | list[str],
    sql: str,
    max_rows: int | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Ejecuta una única sentencia SELECT (o WITH … SELECT) de solo lectura en uno o más RCs.

    Dialecto Oracle: ROWNUM / FETCH FIRST n ROWS ONLY (no LIMIT); fechas con
    TO_DATE('2024-01-31 00:00:00','YYYY-MM-DD HH24:MI:SS'). Se rechazan DML/DDL/PL-SQL,
    FOR UPDATE, varias sentencias y paquetes DBMS_/UTL_ con efectos laterales.

    Args:
        rc: Un RC ("rc1"), lista ["rc1","rc8"], "rc1,rc8" o "all".
        sql: Una sola sentencia SELECT o WITH … SELECT (';' final opcional).
        max_rows: Filas máximas por RC. Default ORACLE_FM_DEFAULT_ROWS (500); hasta
            20000 por llamada. Para conteos usa COUNT(*)/GROUP BY en lugar de subirlo.
        timeout_s: Timeout por round-trip en segundos (default ORACLE_FM_QUERY_TIMEOUT_S;
            tope 300).

    Returns:
        JSON compacto. Un RC: ok, rc, row_count, truncated, columns, rows.
        Varios RCs: rcs_with_rows / rcs_zero_rows / rcs_failed y results[] por RC
        (un fallo no aborta a los demás). truncated=true indica que había más filas.
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        names = resolve_rcs(rc)
        if len(names) == 1:
            result = execute_query(
                names[0], sql, max_rows=max_rows, timeout_s=timeout_s, settings=settings
            )
            return _result(result.to_dict(), settings)
        payload = execute_query_many(
            names, sql, max_rows=max_rows, timeout_s=timeout_s, settings=settings
        )
        return _result(payload, settings)
    except Exception as e:
        logger.exception("query")
        return _err(str(e), rc=str(rc), sql=sql[:200])


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def query_all(
    sql: str,
    max_rows: int | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Difunde un SELECT/WITH a los 6 collectors en paralelo y etiqueta resultados por RC.

    Equivale a query(rc="all", ...). Un RC que falle no aborta a los demás; la
    respuesta distingue rcs_with_rows / rcs_zero_rows / rcs_failed. Ideal para
    conteos globales, p.ej. SELECT COUNT(*) FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL.

    Args:
        sql: Una sola sentencia SELECT o WITH … SELECT (dialecto Oracle).
        max_rows: Filas máximas por RC (default ORACLE_FM_DEFAULT_ROWS).
        timeout_s: Timeout por RC en segundos.
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        payload = execute_query_many(
            list(ALL_RCS),
            sql,
            max_rows=max_rows,
            timeout_s=timeout_s,
            settings=settings,
        )
        return _result(payload, settings)
    except Exception as e:
        logger.exception("query_all")
        return _err(str(e), sql=sql[:200])


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def active_alarms_summary(
    rcs: str | list[str] | None = None,
    group_by: str = "severity",
) -> CallToolResult:
    """Resumen de alarmas activas (CANCEL_TIME IS NULL) con conteos y top textos.

    Args:
        rcs: RC(s) o null/all para los 6 en paralelo.
        group_by: Agrupar por severity, alarm_number o dn_prefix (primer segmento del DN).
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        if group_by not in ("severity", "alarm_number", "dn_prefix"):
            return _err(
                "group_by inválido; use severity, alarm_number o dn_prefix.",
                group_by=group_by,
            )
        payload = alarms_active_summary(rcs=rcs, group_by=group_by, settings=settings)
        return _result(payload, settings)
    except Exception as e:
        logger.exception("active_alarms_summary")
        return _err(str(e))


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def alarm_history(
    dn_contains: str,
    since_hours: int = 24,
    rcs: str | list[str] | None = None,
    max_rows: int | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Historial reciente de alarmas cuyo DN contiene un fragmento (LIKE).

    Args:
        dn_contains: Subcadena a buscar en DN (sanitizada).
        since_hours: Ventana hacia atrás desde SYSDATE (default 24).
        rcs: RC(s) o null para los 6.
        max_rows: Filas máximas por RC (default 500).
        timeout_s: Timeout por RC (tope 300 s).
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        payload = alarms_history(
            dn_contains,
            since_hours=since_hours,
            rcs=rcs,
            max_rows=max_rows,
            settings=settings,
            timeout_s=timeout_s,
        )
        return _result(payload, settings)
    except Exception as e:
        logger.exception("alarm_history")
        return _err(str(e), dn_contains=dn_contains)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def top_alarms(
    since_hours: int = 24,
    by: str = "alarm_number",
    top: int = 30,
    rcs: str | list[str] | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Alarmas más frecuentes en una ventana temporal (conteo por clave).

    Args:
        since_hours: Horas hacia atrás desde SYSDATE.
        by: alarm_number, text o dn.
        top: Cuántos grupos devolver (máx 500).
        rcs: RC(s) o null para los 6.
        timeout_s: Timeout por RC.
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        if by not in ("alarm_number", "text", "dn"):
            return _err("by inválido; use alarm_number, text o dn.", by=by)
        payload = alarms_top(
            since_hours=since_hours,
            by=by,
            top=top,
            rcs=rcs,
            settings=settings,
            timeout_s=timeout_s,
        )
        return _result(payload, settings)
    except Exception as e:
        logger.exception("top_alarms")
        return _err(str(e))


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def alarm_timeline(
    alarm_number: int | None = None,
    dn_contains: str | None = None,
    since_hours: int = 72,
    bucket: str = "hour",
    rcs: str | list[str] | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Serie temporal de conteos de alarmas por hora o día.

    Args:
        alarm_number: Filtro opcional por número de alarma.
        dn_contains: Filtro opcional en DN.
        since_hours: Ventana hacia atrás (default 72).
        bucket: hour o day.
        rcs: RC(s) o null para los 6.
        timeout_s: Timeout por RC.
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        if bucket not in ("hour", "day"):
            return _err("bucket inválido; use hour o day.", bucket=bucket)
        payload = alarms_timeline(
            alarm_number=alarm_number,
            dn_contains=dn_contains,
            since_hours=since_hours,
            bucket=bucket,
            rcs=rcs,
            settings=settings,
            timeout_s=timeout_s,
        )
        return _result(payload, settings)
    except Exception as e:
        logger.exception("alarm_timeline")
        return _err(str(e))


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def list_tables(
    rc: str,
    owner: str | None = None,
    search: str | None = None,
    max_rows: int | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Lista tablas y vistas Oracle accesibles (ALL_TABLES / ALL_VIEWS).

    Args:
        rc: Un solo collector.
        owner: Filtrar por esquema (ej. ALARMS).
        search: Subcadena en nombre de tabla/vista (LIKE, case-insensitive).
        max_rows: Tope de objetos listados (default 500).
        timeout_s: Timeout de la consulta.
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        names = resolve_rcs(rc)
        if len(names) != 1:
            return _err("list_tables requiere un solo RC.", rc=rc)
        result = oracle_list_tables(
            names[0],
            owner=owner,
            search=search,
            settings=settings,
            timeout_s=timeout_s,
            max_rows=max_rows,
        )
        return _result(result.to_dict(), settings)
    except Exception as e:
        logger.exception("list_tables")
        return _err(str(e), rc=rc)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def explain_query(
    rc: str,
    sql: str,
    timeout_s: int | None = None,
) -> CallToolResult:
    """EXPLAIN PLAN + DBMS_XPLAN.DISPLAY resumido para un SELECT/WITH.

    Args:
        rc: Un solo RC.
        sql: Sentencia SELECT o WITH … SELECT a planificar (solo lectura).
        timeout_s: Timeout por RC.
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        names = resolve_rcs(rc)
        if len(names) != 1:
            return _err("explain_query requiere un solo RC.", rc=rc)
        result = oracle_explain_query(
            names[0], sql, settings=settings, timeout_s=timeout_s
        )
        return _result(result.to_dict(), settings)
    except Exception as e:
        logger.exception("explain_query")
        return _err(str(e), rc=rc, sql=sql[:200])


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
def export_query(
    sql: str,
    rcs: str | list[str] | None = None,
    format: str = "csv",
    max_rows: int | None = None,
    timeout_s: int | None = None,
) -> CallToolResult:
    """Exporta un SELECT/WITH a CSV o Parquet en disco (sin volcar filas al LLM).

    Escribe bajo ORACLE_FM_EXPORT_DIR (default C:\\DVB\\exports\\oss\\…), un archivo
    por RC. Usa el mismo troceo rc8 que query cuando la SQL filtra ALARM_TIME.

    Args:
        sql: SELECT o WITH … SELECT.
        rcs: RC(s) o null para los 6.
        format: csv o parquet.
        max_rows: Filas máximas por RC (default 500; hasta 20000).
        timeout_s: Timeout por RC (tope 300).
    """
    try:
        settings = get_settings()
        settings.validate_credentials()
        if format.lower() not in ("csv", "parquet"):
            return _err("format debe ser csv o parquet.", format=format)
        payload = oracle_export_query(
            sql,
            rcs=rcs,
            format=format,
            max_rows=max_rows,
            settings=settings,
            timeout_s=timeout_s,
        )
        return _result(payload, settings)
    except Exception as e:
        logger.exception("export_query")
        return _err(str(e), sql=sql[:200])


def run_smoke() -> int:
    """Smoke manual: test_connection(all) + COUNT alarmas activas por RC."""
    settings = get_settings()
    try:
        settings.validate_credentials()
    except RuntimeError as e:
        print(f"SKIP smoke: {e}", file=sys.stderr)
        print(
            "Creá .env desde .env.example con ORACLE_FM_USER / ORACLE_FM_PASSWORD "
            "y reintentá: python -m oracle_alarms_readonly_mcp --smoke",
            file=sys.stderr,
        )
        return 2

    print("=== test_connection(all) ===")
    conn_payload = test_connections("all", settings)
    print(json.dumps(conn_payload, ensure_ascii=False, indent=2, default=str))

    sql = "SELECT COUNT(*) AS ACTIVE_ALARMS FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL"
    print("\n=== active alarm counts per RC ===")
    print(f"SQL: {sql}")
    counts = execute_query_many(list(ALL_RCS), sql, max_rows=10, settings=settings)
    print(json.dumps(counts, ensure_ascii=False, indent=2, default=str))

    ok_conn = conn_payload.get("ok_count", 0)
    total = conn_payload.get("total", len(ALL_RCS))
    ok_q = counts.get("ok_count", 0)
    print(
        f"\nResumen: conexiones {ok_conn}/{total}; "
        f"COUNT OK {ok_q}/{len(ALL_RCS)}; "
        f"con filas {counts.get('rcs_with_rows')}; "
        f"cero {counts.get('rcs_zero_rows')}; "
        f"fallidos {counts.get('rcs_failed')}"
    )
    if ok_conn == 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OSS FM Oracle MCP (solo lectura)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Prueba conexiones + COUNT alarmas activas por RC y sale",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Muestra versión y sale",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.smoke:
        return run_smoke()

    settings = get_settings()
    try:
        settings.validate_credentials()
    except RuntimeError as e:
        # Se arranca igual: cada herramienta devuelve el error explícito al cliente.
        logger.warning("%s", e)

    logger.info("OSS FM Oracle MCP v%s (READ ONLY)", __version__)
    logger.info("  env_file=%s (exists=%s)", ENV_FILE, ENV_FILE.exists())
    logger.info("  host=%s service=%s", settings.host, settings.service)
    logger.info(
        "  RCs: %s",
        ", ".join(f"{n}:{RC_PORTS[n]}" for n in ALL_RCS),
    )
    logger.info(
        "  default_rows=%s per_call_max_rows=%s env_max_rows=%s timeout_s=%s (cap %s) "
        "rc8_chunk_days=%s export_dir=%s max_response_chars=%s",
        settings.default_rows,
        PER_CALL_MAX_ROWS,
        settings.max_rows,
        settings.query_timeout_s,
        PER_CALL_MAX_TIMEOUT_S,
        settings.rc8_chunk_days,
        settings.export_dir,
        settings.max_response_chars,
    )
    logger.info("  network_tz=%s", NETWORK_TZ_LABEL)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
