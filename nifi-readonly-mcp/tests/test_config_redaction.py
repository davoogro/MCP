from __future__ import annotations

import httpx
import pytest
import respx

from nifi_readonly_mcp.auth import AuthManager, looks_sensitive_key, redact_mapping
from nifi_readonly_mcp.client import NiFiClient, validate_debug_endpoint
from nifi_readonly_mcp.config import load_config, resolve_server_ids
from nifi_readonly_mcp.exceptions import ConfigError, DebugEndpointDenied
from nifi_readonly_mcp.models import GlobalConfig, ServerConfig, TlsConfig
from nifi_readonly_mcp.normalize import build_breadcrumb, normalize_processor
from nifi_readonly_mcp.responses import validate_limit
from nifi_readonly_mcp.services import NiFiService


def test_config_loads_flat_fields(sample_config_dir, monkeypatch):
    monkeypatch.setenv("NIFI_MCP_CONFIG_DIR", str(sample_config_dir))
    cfg = load_config()
    s = cfg.servers["lab01"]
    assert s.scheme == "https"
    assert s.port == 8443
    assert s.environment == "lab"
    assert s.base_url == "https://nifi-lab.local:8443/nifi-api"


def test_resolve_server_ids_defaults_all(sample_config_dir, monkeypatch):
    monkeypatch.setenv("NIFI_MCP_CONFIG_DIR", str(sample_config_dir))
    assert resolve_server_ids(None) == ["lab01"]
    assert resolve_server_ids(["lab01"]) == ["lab01"]


def test_validate_limit_rejects_zero():
    with pytest.raises(ConfigError):
        validate_limit(0, default=100, max_limit=1000)


def test_validate_limit_uses_default():
    assert validate_limit(None, default=100, max_limit=1000) == 100


def test_discovery_urls_prefer_configured_port():
    srv = ServerConfig(id="stl", name="STL", host="nifisrv05", port=8445)
    urls = srv.discovery_urls()
    assert urls[0] == "https://nifisrv05:8445/nifi-api"
    assert "http://nifisrv05:8445/nifi-api" in urls


def test_debug_denies_sensitive_content():
    for ep in (
        "/flowfile-queues/q1/flowfiles/f1/content",
        "/provenance-events/abc/content",
        "/flow/process-groups/root?recursive=true",
    ):
        with pytest.raises(DebugEndpointDenied):
            validate_debug_endpoint(ep)


def test_debug_allows_observational():
    assert validate_debug_endpoint("/flow/about") == "/flow/about"


def test_breadcrumb_root_prefix():
    assert build_breadcrumb(["PG1", "Proc"]) == "Root / PG1 / Proc"


def test_redact_sensitive_properties():
    data = {"username": "u", "password": "p", "nested": {"api_key": "k"}}
    redacted = redact_mapping(data)
    assert redacted["password"] == "[REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"


def test_normalize_processor_redacts_properties():
    proc = normalize_processor(
        "lab01",
        {"component": {"id": "x", "name": "P", "config": {"properties": {"Password": "x"}}}},
        include_details=True,
    )
    assert proc["details"]["properties"]["Password"] == "[REDACTED]"


def test_looks_sensitive_key():
    assert looks_sensitive_key("client_secret")
    assert not looks_sensitive_key("hostname")


@pytest.mark.asyncio
@respx.mock
async def test_discovery_accepts_401():
    global_cfg = GlobalConfig(default_timeout_seconds=5, default_retries=1)
    srv = ServerConfig(
        id="disc",
        name="Disc",
        host="nifi-disc.local",
        base_url=None,
        tls=TlsConfig(verify=False),
    )

    async def route(request):
        url = str(request.url)
        if url.endswith("/flow/about"):
            return httpx.Response(401, headers={"content-type": "application/json"}, text='{"nifi":"x"}')
        return httpx.Response(404)

    respx.route(url__regex=r"https?://nifi-disc\.local.*").mock(side_effect=route)
    client = NiFiClient(srv, global_cfg, AuthManager())
    base = await client.discover_base_url()
    assert "nifi-disc.local" in base
    assert any(a.status_code == 401 for a in client.discovery_attempts)
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_list_processors_uses_flow_endpoint(app_config, root_flow_response):
    svc = NiFiService(config=app_config)
    base = app_config.servers["lab01"].base_url
    root_flow_response["processGroupFlow"]["flow"]["processGroups"] = []
    respx.get(f"{base}/flow/process-groups/root").mock(
        return_value=httpx.Response(200, json=root_flow_response)
    )
    out = await svc.list_processors("lab01", recursive=False)
    assert out["items"][0]["path"].startswith("Root /")
    await svc.close()
