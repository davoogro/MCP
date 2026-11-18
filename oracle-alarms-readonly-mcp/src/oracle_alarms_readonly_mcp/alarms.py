"""Consultas de alto nivel sobre ALARMS.ALARM_LOG (activas, historial, tops, timeline)."""

from __future__ import annotations

import re
from typing import Any, Literal

from oracle_alarms_readonly_mcp.config import Settings, get_settings, resolve_rcs_optional
from oracle_alarms_readonly_mcp.oracle import execute_query_many

_DN_UNSAFE = re.compile(r"[^a-zA-Z0-9_./\\:%-]")


def _sql_literal(text: str) -> str:
    return text.replace("'", "''")


def _sanitize_dn_fragment(text: str) -> str:
    cleaned = _DN_UNSAFE.sub("", text.strip())
    if not cleaned:
        raise ValueError("dn_contains vacío o sin caracteres válidos tras sanitizar")
    return cleaned


def _hours_clause(hours: int) -> str:
    h = max(1, min(int(hours), 24 * 365))
    return f"SYSDATE - ({h}/24)"


def active_alarms_summary(
    rcs: str | list[str] | None = None,
    group_by: Literal["severity", "alarm_number", "dn_prefix"] = "severity",
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Resumen de alarmas activas por RC con conteos agrupados y top textos."""
    settings = settings or get_settings()
    names = resolve_rcs_optional(rcs)

    if group_by == "severity":
        group_sql = (
            "SELECT SEVERITY AS GRP_KEY, COUNT(*) AS CNT "
            "FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL "
            "GROUP BY SEVERITY ORDER BY CNT DESC"
        )
    elif group_by == "alarm_number":
        group_sql = (
            "SELECT TO_CHAR(ALARM_NUMBER) AS GRP_KEY, COUNT(*) AS CNT "
            "FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL "
            "GROUP BY ALARM_NUMBER ORDER BY CNT DESC FETCH FIRST 100 ROWS ONLY"
        )
    else:
        group_sql = (
            "SELECT SUBSTR(DN, 1, INSTR(DN || '/', '/') - 1) AS GRP_KEY, COUNT(*) AS CNT "
            "FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL "
            "GROUP BY SUBSTR(DN, 1, INSTR(DN || '/', '/') - 1) "
            "ORDER BY CNT DESC FETCH FIRST 100 ROWS ONLY"
        )

    top_text_sql = (
        "SELECT TEXT, COUNT(*) AS CNT FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL "
        "GROUP BY TEXT ORDER BY COUNT(*) DESC FETCH FIRST 15 ROWS ONLY"
    )

    grouped = execute_query_many(
        names, group_sql, max_rows=500, timeout_s=timeout_s, settings=settings
    )
    top_text = execute_query_many(
        names, top_text_sql, max_rows=20, timeout_s=timeout_s, settings=settings
    )
    return {
        "ok": bool(grouped.get("ok")) and bool(top_text.get("ok")),
        "group_by": group_by,
        "rcs_requested": names,
        "summary_by_rc": grouped,
        "top_texts_by_rc": top_text,
    }


def alarm_history(
    dn_contains: str,
    since_hours: int = 24,
    rcs: str | list[str] | None = None,
    max_rows: int | None = None,
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Historial reciente de alarmas cuyo DN contiene el fragmento indicado."""
    settings = settings or get_settings()
    names = resolve_rcs_optional(rcs)
    frag = _sanitize_dn_fragment(dn_contains)
    since = _hours_clause(since_hours)
    sql = (
        "SELECT DN, ALARM_NUMBER, TEXT, ALARM_TIME, CANCEL_TIME, SEVERITY "
        "FROM ALARMS.ALARM_LOG "
        f"WHERE UPPER(DN) LIKE '%' || UPPER('{_sql_literal(frag)}') || '%' "
        f"AND ALARM_TIME >= {since} "
        "ORDER BY ALARM_TIME DESC"
    )
    payload = execute_query_many(
        names, sql, max_rows=max_rows, timeout_s=timeout_s, settings=settings
    )
    payload["dn_contains"] = frag
    payload["since_hours"] = since_hours
    return payload


def top_alarms(
    since_hours: int = 24,
    by: Literal["alarm_number", "text", "dn"] = "alarm_number",
    top: int = 30,
    rcs: str | list[str] | None = None,
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Alarmas más frecuentes en la ventana temporal."""
    settings = settings or get_settings()
    names = resolve_rcs_optional(rcs)
    since = _hours_clause(since_hours)
    n = max(1, min(int(top), 500))
    if by == "text":
        key_expr = "TEXT"
    elif by == "dn":
        key_expr = "DN"
    else:
        key_expr = "TO_CHAR(ALARM_NUMBER)"
    sql = (
        f"SELECT {key_expr} AS KEY_VAL, COUNT(*) AS CNT "
        "FROM ALARMS.ALARM_LOG "
        f"WHERE ALARM_TIME >= {since} "
        f"GROUP BY {key_expr} ORDER BY COUNT(*) DESC FETCH FIRST {n} ROWS ONLY"
    )
    payload = execute_query_many(
        names, sql, max_rows=n + 5, timeout_s=timeout_s, settings=settings
    )
    payload["since_hours"] = since_hours
    payload["by"] = by
    payload["top"] = n
    return payload


def alarm_timeline(
    alarm_number: int | None = None,
    dn_contains: str | None = None,
    since_hours: int = 72,
    bucket: Literal["hour", "day"] = "hour",
    rcs: str | list[str] | None = None,
    settings: Settings | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Conteos de alarmas por intervalo (hora o día)."""
    settings = settings or get_settings()
    names = resolve_rcs_optional(rcs)
    since = _hours_clause(since_hours)
    trunc = "HH24" if bucket == "hour" else "DD"
    filters = [f"ALARM_TIME >= {since}"]
    if alarm_number is not None:
        filters.append(f"ALARM_NUMBER = {int(alarm_number)}")
    if dn_contains:
        frag = _sanitize_dn_fragment(dn_contains)
        filters.append(
            f"UPPER(DN) LIKE '%' || UPPER('{_sql_literal(frag)}') || '%'"
        )
    where = " AND ".join(filters)
    sql = (
        f"SELECT TRUNC(ALARM_TIME, '{trunc}') AS BUCKET_START, COUNT(*) AS CNT "
        f"FROM ALARMS.ALARM_LOG WHERE {where} "
        f"GROUP BY TRUNC(ALARM_TIME, '{trunc}') ORDER BY BUCKET_START"
    )
    payload = execute_query_many(
        names, sql, max_rows=5000, timeout_s=timeout_s, settings=settings
    )
    payload["since_hours"] = since_hours
    payload["bucket"] = bucket
    if alarm_number is not None:
        payload["alarm_number"] = int(alarm_number)
    if dn_contains:
        payload["dn_contains"] = dn_contains
    return payload
