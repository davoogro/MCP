"""MCP server surface: tool metadata/annotations and cwd-independent stdio startup."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from windows_server_admin_mcp.exceptions import LocalhostBlockedError
from windows_server_admin_mcp.policies import PolicyEngine, is_local_host

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"

READ_ONLY_TOOLS = {
    "list_servers",
    "diagnose_server_access",
    "get_server_identity",
    "get_server_health",
    "get_disks",
    "get_services",
    "get_processes",
    "get_windows_events",
    "test_remote_execution",
    "wait_for_server",
    "get_operation_status",
    "query_audit_log",
}
MUTATING_TOOLS = {
    "run_authorized_script",
    "restart_service",
    "prepare_server_restart",
    "confirm_server_restart",
    "cancel_server_restart",
}


@pytest.fixture
def server_module(test_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WSA_MCP_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("WSA_MCP_CONFIG_DIR", str(test_config_dir))
    monkeypatch.setenv("WSA_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    import windows_server_admin_mcp.server as server

    return importlib.reload(server)


def _annotation(tool, camel: str, snake: str):
    ann = tool.annotations
    return getattr(ann, snake, None) if hasattr(ann, snake) else getattr(ann, camel, None)


def test_tools_have_safety_annotations(server_module):
    tools = asyncio.run(server_module.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == READ_ONLY_TOOLS | MUTATING_TOOLS
    for tool in tools:
        assert tool.description and len(tool.description) > 20
        read_only = _annotation(tool, "readOnlyHint", "read_only_hint")
        destructive = _annotation(tool, "destructiveHint", "destructive_hint")
        if tool.name in MUTATING_TOOLS:
            assert read_only is False, tool.name
            assert destructive is True, tool.name
        else:
            assert read_only is True, tool.name


def test_restart_service_defaults_to_dry_run(server_module):
    tools = {t.name: t for t in asyncio.run(server_module.mcp.list_tools())}
    schema = tools["restart_service"].input_schema if hasattr(
        tools["restart_service"], "input_schema"
    ) else tools["restart_service"].inputSchema
    assert schema["properties"]["dry_run"]["default"] is True


def test_audit_dir_env_override(server_module, tmp_path: Path):
    assert Path(server_module.AUDIT.log_dir) == (tmp_path / "audit").resolve()


def test_local_host_restart_blocked(app_config, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WSA_MCP_LOCAL_HOSTNAME", "lab-server-02.example.local")
    server = app_config.servers["lab-server-02"]
    server.permissions.restart_server = True
    assert is_local_host(server.hostname)
    with pytest.raises(LocalhostBlockedError):
        PolicyEngine(app_config).check_server_restart(server)


def test_remote_hosts_not_flagged_local(app_config):
    assert not is_local_host(app_config.servers["integration-server-01"].hostname)


def test_dispatcher_avoids_ps7_only_ashashtable():
    source = (PROJECT_ROOT / "powershell" / "Invoke-McpRemoteOperation.ps1").read_text(
        encoding="utf-8"
    )
    code_lines = [ln for ln in source.splitlines() if not ln.strip().startswith("#")]
    assert not any("-AsHashtable" in ln for ln in code_lines)
    assert "HASH_INVALID" in source


def _rpc(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, msg_id: int) -> dict:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout")
        msg = json.loads(line)
        if msg.get("id") == msg_id:
            return msg


def test_stdio_handshake_from_foreign_cwd(test_config_dir: Path, tmp_path: Path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT)
    env["WSA_MCP_CONFIG_DIR"] = str(test_config_dir)
    env["WSA_MCP_AUDIT_DIR"] = str(tmp_path / "audit")
    env.pop("WSA_MCP_PROJECT_ROOT", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "windows_server_admin_mcp.server"],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    watchdog = threading.Timer(60, proc.kill)  # never hang the suite
    watchdog.start()
    try:
        _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
        )
        init = _read_response(proc, 1)
        assert init["result"]["serverInfo"]["name"] == "windows-server-admin-mcp"
        assert "two-step" in init["result"].get("instructions", "")
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = _read_response(proc, 2)["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert by_name["confirm_server_restart"]["annotations"]["destructiveHint"] is True
        assert by_name["list_servers"]["annotations"]["readOnlyHint"] is True
        _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_servers", "arguments": {}},
            },
        )
        call = _read_response(proc, 3)["result"]
        text = call["content"][0]["text"]
        payload = json.loads(text)
        assert payload["success"] is True
        assert payload["data"]["count"] == 2
    finally:
        watchdog.cancel()
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
