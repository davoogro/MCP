"""Límites de filas, timeout y resolución de RCs."""

from __future__ import annotations

import pytest

from oss_fm_oracle_mcp.config import (
    PER_CALL_MAX_ROWS,
    PER_CALL_MAX_TIMEOUT_S,
    Settings,
    get_settings,
    resolve_rcs_optional,
)


def test_default_rows_500(monkeypatch):
    monkeypatch.delenv("ORACLE_FM_DEFAULT_ROWS", raising=False)
    s = Settings.from_env()
    assert s.default_rows == 500


def test_effective_rows_cap():
    s = Settings(
        user="u",
        password="p",
        host="h",
        service="OSS",
        max_rows=5000,
        query_timeout_s=30,
        rc8_chunk_days=3,
        default_rows=500,
    )
    assert s.effective_rows(None) == 500
    assert s.effective_rows(1000) == 1000
    assert s.effective_rows(PER_CALL_MAX_ROWS + 100) == PER_CALL_MAX_ROWS


def test_effective_timeout_cap():
    s = Settings(
        user="u",
        password="p",
        host="h",
        service="OSS",
        max_rows=5000,
        query_timeout_s=30,
        rc8_chunk_days=3,
    )
    assert s.effective_timeout(None) == 30
    assert s.effective_timeout(400) == PER_CALL_MAX_TIMEOUT_S


def test_resolve_rcs_optional_none():
    names = resolve_rcs_optional(None)
    assert len(names) == 6


def test_get_settings_cached(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("ORACLE_FM_DEFAULT_ROWS", "501")
    a = get_settings()
    monkeypatch.setenv("ORACLE_FM_DEFAULT_ROWS", "502")
    b = get_settings()
    assert a is b
    assert a.default_rows == 501
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
