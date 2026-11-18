"""Script hash mismatch must block execution."""

from __future__ import annotations

import pytest

from windows_server_admin_mcp.exceptions import ScriptHashMismatchError
from windows_server_admin_mcp.services.scripts import ScriptsService


@pytest.mark.security
def test_hash_mismatch_blocks_script_run(app_config, policy_engine, project_root):
    svc = ScriptsService(app_config, policy_engine)
    with pytest.raises(ScriptHashMismatchError):
        svc.catalog.assert_allowed("bad_hash_script", "integration-server-01")


@pytest.mark.security
def test_scripts_service_propagates_hash_mismatch(app_config, policy_engine, monkeypatch):
    svc = ScriptsService(app_config, policy_engine)

    def _boom(*_a, **_kw):
        raise ScriptHashMismatchError("SHA256 mismatch", details={"expected_prefix": "000"})

    monkeypatch.setattr(svc.catalog, "assert_allowed", _boom)

    from windows_server_admin_mcp.exceptions import ScriptHashMismatchError as SHME

    with pytest.raises(SHME):
        svc.catalog.assert_allowed("bad_hash_script", "integration-server-01")
