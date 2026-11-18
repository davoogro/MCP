from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from windows_winrm_fleet_mcp.config import (
    deep_merge,
    reload_settings,
    resolve_server_ids,
    get_server,
)


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    reload_settings()
    yield
    monkeypatch.delenv("WINFLEET_CONFIG", raising=False)
    reload_settings()


def test_deep_merge_permissions():
    base = {"permissions": {"read": True, "manage_services": False}}
    over = {"permissions": {"manage_services": True}}
    merged = deep_merge(base, over)
    assert merged["permissions"]["read"] is True
    assert merged["permissions"]["manage_services"] is True


def test_resolve_all_and_tags():
    ids = resolve_server_ids("all")
    assert len(ids) >= 5
    apps = resolve_server_ids("tag:apps")
    assert "appsrv01" in apps
    env = resolve_server_ids("env:integration")
    assert "appsrv01" in env


def test_resolve_invalid():
    with pytest.raises(ValueError, match="inválido"):
        resolve_server_ids("no-such-server")


def test_permissions_default_read_only():
    srv = get_server("appsrv01")
    perms = srv.effective_permissions()
    assert perms["read"] is True
    assert perms["run_powershell"] is False


def test_local_override_merge(tmp_path, monkeypatch):
    main = tmp_path / "servers.yaml"
    local = tmp_path / "servers.local.yaml"
    main.write_text(
        yaml.dump(
            {
                "defaults": {"permissions": {"read": True, "run_powershell": False}},
                "servers": {"srv1": {"hostname": "srv1"}},
            }
        ),
        encoding="utf-8",
    )
    local.write_text(
        yaml.dump({"servers": {"srv1": {"permissions": {"run_powershell": True}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WINFLEET_CONFIG", str(main))
    reload_settings()
    from windows_winrm_fleet_mcp.config import get_settings

    s = get_settings().servers["srv1"]
    assert s.effective_permissions()["run_powershell"] is True
