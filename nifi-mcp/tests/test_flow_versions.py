from __future__ import annotations

import httpx
import pytest
import respx

from nifi_mcp.services import NiFiService


@pytest.mark.asyncio
@respx.mock
async def test_flow_versions_recurses_child_without_version_control(app_config):
    """Hijos sin versionControlInformation deben poder recorrerse sin UnboundLocalError."""
    svc = NiFiService(config=app_config)
    base = app_config.servers["lab01"].base_url

    root_flow = {
        "processGroupFlow": {
            "breadcrumb": {"name": "NiFi Flow"},
            "component": {"id": "root-id", "name": "NiFi Flow"},
            "flow": {
                "processGroups": [
                    {
                        "component": {
                            "id": "pg-nested",
                            "name": "NestedPG",
                        }
                    }
                ],
                "processors": [],
                "connections": [],
            },
        }
    }
    nested_flow = {
        "processGroupFlow": {
            "breadcrumb": {"name": "NestedPG"},
            "component": {
                "id": "pg-nested",
                "name": "NestedPG",
                "versionControlInformation": {
                    "state": "UP_TO_DATE",
                    "registryId": "reg1",
                    "registryName": "Reg",
                    "bucketId": "b1",
                    "bucketName": "Bucket",
                    "flowId": "f1",
                    "flowName": "Flow",
                    "version": "3",
                },
            },
            "flow": {"processGroups": [], "processors": [], "connections": []},
        }
    }

    respx.get(f"{base}/flow/process-groups/root").mock(
        return_value=httpx.Response(200, json=root_flow)
    )
    respx.get(f"{base}/flow/process-groups/pg-nested").mock(
        return_value=httpx.Response(200, json=nested_flow)
    )

    out = await svc.flow_versions("lab01")
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["items"][0]["name"] == "NestedPG"
    assert out["items"][0]["version"] == "3"
    await svc.close()
