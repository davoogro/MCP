from __future__ import annotations

import pytest
from mcp import Client

from nifi_readonly_mcp.server import mcp

EXPECTED_TOOLS = {
    "nifi_list_servers",
    "nifi_server_info",
    "nifi_test_connection",
    "nifi_test_all_connections",
    "nifi_get_root_flow",
    "nifi_list_process_groups",
    "nifi_get_process_group",
    "nifi_list_processors",
    "nifi_get_processor",
    "nifi_search",
    "nifi_get_status",
    "nifi_get_process_group_status",
    "nifi_get_processor_status",
    "nifi_get_connection_status",
    "nifi_get_bulletins",
    "nifi_get_errors",
    "nifi_health_check",
    "nifi_health_overview",
    "nifi_find_processors",
    "nifi_find_large_queues",
    "nifi_find_stopped_processors",
    "nifi_find_invalid_components",
    "nifi_compare_servers",
    "nifi_list_controller_services",
    "nifi_find_problem_controller_services",
    "nifi_list_parameter_contexts",
    "nifi_processor_status_history",
    "nifi_process_group_status_history",
    "nifi_list_connections",
    "nifi_get_provenance",
    "nifi_list_queue",
    "nifi_cluster_status",
    "nifi_get_controller_service",
    "nifi_flow_versions",
    "nifi_get_parameter_context",
    "nifi_system_diagnostics",
    "nifi_debug_get",
}


@pytest.mark.asyncio
async def test_mcp_lists_all_tools():
    async with Client(mcp) as client:
        tools_result = await client.list_tools()
        names = {t.name for t in tools_result.tools}
        assert names == EXPECTED_TOOLS
        assert len(names) == 37


@pytest.mark.asyncio
async def test_mcp_returns_structured_dict():
    async with Client(mcp) as client:
        result = await client.call_tool("nifi_list_servers", {})
        sc = result.structured_content
        assert isinstance(sc, dict)
        assert sc.get("ok") is True
        assert "items" in sc


@pytest.mark.asyncio
async def test_search_tool_schema_defaults_multi():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["nifi_search"].input_schema
        assert "query" in schema.get("properties", {})
        assert "server_id" in schema.get("properties", {})
