"""Inventory allowlist: only configured servers are reachable."""

from __future__ import annotations

import pytest

from windows_guarded_admin_mcp.exceptions import ServerNotFoundError, ValidationError
from windows_guarded_admin_mcp.services.diagnostics import DiagnosticsService


def test_list_servers_only_inventoried(app_config, policy_engine):
    svc = DiagnosticsService(app_config, policy_engine)
    result = svc.list_servers()
    assert result.success is True
    ids = {s["server_id"] for s in result.data["servers"]}
    assert ids == {"integration-server-01", "lab-server-02"}
    assert result.data["count"] == 2


def test_unknown_server_rejected(policy_engine):
    with pytest.raises(ServerNotFoundError, match="not in inventory"):
        policy_engine.get_server("unknown-host-99")


def test_invalid_server_id_rejected(policy_engine):
    with pytest.raises(ValidationError, match="Invalid server_id"):
        policy_engine.get_server("integration-server-01; Restart-Computer")


def test_empty_server_id_rejected(policy_engine):
    with pytest.raises(ValidationError):
        policy_engine.get_server("")


def test_inventoried_server_resolves(policy_engine):
    server = policy_engine.get_server("integration-server-01")
    assert server.hostname == "integration-server-01.example.local"
    assert server.permissions.read_operations is True


def test_local_config_merges_when_present(project_root, monkeypatch):
    """Real config dir may include servers.local.yaml; load must not crash."""
    monkeypatch.setenv("WSA_MCP_PROJECT_ROOT", str(project_root))
    from windows_guarded_admin_mcp.config import load_config, resolve_config_dir

    cfg = load_config(resolve_config_dir())
    assert len(cfg.servers) >= 1
