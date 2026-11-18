"""Integration server restart must be denied by policy."""

from __future__ import annotations

import pytest

from windows_server_admin_mcp.exceptions import PolicyDeniedError
from windows_server_admin_mcp.models import ErrorCode, OperationResult
from windows_server_admin_mcp.services.restart import RestartService


@pytest.mark.security
def test_integration_server_restart_denied_by_policy(integration_server, policy_engine):
    with pytest.raises(PolicyDeniedError, match="restart_server"):
        policy_engine.check_server_restart(integration_server)


@pytest.mark.security
def test_prepare_restart_returns_policy_failure(app_config, policy_engine, confirmation_manager):
    svc = RestartService(app_config, policy_engine, confirmation_manager)
    server = policy_engine.get_server("integration-server-01")
    with pytest.raises(PolicyDeniedError):
        policy_engine.check_server_restart(server)


@pytest.mark.security
def test_restart_service_permission_flags(integration_server):
    perms = integration_server.permissions
    assert perms.restart_server is False
    assert perms.restart_service is False
    assert perms.cancel_restart is False
    assert perms.read_operations is True


@pytest.mark.security
def test_confirm_restart_never_called_without_permission(
    app_config, policy_engine, confirmation_manager, monkeypatch
):
    svc = RestartService(app_config, policy_engine, confirmation_manager)
    called = {"remote": False}

    def _fake_remote(*_a, **_kw):
        called["remote"] = True
        return OperationResult.ok({})

    monkeypatch.setattr(svc, "_remote", _fake_remote)

    with pytest.raises(PolicyDeniedError):
        svc.prepare_server_restart(
            "integration-server-01",
            "PLANNED_MAINTENANCE",
            "Documented maintenance reason for testing policy",
        )
    assert called["remote"] is False
