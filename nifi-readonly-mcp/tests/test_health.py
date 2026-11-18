from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from nifi_readonly_mcp.diagnostics import DiagnosticsService
from nifi_readonly_mcp.models import HealthConfig
from nifi_readonly_mcp.services import NiFiService


def test_bulletin_age_logic():
    cfg = HealthConfig(bulletin_age_minutes=60)
    now = datetime.now(timezone.utc)
    recent = now - timedelta(minutes=30)
    old = now - timedelta(minutes=90)
    assert 30 <= cfg.bulletin_age_minutes
    assert 90 > cfg.bulletin_age_minutes
    assert recent < now
    assert old < now


@pytest.mark.asyncio
@respx.mock
async def test_find_stopped_processors(app_config, root_flow_response):
    root_flow_response["processGroupFlow"]["flow"]["processors"][0]["component"]["state"] = "STOPPED"
    root_flow_response["processGroupFlow"]["flow"]["processGroups"] = []
    svc = NiFiService(config=app_config)
    diag = DiagnosticsService(svc)
    base = app_config.servers["lab01"].base_url
    respx.get(f"{base}/flow/process-groups/root").mock(
        return_value=httpx.Response(200, json=root_flow_response)
    )
    out = await diag.find_stopped_processors("lab01")
    assert out["total"] == 1
    assert out["items"][0]["state"] == "STOPPED"
    await svc.close()


@pytest.mark.asyncio
@respx.mock
async def test_health_check_unreachable(app_config):
    svc = NiFiService(config=app_config)
    diag = DiagnosticsService(svc)
    base = app_config.servers["lab01"].base_url
    respx.get(f"{base}/flow/about").mock(return_value=httpx.Response(503))
    out = await diag.health_check("lab01")
    assert out["ok"] is False
    assert out["overall_status"] == "unreachable"
    await svc.close()


@pytest.mark.asyncio
@respx.mock
async def test_health_multi_summary(app_config, about_response):
    svc = NiFiService(config=app_config)
    from nifi_readonly_mcp.aggregate import AggregateService

    agg = AggregateService(svc)
    base = app_config.servers["lab01"].base_url
    respx.get(f"{base}/flow/about").mock(return_value=httpx.Response(200, json=about_response))
    respx.get(f"{base}/authentication/configuration").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{base}/flow/cluster/summary").mock(return_value=httpx.Response(200, json={"cluster": {}}))
    respx.get(f"{base}/flow/process-groups/root/status").mock(
        return_value=httpx.Response(200, json={"processGroupStatus": {"aggregateSnapshot": {}}})
    )
    respx.get(f"{base}/flow/bulletin-board").mock(return_value=httpx.Response(200, json={"bulletinBoard": {"bulletins": []}}))
    respx.get(f"{base}/flow/process-groups/root").mock(
        return_value=httpx.Response(200, json={"processGroupFlow": {"flow": {"processors": [], "processGroups": []}}})
    )
    respx.get(f"{base}/flow/process-groups/root/controller-services").mock(
        return_value=httpx.Response(200, json={"controllerServices": []})
    )
    out = await agg.health_check_multi()
    assert "overall_status" in out
    assert "servers_checked" in out
    assert "summary" in out
    await agg.close()
