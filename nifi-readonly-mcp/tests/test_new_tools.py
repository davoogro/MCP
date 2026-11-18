from __future__ import annotations

import httpx
import pytest
import respx

from nifi_readonly_mcp.services import NiFiService


@pytest.mark.asyncio
@respx.mock
async def test_get_provenance_service(app_config):
    svc = NiFiService(config=app_config)
    base = app_config.servers["lab01"].base_url
    respx.post(f"{base}/provenance").mock(
        return_value=httpx.Response(200, json={"provenance": {"id": "p1"}})
    )
    respx.get(f"{base}/provenance/p1").mock(
        return_value=httpx.Response(
            200,
            json={
                "provenance": {
                    "id": "p1",
                    "finished": True,
                    "results": {"provenanceEvents": [{"eventId": 9}]},
                }
            },
        )
    )
    respx.delete(f"{base}/provenance/p1").mock(return_value=httpx.Response(200, json={}))

    out = await svc.get_provenance("lab01", max_results=10, since_minutes=5)
    assert out["ok"] is True
    assert out["items"][0]["eventId"] == 9
    assert "provenance" in out.get("note", "").lower() or "POST" in out.get("note", "")
    await svc.close()


@pytest.mark.asyncio
@respx.mock
async def test_list_connections_merges_queue(app_config, root_flow_response):
    svc = NiFiService(config=app_config)
    base = app_config.servers["lab01"].base_url
    flow = dict(root_flow_response)
    flow["processGroupFlow"]["flow"]["connections"] = [
        {
            "component": {
                "id": "c1",
                "name": "A->B",
                "source": {"id": "p1", "name": "Proc1", "type": "PROCESSOR"},
                "destination": {"id": "p2", "name": "Proc2", "type": "PROCESSOR"},
            }
        }
    ]
    respx.get(f"{base}/flow/process-groups/root").mock(return_value=httpx.Response(200, json=flow))
    respx.get(f"{base}/flow/process-groups/pg1").mock(
        return_value=httpx.Response(
            200,
            json={
                "processGroupFlow": {
                    "flow": {"processGroups": [], "processors": [], "connections": []},
                    "breadcrumb": {"name": "PG1"},
                }
            },
        )
    )
    respx.get(f"{base}/flow/process-groups/root/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "processGroupStatus": {
                    "connectionStatusSnapshots": [
                        {
                            "connection": {"id": "c1", "name": "A->B"},
                            "connectionStatusSnapshot": {
                                "aggregateSnapshot": {
                                    "flowFilesQueued": 42,
                                    "bytesQueued": 100,
                                    "percentUseCount": "42%",
                                    "percentUseBytes": "10%",
                                }
                            },
                        }
                    ]
                }
            },
        )
    )

    out = await svc.list_connections("lab01", min_queued=1)
    assert out["ok"] is True
    assert out["items"][0]["queue"]["flowFilesQueued"] == 42
    assert out["items"][0]["details"]["source"]["name"] == "Proc1"
    await svc.close()
