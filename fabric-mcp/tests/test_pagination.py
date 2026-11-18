"""Paginación: filas recortadas por tamaño de respuesta no se pierden entre query y fetch_more."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from fabric_mcp.config import Connection
from fabric_mcp.db import (
    _OPEN,
    _OPEN_LOCK,
    _OpenQuery,
    _take_query_page,
    fetch_query_page,
)


class _FakeCursor:
    def __init__(self, values: list[int]):
        self.description = [("id", str)]
        self._values = values
        self._pos = 0

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        chunk = self._values[self._pos : self._pos + size]
        self._pos += len(chunk)
        return [(v,) for v in chunk]

    def close(self) -> None:
        pass


class _FakeConnection:
    def close(self) -> None:
        pass


def _conn() -> Connection:
    return Connection(
        "t", "FABRIC_", "s", "db", "1433", "u", "p", "ActiveDirectoryPassword",
        "", True, 120, 0, True,
    )


def _entry(values: list[int], *, page_budget: int, max_cell: int = 0) -> _OpenQuery:
    return _OpenQuery(
        query_id="q-test",
        cur=_FakeCursor(values),  # type: ignore[arg-type]
        conn=_conn(),
        database=None,
        columns=["id"],
        created=0.0,
        last_used=0.0,
        max_cell_chars=max_cell,
        pending_rows=deque(),
        max_response_chars=page_budget,
        response_envelope={"ok": True, "connection": "t", "database": "db"},
    )


def test_response_budget_buffers_overflow_for_fetch_more():
    ids = list(range(1, 21))
    e = _entry(ids, page_budget=90)
    seen: list[int] = []
    while True:
        rows, has_more = _take_query_page(e, page_size=10)
        seen.extend(r[0] for r in rows)
        assert len(rows) >= 1
        if not has_more:
            break
    assert seen == ids


def test_fetch_query_page_closes_when_done():
    now = time.time()
    e = _entry([1, 2], page_budget=8000)
    e.created = now
    e.last_used = now
    with _OPEN_LOCK:
        _OPEN.clear()
        _OPEN["cid"] = e
    out = fetch_query_page("cid", page_size=10)
    assert out["row_count"] == 2
    assert out["next_cursor"] is None
    assert "cid" not in _OPEN
