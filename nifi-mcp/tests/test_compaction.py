"""Respuestas compactas, timestamps de bulletins, history y diagnósticos sin tope de 100."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from mcp import Client

from nifi_mcp.config import project_root, resolve_config_dir
from nifi_mcp.diagnostics import DiagnosticsService, _parse_ts
from nifi_mcp.models import AppConfig
from nifi_mcp.responses import as_tool_result, fit_payload
from nifi_mcp.server import mcp
from nifi_mcp.services import NiFiService, compact_status_history


def test_fit_payload_trims_largest_list_and_updates_counts():
    items = [{"id": i, "name": "x" * 200} for i in range(500)]
    payload = {"ok": True, "items": items, "total": 500, "returned": 500}
    text = fit_payload(payload, 20_000)
    assert len(text) <= 20_000
    data = json.loads(text)
    assert data["response_truncated"] is True
    assert data["truncated"] is True
    assert data["returned"] == len(data["items"]) < 500
    assert data["total"] == 500


def test_fit_payload_prefers_nested_long_lists_over_server_results():
    results = [
        {"server_id": f"s{i}", "findings": [{"m": "y" * 150} for _ in range(200)]}
        for i in range(4)
    ]
    data = json.loads(fit_payload({"ok": True, "results": results}, 30_000))
    assert len(data["results"]) == 4  # no se descartan servidores enteros


def test_as_tool_result_is_compact_and_structured():
    res = as_tool_result({"ok": True, "items": [1, 2]})
    assert res.content[0].text == '{"ok":true,"items":[1,2]}'
    assert res.structured_content == {"ok": True, "items": [1, 2]}
    assert not res.is_error


@pytest.mark.parametrize(
    "value,expected_hour_utc",
    [
        ("17:09:45 COT", 22),
        ("17:10:00 CDT", 22),
        ("12:00:00 UTC", 12),
        ("2026-10-14T12:00:00Z", 12),
    ],
)
def test_parse_bulletin_timestamps(value, expected_hour_utc):
    now = datetime(2026, 10, 14, 23, 0, tzinfo=timezone.utc)
    ts = _parse_ts(value, now)
    assert ts is not None
    assert ts.astimezone(timezone.utc).hour == expected_hour_utc
    assert ts <= now


def test_parse_bulletin_timestamp_rolls_back_a_day():
    now = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)  # 20:00 COT del día 13
    ts = _parse_ts("23:59:00 COT", now)
    assert ts is not None and ts < now


def test_compact_status_history():
    history = {
        "generated": "x",
        "componentDetails": {"Name": "P"},
        "fieldDescriptors": [{"field": "bytesRead", "label": "Bytes Read", "description": "…"}],
        "aggregateSnapshots": [{"timestamp": i, "statusMetrics": {"bytesRead": i}} for i in range(100)],
        "nodeSnapshots": [{"nodeId": "n1", "statusSnapshots": []}],
    }
    out = compact_status_history(history, 10)
    assert out["points_total"] == 100
    assert len(out["aggregateSnapshots"]) == 10
    assert out["aggregateSnapshots"][-1]["timestamp"] == 99
    assert out["fields"] == {"bytesRead": "Bytes Read"}
    assert "nodeSnapshots" not in out


@pytest.mark.asyncio
async def test_find_stopped_sees_more_than_default_limit(app_config: AppConfig):
    service = NiFiService(config=app_config)
    procs = [
        {"server": "lab01", "id": f"p{i}", "name": f"P{i}", "type": "processor", "state": "STOPPED"}
        for i in range(250)
    ]

    async def fake_all(server_id, *, include_details=False, refresh=False):
        return procs

    service.all_processors = fake_all  # type: ignore[method-assign]
    diag = DiagnosticsService(service)
    out = await diag.find_stopped_processors("lab01")
    assert out["total"] == 250
    assert out["returned"] == app_config.global_config.default_limit
    assert out["truncated"] is True


def test_paths_do_not_depend_on_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("NIFI_MCP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("NIFI_MCP_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    root = project_root()
    assert (root / "src" / "nifi_mcp").is_dir()
    assert resolve_config_dir() == root / "config"


@pytest.mark.asyncio
async def test_tools_have_readonly_annotations_and_instructions():
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        assert all(t.annotations.open_world_hint for t in tools)
        assert "nifi_health_overview" in (client.instructions or "")
