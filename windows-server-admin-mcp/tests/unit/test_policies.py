"""Policy engine permission and validation checks."""

from __future__ import annotations

import pytest

from windows_server_admin_mcp.exceptions import (
    AllowlistRejectedError,
    LocalhostBlockedError,
    PolicyDeniedError,
    ValidationError,
)


def test_read_operations_allowed(integration_server, policy_engine):
    policy_engine.check_read_operations(integration_server)


def test_server_restart_denied_integration(integration_server, policy_engine):
    with pytest.raises(PolicyDeniedError, match="restart_server"):
        policy_engine.check_server_restart(integration_server)


def test_service_restart_denied_integration(integration_server, policy_engine):
    with pytest.raises(PolicyDeniedError, match="restart_service"):
        policy_engine.check_service_restart(integration_server, "Spooler")


def test_service_restart_allowed_lab(lab_server, policy_engine):
    policy_engine.check_service_restart(lab_server, "Spooler")


def test_denylisted_service_rpcss(lab_server, policy_engine):
    with pytest.raises(PolicyDeniedError, match="protected service"):
        policy_engine.check_service_restart(lab_server, "RpcSs")


def test_denylisted_msexchange_prefix(lab_server, policy_engine):
    with pytest.raises(PolicyDeniedError):
        policy_engine.check_service_restart(lab_server, "MSEXCHANGESOMETHING")


def test_invalid_service_name_injection(lab_server, policy_engine):
    with pytest.raises(ValidationError):
        policy_engine.check_service_restart(lab_server, 'service"; shutdown /r /t 0; "')


def test_localhost_blocked():
    from windows_server_admin_mcp.policies import validate_hostname

    with pytest.raises(LocalhostBlockedError):
        validate_hostname("localhost")


def test_wildcard_hostname_blocked():
    from windows_server_admin_mcp.policies import validate_hostname

    with pytest.raises((AllowlistRejectedError, LocalhostBlockedError)):
        validate_hostname("*")


def test_unc_hostname_blocked():
    from windows_server_admin_mcp.policies import validate_hostname

    with pytest.raises(AllowlistRejectedError):
        validate_hostname(r"\\otroservidor\C$")


def test_shell_metachar_in_string_param():
    from windows_server_admin_mcp.policies import validate_string_param

    with pytest.raises(ValidationError, match="metacharacters"):
        validate_string_param("integration-server-01 && whoami")

    with pytest.raises(ValidationError, match="metacharacters"):
        validate_string_param("$(Get-Content secret.txt)")


def test_approved_diagnostic_only(policy_engine):
    policy_engine.validate_diagnostic("ping")
    with pytest.raises(ValidationError, match="not approved"):
        policy_engine.validate_diagnostic("format_c_drive")


def test_tcp_port_allowlist(policy_engine):
    policy_engine.validate_tcp_port(5986)
    with pytest.raises(ValidationError):
        policy_engine.validate_tcp_port(9999)


def test_reason_code_validation(policy_engine):
    desc = policy_engine.validate_reason_code("PLANNED_MAINTENANCE")
    assert "maintenance" in desc.lower()
    with pytest.raises(ValidationError):
        policy_engine.validate_reason_code("INVALID_CODE")


def test_reason_length_and_metachars():
    from windows_server_admin_mcp.policies import validate_reason

    with pytest.raises(ValidationError, match="10 and 500"):
        validate_reason("short")
    validate_reason("Valid documented reason for maintenance window")
    with pytest.raises(ValidationError, match="metacharacters"):
        validate_reason("Reason with ; injection attempt here")
