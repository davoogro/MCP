"""Tests de ops con subprocess mockeado (sin WSL real)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from wsl_mcp import ops


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO", "testdistro")
    monkeypatch.setenv("WSL_COMMAND_TIMEOUT", "120")
    monkeypatch.setenv("WSL_MAX_OUTPUT", "30000")


def test_run_command_empty():
    out = ops.op_run_command("")
    assert out["ok"] is False


@patch("wsl_mcp.ops.subprocess.run")
def test_run_command_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=b"out\n", stderr=b"")
    out = ops.op_run_command("echo hi", stdin="data", timeout=60)
    assert out["ok"] is True
    assert out["success"] is True
    assert out["stdout"] == "out\n"
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["input"] == b"data"
    argv = mock_run.call_args.args[0]
    assert argv[0] == "wsl.exe"
    assert "-d" in argv and "testdistro" in argv


@patch("wsl_mcp.ops.subprocess.run")
def test_run_command_distro_override(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
    ops.op_run_command("true", distro="other")
    argv = mock_run.call_args.args[0]
    assert "other" in argv


@patch("wsl_mcp.ops.subprocess.run")
def test_docker_ps_missing(mock_run):
    mock_run.return_value = MagicMock(
        returncode=127,
        stdout=b"",
        stderr="docker no instalado".encode(),
    )
    out = ops.op_docker_ps()
    assert out["ok"] is False
    assert "docker" in (out.get("error") or out.get("stderr", "")).lower()


@patch("wsl_mcp.ops.subprocess.run")
def test_read_file_bytes_mode(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=b"abc\n__WSL_MCP_TOTAL_LINES__=10\n",
        stderr=b"",
    )
    out = ops.op_read_file("/tmp/x", offset_bytes=0, max_bytes=100)
    assert out["ok"] is True
    assert out["mode"] == "bytes"
    assert "abc" in out["content"]


@patch("wsl_mcp.ops.subprocess.run")
def test_copy_between(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
    out = ops.op_copy_between("/a", r"C:\b")
    assert out["ok"] is True
    cmd = mock_run.call_args.args[0]
    assert "cp -a" in cmd[-1]
