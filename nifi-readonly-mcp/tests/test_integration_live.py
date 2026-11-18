from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NIFI_MCP_LIVE_TESTS") != "1",
    reason="live tests disabled",
)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_readonly_connections_and_status():
    from nifi_readonly_mcp.aggregate import AggregateService

    agg = AggregateService()
    try:
        inventory = await agg.list_servers()
        assert inventory["ok"] is True
        assert inventory["total"] >= 1

        connections = await agg.test_all_connections()
        assert connections["total"] == len(
            [item for item in inventory["items"] if item["enabled"]]
        )

        # Solo GET: confirma una lectura de status en cada NiFi accesible.
        reachable = [item["server_id"] for item in connections["results"] if item.get("ok")]
        for server_id in reachable:
            status = await agg.service.get_status(server_id, recursive=False, refresh=True)
            assert status["ok"] is True
            assert "status" in status
    finally:
        await agg.close()
