"""OperationResult uniform envelope."""

from __future__ import annotations

from windows_guarded_admin_mcp.models import ErrorCode, OperationError, OperationResult


def test_ok_envelope():
    result = OperationResult.ok(
        {"servers": []},
        operation_id="op-1",
        server_id="integration-server-01",
        duration_ms=42,
        transport="winrm",
    )
    assert result.success is True
    assert result.data == {"servers": []}
    assert result.error is None
    assert result.operation_id == "op-1"
    assert result.server_id == "integration-server-01"
    assert result.duration_ms == 42
    assert result.transport == "winrm"
    assert result.timestamp.endswith("Z")


def test_fail_envelope():
    result = OperationResult.fail(
        ErrorCode.POLICY_DENIED,
        "Permission denied",
        details={"permission": "restart_server"},
        server_id="integration-server-01",
    )
    assert result.success is False
    assert result.data is None
    assert isinstance(result.error, OperationError)
    assert result.error.code == ErrorCode.POLICY_DENIED
    assert result.error.message == "Permission denied"
    assert result.error.details == {"permission": "restart_server"}


def test_model_dump_json_serializable():
    result = OperationResult.ok({"key": "value"})
    dumped = result.model_dump()
    assert dumped["success"] is True
    assert "timestamp" in dumped


def test_all_error_codes_are_strings():
    for code in ErrorCode:
        assert isinstance(code.value, str)
        fail = OperationResult.fail(code, "test")
        assert fail.error.code == code
