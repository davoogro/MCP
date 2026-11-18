"""Historial en memoria de consultas ejecutadas en la sesión del proceso MCP."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Optional

_MAX = 100
_LOCK = threading.Lock()
_ENTRIES: deque["_Entry"] = deque(maxlen=_MAX)


@dataclass
class _Entry:
    ts: float
    connection: Optional[str]
    database: Optional[str]
    sql: str
    row_count: Optional[int]
    elapsed_ms: Optional[int]
    error: Optional[str]
    tool: str


def record(
    *,
    tool: str,
    sql: str,
    connection: Optional[str] = None,
    database: Optional[str] = None,
    row_count: Optional[int] = None,
    elapsed_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    sql_trim = (sql or "").strip()
    if len(sql_trim) > 500:
        sql_trim = sql_trim[:500] + "…"
    with _LOCK:
        _ENTRIES.append(_Entry(
            time.time(), connection, database, sql_trim, row_count, elapsed_ms, error, tool,
        ))


def list_history(limit: int = 100) -> list[dict[str, Any]]:
    lim = max(1, min(limit, _MAX))
    with _LOCK:
        items = list(_ENTRIES)[-lim:]
    return [asdict(e) for e in reversed(items)]
