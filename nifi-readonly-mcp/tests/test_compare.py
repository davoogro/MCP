from __future__ import annotations

import httpx
import pytest
import respx

from nifi_readonly_mcp.aggregate import AggregateService
from nifi_readonly_mcp.services import NiFiService


@pytest.mark.asyncio
@respx.mock
async def test_compare_servers_metrics(app_config, about_response, root_flow_response):
    svc = NiFiService(config=app_config)
    agg = AggregateService(svc)
    base = app_config.servers["lab01"].base_url
    root_flow_response["processGroupFlow"]["flow"]["processGroups"] = []
    respx.get(f"{base}/flow/about").mock(return_value=httpx.Response(200, json=about_response))
    respx.get(f"{base}/authentication/configuration").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{base}/flow/cluster/summary").mock(return_value=httpx.Response(200, json={"cluster": {}}))
    respx.get(f"{base}/flow/process-groups/root").mock(
        return_value=httpx.Response(200, json=root_flow_response)
    )
    respx.get(f"{base}/flow/process-groups/root/status").mock(
        return_value=httpx.Response(
            200,
            json={"processGroupStatus": {"aggregateSnapshot": {"flowFilesQueued": 5, "bytesQueued": 100}}},
        )
    )
    respx.get(f"{base}/flow/bulletin-board").mock(
        return_value=httpx.Response(
            200,
            json={
                "bulletinBoard": {
                    "bulletins": [
                        {"bulletin": {"level": "ERROR", "message": "x"}},
                        {"bulletin": {"level": "WARN", "message": "y"}},
                    ]
                }
            },
        )
    )
    out = await agg.compare_servers()
    comp = out["comparisons"][0]
    for key in (
        "version",
        "cluster_mode",
        "processor_total",
        "processor_running",
        "processor_stopped",
        "processor_invalid",
        "process_group_count",
        "queued_flowfiles",
        "queued_bytes",
        "bulletins_error",
        "bulletins_warn",
    ):
        assert key in comp, key
    await agg.close()
