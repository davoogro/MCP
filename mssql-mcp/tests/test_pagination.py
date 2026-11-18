"""Paginación: filas recortadas por tamaño de respuesta no se pierden entre query y fetch_more."""

from __future__ import annotations

from collections import deque
from typing import Any

import time

from mssql_mcp.config import Server
from mssql_mcp.db import (
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
    def rollback(self) -> None:
        pass


def _entry(values: list[int], *, page_budget: int, max_cell: int = 0) -> _OpenQuery:
    srv = Server(alias="t", host="127.0.0.1", port=1)
    return _OpenQuery(
        query_id="q-test",
        cur=_FakeCursor(values),  # type: ignore[arg-type]
        cnx=_FakeConnection(),  # type: ignore[arg-type]
        server=srv,
        database=None,
        columns=["id"],
        col_types=["str"],
        created=0.0,
        last_used=0.0,
        max_cell_chars=max_cell,
        pending_rows=deque(),
        max_response_chars=page_budget,
        response_envelope={"ok": True, "connection": "t", "database": "(default del login)"},
    )


def test_response_budget_buffers_overflow_for_fetch_more():
    """Recorte por max_response_chars: todas las filas llegan en varias páginas pequeñas."""
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


def test_no_row_skipped_across_pages():
    all_seen: list[int] = []
    ids = list(range(1, 101))
    e = _entry(ids, page_budget=800)
    while True:
        rows, has_more = _take_query_page(e, page_size=25)
        all_seen.extend(r[0] for r in rows)
        if not has_more:
            break
    assert all_seen == ids
    assert e.cur._pos == len(ids)


def test_fetch_query_page_drains_buffer_before_sql_cursor(monkeypatch):
    """fetch_query_page entrega primero pending_rows (filas 1251-5000 no se saltan)."""
    srv = Server(alias="t", host="127.0.0.1", port=1)
    cur = _FakeCursor(list(range(5001, 12001)))
    entry = _OpenQuery(
        query_id="live-cursor",
        cur=cur,  # type: ignore[arg-type]
        cnx=_FakeConnection(),  # type: ignore[arg-type]
        server=srv,
        database=None,
        columns=["id"],
        col_types=["str"],
        created=time.time(),
        last_used=time.time(),
        max_cell_chars=0,
        pending_rows=deque([[i] for i in range(1251, 5001)]),
        max_response_chars=500_000,
        response_envelope={"ok": True, "connection": "t", "database": "(default del login)"},
    )
    with _OPEN_LOCK:
        _OPEN["live-cursor"] = entry

    closed: list[str] = []

    def _fake_close(e: _OpenQuery) -> None:
        closed.append(e.query_id)

    monkeypatch.setattr("mssql_mcp.db._close_open", _fake_close)

    out = fetch_query_page("live-cursor", page_size=3750)
    first_ids = [r[0] for r in out["rows"]]
    assert first_ids[0] == 1251
    assert first_ids[-1] == 5000
    assert out["rows_returned"] == 3750
    assert out["has_more"]
    assert cur._pos == 0


def test_twelve_thousand_rows_contiguous_with_tight_response_budget():
    """12000 filas numeradas: ninguna se pierde pese a respuestas JSON pequeñas."""
    ids = list(range(1, 12001))
    e = _entry(ids, page_budget=120)
    seen: list[int] = []
    while True:
        rows, has_more = _take_query_page(e, page_size=5000)
        seen.extend(r[0] for r in rows)
        if not has_more:
            break
    assert seen == ids
    assert e.cur._pos == len(ids)
