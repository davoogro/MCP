from __future__ import annotations

import httpx
import pytest
import respx

from nifi_mcp.auth import AuthManager
from nifi_mcp.client import NiFiClient
from nifi_mcp.exceptions import NiFiClientError
from nifi_mcp.models import AuthConfig, AuthType, GlobalConfig, ServerConfig, TlsConfig


@pytest.fixture
def server_cfg() -> ServerConfig:
    return ServerConfig(
        id="lab01",
        name="Lab",
        host="nifi-lab.local",
        base_url="https://nifi-lab.local:8443/nifi-api",
        tls=TlsConfig(verify=False),
        auth=AuthConfig(type=AuthType.NONE),
    )


@pytest.fixture
def global_cfg() -> GlobalConfig:
    return GlobalConfig(default_timeout_seconds=5, default_retries=0)


@pytest.mark.asyncio
async def test_post_delete_not_on_allowlist(server_cfg, global_cfg):
    client = NiFiClient(server_cfg, global_cfg, AuthManager())
    with pytest.raises(NiFiClientError, match="Método no permitido"):
        await client.post_json_read_only("/processors/x", {})
    with pytest.raises(NiFiClientError, match="Método no permitido"):
        await client.delete_json_read_only("/processors/x")
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_provenance_lifecycle(server_cfg, global_cfg):
    base = server_cfg.base_url
    create = respx.post(f"{base}/provenance").mock(
        return_value=httpx.Response(200, json={"provenance": {"id": "q1"}})
    )
    poll = respx.get(f"{base}/provenance/q1").mock(
        return_value=httpx.Response(
            200,
            json={
                "provenance": {
                    "id": "q1",
                    "finished": True,
                    "results": {"provenanceEvents": [{"eventId": 1, "eventType": "CREATE"}]},
                }
            },
        )
    )
    delete = respx.delete(f"{base}/provenance/q1").mock(return_value=httpx.Response(200, json={}))

    client = NiFiClient(server_cfg, global_cfg, AuthManager())
    created = await client.post_json_read_only("/provenance", {"provenance": {"request": {}}})
    assert created["provenance"]["id"] == "q1"
    data = await client.get_json("/provenance/q1")
    assert data["provenance"]["finished"] is True
    await client.delete_json_read_only("/provenance/q1")
    assert create.called and poll.called and delete.called
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_queue_listing_lifecycle(server_cfg, global_cfg):
    base = server_cfg.base_url
    conn = "11111111-1111-1111-1111-111111111111"
    create = respx.post(f"{base}/flowfile-queues/{conn}/listing-requests").mock(
        return_value=httpx.Response(200, json={"listingRequest": {"id": "lr1"}})
    )
    poll = respx.get(f"{base}/flowfile-queues/{conn}/listing-requests/lr1").mock(
        return_value=httpx.Response(
            200,
            json={
                "listingRequest": {
                    "id": "lr1",
                    "finished": True,
                    "flowFileSummaries": [{"uuid": "f1", "filename": "a.txt"}],
                }
            },
        )
    )
    delete = respx.delete(f"{base}/flowfile-queues/{conn}/listing-requests/lr1").mock(
        return_value=httpx.Response(200, json={})
    )

    client = NiFiClient(server_cfg, global_cfg, AuthManager())
    await client.post_json_read_only(f"/flowfile-queues/{conn}/listing-requests", {})
    listing = await client.get_json(f"/flowfile-queues/{conn}/listing-requests/lr1")
    assert listing["listingRequest"]["finished"] is True
    await client.delete_json_read_only(f"/flowfile-queues/{conn}/listing-requests/lr1")
    assert create.called and poll.called and delete.called
    await client.close()
