from __future__ import annotations

import httpx
import pytest
import respx

from nifi_readonly_mcp.aggregate import AggregateService
from nifi_readonly_mcp.auth import AuthManager
from nifi_readonly_mcp.client import NiFiClient
from nifi_readonly_mcp.exceptions import NiFiClientError
from nifi_readonly_mcp.models import AuthConfig, AuthType, GlobalConfig, ServerConfig, TlsConfig
from nifi_readonly_mcp.services import NiFiService


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
    return GlobalConfig(default_timeout_seconds=5, default_retries=1)


@pytest.mark.asyncio
@respx.mock
async def test_get_about_success(server_cfg, global_cfg, about_response):
    respx.get("https://nifi-lab.local:8443/nifi-api/flow/about").mock(
        return_value=httpx.Response(200, json=about_response)
    )
    client = NiFiClient(server_cfg, global_cfg, AuthManager())
    about = await client.get_about()
    assert about["about"]["version"] == "1.25.0"
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_http_error_raises(server_cfg, global_cfg):
    respx.get("https://nifi-lab.local:8443/nifi-api/flow/about").mock(
        return_value=httpx.Response(503, json={"message": "down"})
    )
    client = NiFiClient(server_cfg, global_cfg, AuthManager())
    with pytest.raises(NiFiClientError) as exc:
        await client.get_json("/flow/about")
    assert exc.value.status_code == 503
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_auth_config_optional_on_connection(app_config, about_response):
    svc = NiFiService(config=app_config)
    base = app_config.servers["lab01"].base_url
    respx.get(f"{base}/flow/about").mock(return_value=httpx.Response(200, json=about_response))
    respx.get(f"{base}/authentication/configuration").mock(return_value=httpx.Response(403))
    respx.get(f"{base}/access/config").mock(return_value=httpx.Response(403))
    respx.get(f"{base}/flow/cluster/summary").mock(return_value=httpx.Response(200, json={"cluster": {}}))
    out = await svc.test_connection("lab01")
    assert out["ok"] is True
    assert "auth_configuration_unavailable" in out.get("warnings", [])
    await svc.close()


@pytest.mark.asyncio
@respx.mock
async def test_username_password_token_post_only(server_cfg, global_cfg, monkeypatch):
    server_cfg.auth = AuthConfig(
        type=AuthType.USERNAME_PASSWORD,
        username_env="NIFI_USER",
        password_env="NIFI_PASS",
    )
    monkeypatch.setenv("NIFI_USER", "u")
    monkeypatch.setenv("NIFI_PASS", "p")
    token_route = respx.post("https://nifi-lab.local:8443/nifi-api/access/token").mock(
        return_value=httpx.Response(200, text="tok123")
    )
    about_route = respx.get("https://nifi-lab.local:8443/nifi-api/flow/about").mock(
        return_value=httpx.Response(200, json={"about": {"version": "1.25.0"}})
    )
    client = NiFiClient(server_cfg, global_cfg, AuthManager())
    data = await client.get_about()
    assert data["about"]["version"] == "1.25.0"
    assert token_route.called
    assert about_route.calls[0].request.headers["authorization"] == "Bearer tok123"
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_multi_server_partial_errors(app_config, about_response):
    svc = NiFiService(config=app_config)
    agg = AggregateService(svc)
    base = app_config.servers["lab01"].base_url
    respx.get(f"{base}/flow/about").mock(return_value=httpx.Response(200, json=about_response))
    respx.get(f"{base}/authentication/configuration").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{base}/flow/cluster/summary").mock(return_value=httpx.Response(200, json={}))
    payload = await agg.test_all_connections()
    assert payload["total"] == 1
    assert payload["results"][0]["ok"] is True
    await agg.close()
