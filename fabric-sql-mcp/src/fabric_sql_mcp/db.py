"""Acceso a Fabric SQL vía pyodbc + Entra ID, pool por (alias, database) y paginación con cursor."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any, Iterator, Optional

import pyodbc

from fabric_sql_mcp.config import Connection

logger = logging.getLogger("fabric-sql-mcp")

MAX_PAGE_ROWS = 5000

_POOL_IDLE_VALIDATE_S = 60
_OPEN_QUERY_TTL_S = 600
_OPEN_QUERY_MAX = 5

# ============================================================
# ODBC
# ============================================================

_DRIVER_CACHE: Optional[str] = None


def get_odbc_driver(conn: Connection) -> str:
    global _DRIVER_CACHE
    if conn.odbc_driver:
        return conn.odbc_driver
    if _DRIVER_CACHE:
        return _DRIVER_CACHE
    drivers = [d for d in pyodbc.drivers() if d.startswith("ODBC Driver") and "SQL Server" in d]
    if not drivers:
        raise RuntimeError(
            "No se encontró 'ODBC Driver 17/18 for SQL Server'. Instálalo desde "
            "https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
        )
    _DRIVER_CACHE = sorted(drivers, reverse=True)[0]
    logger.info(f"ODBC Driver detectado: {_DRIVER_CACHE}")
    return _DRIVER_CACHE


def _odbc_escape(value: str) -> str:
    if any(c in value for c in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_connection_string(conn: Connection, database: Optional[str] = None) -> str:
    parts = [
        f"DRIVER={{{get_odbc_driver(conn)}}}",
        f"SERVER={conn.server},{conn.port}",
        f"DATABASE={_odbc_escape(database or conn.database)}",
        f"Authentication={conn.auth}",
        "Encrypt=yes",
        "TrustServerCertificate=no",
        "LoginTimeout=60",
        "ConnectRetryCount=2",
        "ConnectRetryInterval=5",
    ]
    if conn.username:
        parts.append(f"UID={_odbc_escape(conn.username)}")
    if conn.password and conn.auth in ("ActiveDirectoryPassword", "ActiveDirectoryServicePrincipal"):
        parts.append(f"PWD={_odbc_escape(conn.password)}")
    return ";".join(parts) + ";"


def _connect(conn: Connection, database: Optional[str] = None) -> pyodbc.Connection:
    problems = conn.problems()
    if problems:
        raise RuntimeError(f"Conexión '{conn.name}' incompleta, faltan: {', '.join(problems)}")
    cnx = pyodbc.connect(build_connection_string(conn, database), autocommit=True)
    cnx.timeout = conn.query_timeout
    return cnx


def _ping(cnx: pyodbc.Connection) -> bool:
    try:
        cur = cnx.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return True
    except pyodbc.Error:
        return False


@dataclass
class _Pooled:
    cnx: pyodbc.Connection
    last_used: float
    holders: int = 0


_POOL: dict[tuple[str, str], _Pooled] = {}
_POOL_LOCK = threading.Lock()


def _pool_key(conn: Connection, database: Optional[str]) -> tuple[str, str]:
    db = (database or conn.database).lower()
    return (conn.name, db)


def _close_pooled(entry: _Pooled) -> None:
    try:
        entry.cnx.close()
    except Exception as e:  # pragma: no cover
        logger.debug(f"close pool: {e}")


def acquire_connection(conn: Connection, database: Optional[str] = None) -> pyodbc.Connection:
    key = _pool_key(conn, database)
    with _POOL_LOCK:
        entry = _POOL.get(key)
        now = time.time()
        if entry and now - entry.last_used > _POOL_IDLE_VALIDATE_S and not _ping(entry.cnx):
            _close_pooled(entry)
            del _POOL[key]
            entry = None
        if entry is None:
            entry = _Pooled(_connect(conn, database), now)
            _POOL[key] = entry
        entry.holders += 1
        entry.last_used = now
        return entry.cnx


def release_connection(conn: Connection, database: Optional[str], *, close: bool = False) -> None:
    key = _pool_key(conn, database)
    with _POOL_LOCK:
        entry = _POOL.get(key)
        if not entry:
            return
        entry.holders = max(0, entry.holders - 1)
        if close:
            _close_pooled(entry)
            _POOL.pop(key, None)
            return
        entry.last_used = time.time()


def close_all_pools() -> None:
    with _POOL_LOCK:
        for entry in list(_POOL.values()):
            _close_pooled(entry)
        _POOL.clear()


atexit.register(close_all_pools)


@contextmanager
def open_connection(conn: Connection, database: Optional[str] = None) -> Iterator[pyodbc.Connection]:
    """Contexto que adquiere/libera una conexión del pool (compatibilidad interna)."""
    cnx = acquire_connection(conn, database)
    try:
        yield cnx
    finally:
        release_connection(conn, database)


# ============================================================
# Caché de cursores abiertos (paginación)
# ============================================================


@dataclass
class _OpenQuery:
    query_id: str
    cur: pyodbc.Cursor
    conn: Connection
    database: Optional[str]
    columns: list[str]
    created: float
    last_used: float
    max_cell_chars: int
    pending_rows: deque[list[Any]]
    max_response_chars: int
    response_envelope: dict[str, Any] = field(default_factory=dict)
    db_exhausted: bool = False
    rows_from_db: int = 0
    cursor_max_rows: int = 1_000_000


_OPEN: dict[str, _OpenQuery] = {}
_OPEN_LOCK = threading.Lock()


def _close_open(entry: _OpenQuery) -> None:
    try:
        entry.cur.close()
    except Exception as e:  # pragma: no cover
        logger.debug(f"close cursor: {e}")
    release_connection(entry.conn, entry.database)


def _evict_open_queries() -> None:
    now = time.time()
    expired = [k for k, v in _OPEN.items() if now - v.last_used > _OPEN_QUERY_TTL_S]
    for k in expired:
        _close_open(_OPEN.pop(k))
    while len(_OPEN) > _OPEN_QUERY_MAX:
        oldest = min(_OPEN.items(), key=lambda kv: kv[1].last_used)[0]
        _close_open(_OPEN.pop(oldest))


def close_all_open_queries() -> None:
    with _OPEN_LOCK:
        for entry in list(_OPEN.values()):
            _close_open(entry)
        _OPEN.clear()


atexit.register(close_all_open_queries)


def _response_shell(entry: _OpenQuery, cursor_id: Optional[str], has_more: bool) -> dict[str, Any]:
    return {
        "columns": entry.columns,
        "rows": [],
        "row_count": 0,
        "rows_returned": 0,
        "rows_buffered": len(entry.pending_rows),
        "has_more": has_more,
        "truncated": has_more,
        "next_cursor": cursor_id if has_more else None,
    }


def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str))


def _split_rows_to_budget(
    entry: _OpenQuery, rows: list[list[Any]],
) -> tuple[list[list[Any]], list[list[Any]]]:
    max_chars = entry.max_response_chars
    if max_chars <= 0 or not rows:
        return rows, []
    shell = {**entry.response_envelope, **_response_shell(entry, entry.query_id, True)}
    lo, hi = 0, len(rows)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        shell["rows"] = rows[:mid]
        shell["row_count"] = mid
        shell["rows_returned"] = mid
        if _json_size(shell) <= max_chars:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best >= len(rows):
        return rows, []
    if best == 0 and rows:
        return rows[:1], rows[1:]
    return rows[:best], rows[best:]


def _fill_pending_from_db(entry: _OpenQuery, target_len: int) -> None:
    mc = entry.max_cell_chars
    while len(entry.pending_rows) < target_len and not entry.db_exhausted:
        if entry.rows_from_db >= entry.cursor_max_rows:
            entry.db_exhausted = True
            break
        need = target_len - len(entry.pending_rows)
        cap = entry.cursor_max_rows - entry.rows_from_db
        fetch_n = min(need + 1, cap + 1)
        if fetch_n <= 0:
            entry.db_exhausted = True
            break
        rows_raw = entry.cur.fetchmany(fetch_n)
        if not rows_raw:
            entry.db_exhausted = True
            break
        if len(rows_raw) > need:
            head, tail = rows_raw[:need], rows_raw[need:]
        else:
            head, tail = rows_raw, []
            if len(rows_raw) < fetch_n:
                entry.db_exhausted = True
        for r in head:
            entry.pending_rows.append([serialize_value(v, mc) for v in r])
        for r in tail:
            entry.pending_rows.append([serialize_value(v, mc) for v in r])
        entry.rows_from_db += len(rows_raw)


def _take_query_page(entry: _OpenQuery, page_size: int) -> tuple[list[list[Any]], bool]:
    _fill_pending_from_db(entry, page_size)
    batch: list[list[Any]] = []
    while len(batch) < page_size and entry.pending_rows:
        batch.append(entry.pending_rows.popleft())
    delivered, overflow = _split_rows_to_budget(entry, batch)
    if overflow:
        entry.pending_rows.extendleft(reversed(overflow))
    has_more = bool(entry.pending_rows) or not entry.db_exhausted
    return delivered, has_more


def _rows_to_dicts(columns: list[str], rows: list, max_chars: int) -> list[dict]:
    return [{col: serialize_value(v, max_chars) for col, v in zip(columns, row)} for row in rows]


def _page_result(
    entry: _OpenQuery, cursor_id: str, rows: list[list[Any]], has_more: bool, elapsed: int,
) -> dict[str, Any]:
    dict_rows = _rows_to_dicts(entry.columns, rows, entry.max_cell_chars)
    buffered = len(entry.pending_rows)
    return {
        "type": "query",
        "columns": entry.columns,
        "rows": dict_rows,
        "row_count": len(dict_rows),
        "rows_returned": len(dict_rows),
        "rows_buffered": buffered,
        "has_more": has_more,
        "truncated": has_more,
        "next_cursor": cursor_id if has_more else None,
        "elapsed_ms": elapsed,
        "_response_rows_fitted": True,
    }


def fetch_query_page(cursor_id: str, page_size: int) -> dict[str, Any]:
    with _OPEN_LOCK:
        _evict_open_queries()
        entry = _OPEN.get(cursor_id)
        if not entry:
            raise LookupError(f"Cursor '{cursor_id}' no existe o expiró (TTL {_OPEN_QUERY_TTL_S}s)")
        entry.last_used = time.time()
    started = time.time()
    rows, has_more = _take_query_page(entry, page_size)
    elapsed = int((time.time() - started) * 1000)
    if not has_more:
        with _OPEN_LOCK:
            _close_open(_OPEN.pop(cursor_id, entry))
    return _page_result(entry, cursor_id, rows, has_more, elapsed)


def start_query_cursor(
    conn: Connection,
    database: Optional[str],
    sql: str,
    page_size: int,
    max_cell_chars: int,
    max_response_chars: int = 0,
    response_envelope: Optional[dict[str, Any]] = None,
    cursor_max_rows: int = 1_000_000,
) -> dict[str, Any]:
    with _OPEN_LOCK:
        _evict_open_queries()
    db_name = database or conn.database
    started = time.time()
    cnx = acquire_connection(conn, database)
    cur = cnx.cursor()
    cur.execute(sql)
    while cur.description is None and cur.nextset():
        pass
    if cur.description is None:
        release_connection(conn, database)
        return {
            "type": "execute",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "rows_returned": 0,
            "rows_buffered": 0,
            "has_more": False,
            "truncated": False,
            "next_cursor": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "_response_rows_fitted": True,
        }
    cols = _unique_columns(cur.description)
    qid = str(uuid.uuid4())
    entry = _OpenQuery(
        qid, cur, conn, database, cols,
        time.time(), time.time(), max_cell_chars, deque(), max_response_chars,
        response_envelope=dict(response_envelope or {}),
        cursor_max_rows=cursor_max_rows,
    )
    with _OPEN_LOCK:
        _OPEN[qid] = entry
    rows, has_more = _take_query_page(entry, page_size)
    elapsed = int((time.time() - started) * 1000)
    if not has_more:
        with _OPEN_LOCK:
            _close_open(_OPEN.pop(qid, entry))
    result = _page_result(entry, qid, rows, has_more, elapsed)
    result.update({"connection": conn.name, "database": db_name, "query_execution_id": qid})
    return result


# ============================================================
# Serialización
# ============================================================


def serialize_value(val: Any, max_chars: int = 0) -> Any:
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if isinstance(val, str):
        if max_chars and len(val) > max_chars:
            return val[:max_chars] + f"…[truncado, {len(val)} chars]"
        return val
    if isinstance(val, Decimal):
        as_float = float(val)
        return as_float if Decimal(repr(as_float)) == val else str(val)
    if isinstance(val, (datetime, date, dtime)):
        return val.isoformat()
    if isinstance(val, timedelta):
        return val.total_seconds()
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, (bytes, bytearray)):
        hexed = val.hex()
        return hexed if not max_chars or len(hexed) <= max_chars else hexed[:max_chars] + "…"
    return str(val)


# ============================================================
# Cache + stats
# ============================================================


class QueryCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def _key(*parts: Any) -> str:
        return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()

    def get(self, ttl: int, *parts: Any) -> Optional[dict]:
        if ttl <= 0:
            return None
        key = self._key(*parts)
        entry = self._store.get(key)
        if not entry:
            return None
        ts, value = entry
        if time.time() - ts > ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, ttl: int, value: dict, *parts: Any) -> None:
        if ttl > 0:
            self._store[self._key(*parts)] = (time.time(), value)


CACHE = QueryCache()

STATS_MAX = 200
_STATS: "OrderedDict[str, dict]" = OrderedDict()


def store_stats(qid: str, stats: dict) -> None:
    _STATS[qid] = stats
    while len(_STATS) > STATS_MAX:
        _STATS.popitem(last=False)


def get_stats(qid: str) -> Optional[dict]:
    return _STATS.get(qid)


# ============================================================
# Ejecución (metadatos / consultas sin cursor)
# ============================================================


def _unique_columns(description) -> list[str]:
    seen: dict[str, int] = {}
    cols = []
    for col in description:
        name = col[0] or "col"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    return cols


def execute(
    conn: Connection,
    sql: str,
    params: tuple = (),
    *,
    database: Optional[str] = None,
    max_rows: int = 100,
    use_cache: bool = True,
    max_chars: int = 4000,
    kind: str = "query",
) -> dict:
    max_rows = max(1, min(int(max_rows), MAX_PAGE_ROWS))
    db_name = database or conn.database
    cache_parts = (conn.name, db_name, sql, params, max_rows)

    if use_cache:
        cached = CACHE.get(conn.cache_ttl, *cache_parts)
        if cached:
            return {**cached, "from_cache": True}

    qid = str(uuid.uuid4())
    started = time.time()

    with open_connection(conn, database) as cnx:
        cursor = cnx.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)

        while cursor.description is None and cursor.nextset():
            pass

        if cursor.description is None:
            rows_affected = cursor.rowcount
            elapsed = time.time() - started
            result = {
                "type": "execute",
                "rows_affected": rows_affected,
                "message": f"{rows_affected} filas afectadas",
            }
        else:
            columns = _unique_columns(cursor.description)
            rows = cursor.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            elapsed = time.time() - started
            result = {
                "type": kind,
                "columns": columns,
                "rows": _rows_to_dicts(columns, rows, max_chars),
                "row_count": len(rows),
                "truncated": truncated,
                "has_more": truncated,
            }

    result.update({"connection": conn.name, "database": db_name, "query_execution_id": qid})
    stats = {
        "query_execution_id": qid,
        "state": "SUCCEEDED",
        "connection": conn.name,
        "database": db_name,
        "type": result["type"],
        "execution_time_ms": int(elapsed * 1000),
        "row_count": result.get("row_count", 0),
        "rows_affected": result.get("rows_affected"),
        "has_more": result.get("has_more"),
        "query_preview": sql[:300],
    }
    store_stats(qid, stats)
    logger.info(
        f"[{qid[:8]}] {conn.name}/{db_name} {result['type']} ok ({elapsed:.2f}s, "
        f"rows={result.get('row_count', result.get('rows_affected'))}): {' '.join(sql.split())[:100]}"
    )

    if use_cache and result["type"] != "execute":
        CACHE.set(conn.cache_ttl, result, *cache_parts)
    return result


def explain(conn: Connection, sql: str, database: Optional[str] = None) -> dict:
    qid = str(uuid.uuid4())
    started = time.time()
    with open_connection(conn, database) as cnx:
        cursor = cnx.cursor()
        cursor.execute("SET SHOWPLAN_XML ON")
        try:
            cursor.execute(sql)
            plans = []
            while True:
                if cursor.description is not None:
                    plans += [serialize_value(r[0]) for r in cursor.fetchall()]
                if not cursor.nextset():
                    break
        finally:
            cursor.execute("SET SHOWPLAN_XML OFF")
    elapsed = time.time() - started
    store_stats(qid, {
        "query_execution_id": qid,
        "state": "SUCCEEDED",
        "connection": conn.name,
        "database": database or conn.database,
        "type": "explain",
        "execution_time_ms": int(elapsed * 1000),
        "query_preview": sql[:300],
    })
    return {"type": "explain", "plan_count": len(plans), "plans": plans, "query_execution_id": qid}
