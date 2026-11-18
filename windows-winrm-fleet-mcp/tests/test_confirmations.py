from __future__ import annotations

import time

from windows_winrm_fleet_mcp.confirmations import RestartConfirmationManager


def test_restart_token_single_use_and_binding():
    mgr = RestartConfirmationManager()
    params = {"reason": "mantenimiento", "delay_seconds": 60}
    token, rec = mgr.issue(server_id="srv1", params=params)
    assert rec.token_prefix == token[:8]
    mgr.consume(token, server_id="srv1", params=params)
    try:
        mgr.consume(token, server_id="srv1", params=params)
        assert False
    except ValueError as exc:
        assert "utilizado" in str(exc) or "inválido" in str(exc)


def test_restart_token_wrong_params():
    mgr = RestartConfirmationManager()
    params = {"reason": "a", "delay_seconds": 60}
    token, _ = mgr.issue(server_id="srv1", params=params)
    try:
        mgr.consume(token, server_id="srv1", params={"reason": "b", "delay_seconds": 60})
        assert False
    except ValueError:
        pass


def test_restart_token_ttl(monkeypatch):
    mgr = RestartConfirmationManager()
    mgr.TTL_SECONDS = 1
    params = {"reason": "x", "delay_seconds": 60}
    token, _ = mgr.issue(server_id="srv1", params=params)
    time.sleep(1.2)
    try:
        mgr.consume(token, server_id="srv1", params=params)
        assert False
    except ValueError as exc:
        assert "expirado" in str(exc)
