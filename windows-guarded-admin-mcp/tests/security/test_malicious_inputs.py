"""Malicious input rejection across policy and validation layers."""

from __future__ import annotations

import pytest

from windows_guarded_admin_mcp.exceptions import (
    AllowlistRejectedError,
    LocalhostBlockedError,
    ScriptNotAuthorizedError,
    ServerNotFoundError,
    ValidationError,
)
from windows_guarded_admin_mcp.policies import (
    validate_hostname,
    validate_script_id,
    validate_server_id,
    validate_service_name,
    validate_string_param,
)


@pytest.mark.security
@pytest.mark.parametrize(
    "malicious_input,validator,error_type",
    [
        ("integration-server-01; Restart-Computer", validate_server_id, ValidationError),
        ("integration-server-01 && whoami", validate_string_param, ValidationError),
        ("$(Get-Content secret.txt)", validate_string_param, ValidationError),
        (r"\\otroservidor\C$", validate_hostname, AllowlistRejectedError),
        ('service"; shutdown /r /t 0; "', validate_service_name, ValidationError),
        ("*", validate_hostname, (AllowlistRejectedError, LocalhostBlockedError)),
        ("localhost", validate_hostname, LocalhostBlockedError),
    ],
)
def test_malicious_inputs_rejected(malicious_input, validator, error_type):
    with pytest.raises(error_type):
        validator(malicious_input)


@pytest.mark.security
def test_path_traversal_script_rejected(script_catalog):
    from windows_guarded_admin_mcp.models import ScriptEntry

    entry = ScriptEntry(
        script_id="traversal",
        path=r"..\..\script.ps1",
        allowed_servers=["integration-server-01"],
    )
    with pytest.raises(ScriptNotAuthorizedError):
        script_catalog.resolve_path(entry)


@pytest.mark.security
def test_command_injection_server_id_not_in_inventory(policy_engine):
    with pytest.raises(ValidationError):
        policy_engine.get_server("integration-server-01; Restart-Computer")


@pytest.mark.security
def test_unknown_server_not_reachable(policy_engine):
    with pytest.raises(ServerNotFoundError):
        policy_engine.get_server("attacker-controlled-host")


@pytest.mark.security
def test_invalid_script_id_rejected():
    with pytest.raises(ValidationError):
        validate_script_id("../script.ps1")
