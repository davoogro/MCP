"""Smoke MCP in-process (sin red): registro de tools, anotaciones e instrucciones."""

from __future__ import annotations

import asyncio
import json

from mcp import Client

from oss_fm_oracle_mcp.server import mcp

EXPECTED = {
    "list_rcs",
    "test_connection",
    "describe_table",
    "query",
    "query_all",
    "active_alarms_summary",
    "alarm_history",
    "top_alarms",
    "alarm_timeline",
    "list_tables",
    "explain_query",
    "export_query",
}


def test_tools_annotations_and_instructions():
    async def run():
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == EXPECTED
            for t in tools.values():
                assert t.annotations is not None
                assert t.annotations.read_only_hint is True
                assert t.annotations.open_world_hint is True
                assert t.description
            assert "ALARM_LOG" in (client.instructions or "")

            res = await client.call_tool("list_rcs", {"probe": False})
            assert not res.is_error
            data = json.loads(res.content[0].text)
            assert data["count"] == 6
            assert "password" not in res.content[0].text.lower()

            bad = await client.call_tool("query", {"rc": "rc1", "sql": "DELETE FROM t"})
            assert bad.is_error

    asyncio.run(run())
