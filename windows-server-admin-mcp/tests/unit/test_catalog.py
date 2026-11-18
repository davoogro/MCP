"""Script catalog: hash verification and path traversal protection."""

from __future__ import annotations

import pytest

from windows_server_admin_mcp.config import compute_file_sha256
from windows_server_admin_mcp.exceptions import ScriptHashMismatchError, ScriptNotAuthorizedError
from windows_server_admin_mcp.models import ScriptEntry


def test_get_known_script(script_catalog):
    entry = script_catalog.get("smoke_test")
    assert entry.script_id == "smoke_test"


def test_unknown_script_rejected(script_catalog):
    with pytest.raises(ScriptNotAuthorizedError, match="not in catalog"):
        script_catalog.get("nonexistent_script")


def test_resolve_path_within_project(script_catalog, project_root):
    entry = script_catalog.get("smoke_test")
    path = script_catalog.resolve_path(entry)
    assert path.suffix == ".ps1"
    assert path.exists()
    assert path.is_relative_to(project_root / "authorized_scripts") or path.is_relative_to(
        project_root
    )


def test_path_traversal_rejected(script_catalog, app_config):
    entry = ScriptEntry(
        script_id="evil",
        path="../../outside.ps1",
        allowed_servers=["integration-server-01"],
    )
    with pytest.raises(ScriptNotAuthorizedError, match="escapes allowed directories"):
        script_catalog.resolve_path(entry)


def test_non_ps1_rejected(script_catalog, app_config):
    bad_file = app_config.authorized_scripts_dir / "test_evil.bat"
    bad_file.write_text("@echo off", encoding="utf-8")
    try:
        entry = ScriptEntry(
            script_id="bat",
            path="authorized_scripts/test_evil.bat",
            allowed_servers=["integration-server-01"],
        )
        with pytest.raises(ScriptNotAuthorizedError, match="Only .ps1"):
            script_catalog.resolve_path(entry)
    finally:
        bad_file.unlink(missing_ok=True)


def test_verify_hash_auto(script_catalog, project_root):
    entry = script_catalog.get("smoke_test")
    path = project_root / "authorized_scripts" / "smoke_test.ps1"
    digest = script_catalog.verify_hash(entry, path)
    assert len(digest) == 64
    assert digest == compute_file_sha256(path)


def test_verify_hash_mismatch(script_catalog, project_root):
    entry = script_catalog.get("bad_hash_script")
    path = project_root / "authorized_scripts" / "smoke_test.ps1"
    with pytest.raises(ScriptHashMismatchError):
        script_catalog.verify_hash(entry, path)


def test_server_allowlist(script_catalog):
    entry = script_catalog.get("smoke_test")
    assert script_catalog.is_allowed_for_server(entry, "integration-server-01") is True
    assert script_catalog.is_allowed_for_server(entry, "unknown") is False


def test_empty_allowlist_denies(script_catalog):
    entry = ScriptEntry(
        script_id="no_servers",
        path="authorized_scripts/smoke_test.ps1",
        allowed_servers=[],
    )
    assert script_catalog.is_allowed_for_server(entry, "integration-server-01") is False


def test_assert_allowed_full_flow(script_catalog):
    entry, path, digest = script_catalog.assert_allowed("smoke_test", "integration-server-01")
    assert entry.script_id == "smoke_test"
    assert path.exists()
    assert len(digest) == 64


def test_assert_allowed_wrong_server(script_catalog):
    with pytest.raises(ScriptNotAuthorizedError, match="not authorized for server"):
        script_catalog.assert_allowed("smoke_test", "nonexistent-server")
