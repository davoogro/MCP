from __future__ import annotations

import pytest

from windows_winrm_fleet_mcp.config import reload_settings
from windows_winrm_fleet_mcp.util import (
    bound_response,
    human_size,
    is_protected_pid,
    is_protected_process,
    path_matches_denylist,
    validate_network_target,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    monkeypatch.delenv("WINFLEET_MAX_RESPONSE_CHARS", raising=False)
    reload_settings()
    yield
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    monkeypatch.delenv("WINFLEET_MAX_RESPONSE_CHARS", raising=False)
    reload_settings()


def test_human_size():
    assert human_size(1024) == "1.0 KB"


def test_denylist():
    patterns = ["*\\config\\SAM", "*.pfx"]
    assert path_matches_denylist(r"C:\Windows\System32\config\SAM", patterns)
    assert path_matches_denylist(r"D:\cert.pfx", patterns)
    assert not path_matches_denylist(r"D:\logs\app.log", patterns)


def test_protected_pid_and_process():
    assert is_protected_pid(4)
    assert is_protected_process("lsass")
    assert not is_protected_process("notepad")


def test_validate_network_target():
    assert validate_network_target("10.0.0.1") == "10.0.0.1"
    assert validate_network_target("host.example.com") == "host.example.com"
    try:
        validate_network_target("not valid!!")
        assert False
    except ValueError:
        pass


def test_bound_response_truncates(monkeypatch):
    monkeypatch.setenv("WINFLEET_MAX_RESPONSE_CHARS", "200")
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    from windows_winrm_fleet_mcp.config import reload_settings

    reload_settings()
    big = {"ok": True, "items": ["x" * 500]}
    out = bound_response(big)
    assert out.get("response_truncated") is True
