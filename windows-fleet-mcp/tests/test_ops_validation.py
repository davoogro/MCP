from __future__ import annotations

import pytest

from windows_fleet_mcp import ops
from windows_fleet_mcp.config import reload_settings
from windows_fleet_mcp.ops import ToolError
from windows_fleet_mcp.worker import FakeWorker, set_fake_worker


@pytest.fixture(autouse=True)
def _reset_inventory(monkeypatch):
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    reload_settings()
    yield
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    reload_settings()


def setup_function():
    set_fake_worker(
        FakeWorker(
            lambda req: {
                "ok": True,
                "data": {"whoami": "TEST\\user"},
            }
        )
    )


def teardown_function():
    set_fake_worker(None)


def test_manage_service_protected():
    try:
        ops.manage_service("appsrv01", "WinRM", action="stop", dry_run=True)
        assert False
    except ToolError as exc:
        assert exc.code == "NotAllowed"


def test_manage_service_wildcard_name():
    try:
        ops.manage_service("appsrv01", "Win*", action="start", dry_run=True)
        assert False
    except ToolError as exc:
        assert exc.code == "InvalidArgument"


def test_run_powershell_not_allowed():
    try:
        ops.run_powershell("appsrv01", "Get-Date")
        assert False
    except ToolError as exc:
        assert exc.code == "NotAllowed"


def test_read_denylist():
    try:
        ops.read_remote_file("appsrv01", r"C:\Windows\System32\config\SAM")
        assert False
    except ToolError as exc:
        assert exc.code == "NotAllowed"


def test_stop_process_protected_pid():
    try:
        ops.stop_process("appsrv01", pid=4, dry_run=True)
        assert False
    except ToolError as exc:
        assert exc.code == "NotAllowed"


def test_test_network_invalid_target():
    try:
        ops.test_network_from_server("appsrv01", "%%%bad")
        assert False
    except ToolError as exc:
        assert exc.code == "InvalidArgument"
