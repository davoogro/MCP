"""Generación SQL de herramientas de alarmas (sin red)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from oss_fm_oracle_mcp.alarms import (
    _sanitize_dn_fragment,
    active_alarms_summary,
    alarm_history,
    alarm_timeline,
    top_alarms,
)


def test_sanitize_rejects_empty():
    with pytest.raises(ValueError):
        _sanitize_dn_fragment("  ***  ")


@patch("oss_fm_oracle_mcp.alarms.execute_query_many")
def test_active_summary_calls_group(mock_many):
    mock_many.return_value = {"ok": True, "results": []}
    active_alarms_summary(rcs=["rc1"], group_by="severity")
    assert mock_many.call_count == 2
    sql = mock_many.call_args_list[0][0][1]
    assert "CANCEL_TIME IS NULL" in sql
    assert "SEVERITY" in sql


@patch("oss_fm_oracle_mcp.alarms.execute_query_many")
def test_alarm_history_sanitizes_and_hours(mock_many):
    mock_many.return_value = {"ok": True}
    alarm_history("site-01", since_hours=12, rcs="rc2")
    sql = mock_many.call_args[0][1]
    assert "site-01" in sql
    assert "SYSDATE - (12/24)" in sql


@patch("oss_fm_oracle_mcp.alarms.execute_query_many")
def test_top_alarms_by_text(mock_many):
    mock_many.return_value = {"ok": True}
    top_alarms(by="text", top=5, rcs=None)
    sql = mock_many.call_args[0][1]
    assert "GROUP BY TEXT" in sql
    assert "FETCH FIRST 5 ROWS ONLY" in sql


@patch("oss_fm_oracle_mcp.alarms.execute_query_many")
def test_timeline_hour_bucket(mock_many):
    mock_many.return_value = {"ok": True}
    alarm_timeline(alarm_number=12345, bucket="hour", since_hours=48)
    sql = mock_many.call_args[0][1]
    assert "ALARM_NUMBER = 12345" in sql
    assert "TRUNC(ALARM_TIME, 'HH24')" in sql
