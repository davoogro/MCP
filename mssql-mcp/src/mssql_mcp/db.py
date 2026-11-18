"""Acceso a SQL Server vía pyodbc, siempre en transacción que se deshace (capa 2 de solo lectura).

Nunca se llama ``commit()`` en este paquete: cada sesión abre la conexión con
``autocommit=False`` y ``ApplicationIntent=ReadOnly`` y termina con ``rollback()``.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Any, Iterator, Optional

import pyodbc

from mssql_mcp.config import Server, Settings

logger = logging.getLogger("mssql-mcp")

pyodbc.pooling = False

_POOL_IDLE_VALIDATE_S = 60
_OPEN_QUERY_TTL_S = 600
_OPEN_QUERY_MAX = 5


def _odbc_escape(value: str) -> str:
    if any(c in value for c in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_connection_string(settings: Settings, server: Server, database: Optional[str] = None) -> str:
    parts = [
        f"DRIVER={{{settings.driver}}}",
        f"SERVER={server.address}",
        f"UID={_odbc_escape(settings.user)}",
        f"PWD={_odbc_escape(settings.password)}",
        f"Encrypt={settings.encrypt}",
        f"TrustServerCertificate={settings.trust_cert}",
        "APP=mssql-mcp",
        "ApplicationIntent=ReadOnly",
    ]
    if database:
        parts.append(f"DATABASE={_odbc_escape(database)}")
    return ";".join(parts) + ";"


def _prepare_connection(cnx: pyodbc.Connection, settings: Settings) -> None:
    cnx.timeout = settings.query_timeout
    cur = cnx.cursor()
    cur.execute("SET LOCK_TIMEOUT 30000; SET TRANSACTION ISOLATION LEVEL READ COMMITTED;")
    cur.close()


def _connect(settings: Settings, server: Server, database: Optional[str] = None) -> pyodbc.Connection:
    problems = settings.problems()
    if problems:
        raise RuntimeError(f"Configuración incompleta, faltan: {', '.join(problems)}")
    cnx = pyodbc.connect(
        build_connection_string(settings, server, database),
        autocommit=False,
        timeout=settings.login_timeout,
    )
    _prepare_connection(cnx, settings)
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


def _pool_key(server: Server, database: Optional[str]) -> tuple[str, str]:
    return (server.alias, (database or "").lower())


def _close_pooled(entry: _Pooled) -> None:
    try:
        entry.cnx.rollback()
    except Exception as e:  # pragma: no cover
        logger.debug(f"rollback pool: {e}")
    try:
        entry.cnx.close()
    except Exception as e:  # pragma: no cover
        logger.debug(f"close pool: {e}")


def acquire_connection(settings: Settings, server: Server, database: Optional[str] = None) -> pyodbc.Connection:
    key = _pool_key(server, database)
    with _POOL_LOCK:
        entry = _POOL.get(key)
        now = time.time()
        if entry and now - entry.last_used > _POOL_IDLE_VALIDATE_S and not _ping(entry.cnx):
            _close_pooled(entry)
            del _POOL[key]
            entry = None
        if entry is None:
            entry = _Pooled(_connect(settings, server, database), now)
            _POOL[key] = entry
        entry.holders += 1
        entry.last_used = now
        return entry.cnx


def release_connection(server: Server, database: Optional[str], *, close: bool = False) -> None:
    key = _pool_key(server, database)
    with _POOL_LOCK:
        entry = _POOL.get(key)
        if not entry:
            return
        entry.holders = max(0, entry.holders - 1)
        if close:
            _close_pooled(entry)
            _POOL.pop(key, None)
            return
        try:
            entry.cnx.rollback()
        except Exception as e:  # pragma: no cover
            logger.debug(f"rollback release: {e}")
        entry.last_used = time.time()


def close_all_pools() -> None:
    with _POOL_LOCK:
        for entry in list(_POOL.values()):
            _close_pooled(entry)
        _POOL.clear()


atexit.register(close_all_pools)


# ============================================================
# Caché de cursores abiertos (paginación)
# ============================================================


@dataclass
class _OpenQuery:
    query_id: str
    cur: pyodbc.Cursor
    cnx: pyodbc.Connection
    server: Server
    database: Optional[str]
    columns: list[str]
    col_types: list[str]
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
    release_connection(entry.server, entry.database)


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
        "columns": [{"name": c, "type": t} for c, t in zip(entry.columns, entry.col_types)],
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


def _split_rows_to_budget(entry: _OpenQuery, rows: list[list[Any]]) -> tuple[list[list[Any]], list[list[Any]]]:
    """Parte filas en entregadas vs cola según max_response_chars (incluye response_envelope)."""
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
    """Llena pending_rows desde pyodbc hasta target_len filas o fin de resultado / tope del cursor."""
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
        if entry.rows_from_db >= entry.cursor_max_rows and tail:
            pass  # tail ya en cola; no leer más en la siguiente iteración


def _take_query_page(entry: _OpenQuery, page_size: int) -> tuple[list[list[Any]], bool]:
    """Cola de filas pendientes: llenar hasta page_size, sacar lote, recortar por presupuesto JSON."""
    _fill_pending_from_db(entry, page_size)
    batch: list[list[Any]] = []
    while len(batch) < page_size and entry.pending_rows:
        batch.append(entry.pending_rows.popleft())
    delivered, overflow = _split_rows_to_budget(entry, batch)
    if overflow:
        entry.pending_rows.extendleft(reversed(overflow))
    has_more = bool(entry.pending_rows) or not entry.db_exhausted
    return delivered, has_more


def _page_result(entry: _OpenQuery, cursor_id: str, rows: list[list[Any]], has_more: bool, elapsed: int) -> dict[str, Any]:
    buffered = len(entry.pending_rows)
    return {
        "columns": [{"name": c, "type": t} for c, t in zip(entry.columns, entry.col_types)],
        "rows": rows,
        "row_count": len(rows),
        "rows_returned": len(rows),
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
    settings: Settings,
    server: Server,
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
    cnx = acquire_connection(settings, server, database)
    started = time.time()
    session = Session(cnx, settings)
    cur = session._cursor(sql)
    if cur.description is None:
        release_connection(server, database)
        return {
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
    col_types = [_type_name(d) for d in cur.description]
    qid = str(uuid.uuid4())
    entry = _OpenQuery(
        qid, cur, cnx, server, database, cols, col_types,
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
    return _page_result(entry, qid, rows, has_more, elapsed)


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
        return str(val)
    if isinstance(val, (datetime, date, dtime)):
        return val.isoformat()
    if isinstance(val, timedelta):
        return val.total_seconds()
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, (bytes, bytearray)):
        hexed = "0x" + bytes(val[:512]).hex()
        return hexed + (f"…[{len(val)} bytes]" if len(val) > 512 else "")
    return str(val)


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


def _type_name(col) -> str:
    t = col[1]
    return getattr(t, "__name__", str(t))


def fit_response(payload: dict, max_chars: int) -> dict:
    """Recorta la lista más larga del payload hasta que el JSON quepa en max_chars."""
    if payload.get("_response_rows_fitted"):
        payload.pop("_response_rows_fitted", None)
        return payload

    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False, default=str))

    if max_chars <= 0 or size() <= max_chars:
        return payload
    truncated = False
    for _ in range(40):
        lists = [(k, v) for k, v in payload.items() if isinstance(v, list) and len(v) > 1]
        nested = [(k, v) for k, v in payload.items() if isinstance(v, dict)]
        if not lists:
            for k, sub in nested:
                for sk, sv in sub.items():
                    if isinstance(sv, dict) and isinstance(sv.get("rows"), list) and len(sv["rows"]) > 1:
                        sv["rows"] = sv["rows"][: len(sv["rows"]) // 2]
                        sv["truncated"] = True
                        truncated = True
                        break
                else:
                    continue
                break
            else:
                break
        else:
            key, lst = max(lists, key=lambda kv: len(json.dumps(kv[1], default=str)))
            payload[key] = lst[: max(1, len(lst) // 2)]
            truncated = True
        if size() <= max_chars:
            break
    if truncated:
        payload["response_truncated"] = True
        if "row_count" in payload and isinstance(payload.get("rows"), list):
            payload["returned_rows"] = len(payload["rows"])
    return payload


# ============================================================
# Sesión
# ============================================================


class Session:
    def __init__(self, cnx: pyodbc.Connection, settings: Settings):
        self.cnx = cnx
        self.settings = settings

    def _cursor(self, sql: str, params: tuple = ()) -> pyodbc.Cursor:
        cur = self.cnx.cursor()
        cur.execute(sql, params) if params else cur.execute(sql)
        while cur.description is None and cur.nextset():
            pass
        return cur

    def dicts(self, sql: str, params: tuple = (), limit: int = 0) -> list[dict]:
        cur = self._cursor(sql, params)
        if cur.description is None:
            return []
        cols = _unique_columns(cur.description)
        rows = cur.fetchmany(limit) if limit else cur.fetchall()
        mc = self.settings.max_cell_chars
        return [{c: serialize_value(v, mc) for c, v in zip(cols, r)} for r in rows]

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        cur = self._cursor(sql, params)
        row = cur.fetchone() if cur.description is not None else None
        return serialize_value(row[0]) if row else None

    def table(
        self,
        sql: str,
        params: tuple = (),
        max_rows: int = 1000,
        max_cell_chars: Optional[int] = None,
    ) -> dict:
        """Columnas + filas como listas (compacto) y detección de truncado."""
        started = time.time()
        cur = self._cursor(sql, params)
        if cur.description is None:
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False,
                    "elapsed_ms": int((time.time() - started) * 1000)}
        cols = _unique_columns(cur.description)
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        mc = max_cell_chars if max_cell_chars is not None else self.settings.max_cell_chars
        return {
            "columns": [{"name": c, "type": _type_name(d)} for c, d in zip(cols, cur.description)],
            "rows": [[serialize_value(v, mc) for v in r] for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": int((time.time() - started) * 1000),
        }


@contextmanager
def session(settings: Settings, server: Server, database: Optional[str] = None) -> Iterator[Session]:
    cnx = acquire_connection(settings, server, database)
    try:
        yield Session(cnx, settings)
    finally:
        try:
            cnx.rollback()
        except Exception as e:  # pragma: no cover
            logger.debug(f"rollback: {e}")
        release_connection(server, database)


# ============================================================
# Bases accesibles (caché por alias)
# ============================================================

_DB_CACHE: dict[str, tuple[float, list[str]]] = {}
_DB_LOCK = threading.Lock()
_DB_TTL = 300


def accessible_databases(settings: Settings, server: Server, refresh: bool = False) -> list[str]:
    with _DB_LOCK:
        hit = _DB_CACHE.get(server.alias)
        if hit and not refresh and time.time() - hit[0] < _DB_TTL:
            return hit[1]
    with session(settings, server) as s:
        names = [r["name"] for r in s.dicts(
            "SELECT name FROM sys.databases WHERE HAS_DBACCESS(name) = 1 AND state = 0 ORDER BY name")]
    with _DB_LOCK:
        _DB_CACHE[server.alias] = (time.time(), names)
    return names


def resolve_database(settings: Settings, server: Server, database: Optional[str]) -> Optional[str]:
    """Nombre canónico de la base (case-insensitive) o error si no es accesible."""
    if not database or not database.strip():
        return None
    wanted = database.strip().strip("[]")
    names = accessible_databases(settings, server)
    for n in names:
        if n.lower() == wanted.lower():
            return n
    names = accessible_databases(settings, server, refresh=True)
    for n in names:
        if n.lower() == wanted.lower():
            return n
    raise LookupError(f"Base '{wanted}' no existe o no es accesible en '{server.alias}'. Accesibles: {names}")
