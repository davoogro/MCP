"""Pool de conexiones (sin red real)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from oracle_alarms_readonly_mcp.config import Settings
from oracle_alarms_readonly_mcp.pool import close_all_pools, get_pool, pool_stats


def _settings() -> Settings:
    return Settings(
        user="u",
        password="p",
        host="10.0.0.1",
        service="OSS",
        max_rows=5000,
        query_timeout_s=30,
        rc8_chunk_days=3,
    )


@patch("oracle_alarms_readonly_mcp.pool.oracledb.create_pool")
def test_get_pool_creates_once(mock_create):
    close_all_pools()
    mock_create.return_value = MagicMock()
    s = _settings()
    p1 = get_pool("rc1", s)
    p2 = get_pool("rc1", s)
    assert p1 is p2
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["min"] == 0
    assert mock_create.call_args.kwargs["max"] == 2
    close_all_pools()


def test_pool_stats_empty():
    close_all_pools()
    assert pool_stats()["pool_count"] == 0
