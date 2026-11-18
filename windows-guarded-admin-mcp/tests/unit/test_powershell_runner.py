"""PowerShell runner: subprocess safety, timeout, truncation, encoding."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from windows_guarded_admin_mcp.execution import powershell_runner as psr


def test_truncate_under_limit():
    text, truncated = psr._truncate("hello", 100)
    assert text == "hello"
    assert truncated is False


def test_truncate_over_limit():
    text, truncated = psr._truncate("x" * 200, 50)
    assert truncated is True
    assert len(text) > 50
    assert "truncated" in text


def test_run_powershell_file_missing_script(tmp_path):
    result = psr.run_powershell_file(tmp_path / "missing.ps1")
    assert result.success is False
    assert "not found" in result.stderr.lower()


def test_run_powershell_never_uses_shell_true(tmp_path):
    """Verify Popen is called with shell=False (default) or shell omitted."""
    script = tmp_path / "noop.ps1"
    script.write_text('Write-Output "{\\"success\\": true}"', encoding="utf-8")

    with patch.object(psr.subprocess, "Popen", wraps=psr.subprocess.Popen) as mock_popen:
        psr.run_powershell_file(script, timeout_seconds=30)
        assert mock_popen.called
        _, kwargs = mock_popen.call_args
        assert kwargs.get("shell") is not True


def test_run_powershell_timeout_truncates_output(tmp_path):
    script = tmp_path / "slow.ps1"
    script.write_text("Start-Sleep -Seconds 60", encoding="utf-8")

    with patch.object(psr, "_kill_process_tree"):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="pwsh", timeout=1),
            ("stdout data " * 10000, "stderr data " * 10000),
        ]

        with patch.object(psr.subprocess, "Popen", return_value=mock_proc):
            result = psr.run_powershell_file(
                script,
                timeout_seconds=1,
                max_output_chars=100,
            )

    assert result.success is False
    assert result.exit_code == -1
    assert result.stdout_truncated or result.stderr_truncated


def test_run_powershell_parses_json_stdout(tmp_path):
    script = tmp_path / "json.ps1"
    script.write_text(
        '@{ success = $true; data = @{ value = 42 } } | ConvertTo-Json -Compress',
        encoding="utf-8",
    )
    if sys.platform != "win32":
        pytest.skip("PowerShell execution requires Windows")
    result = psr.run_powershell_file(script, timeout_seconds=30)
    if result.exit_code != 0 and "not found" in result.stderr.lower():
        pytest.skip("powershell.exe not available")
    assert result.data is not None or result.stdout


def test_run_powershell_uses_utf8_encoding(tmp_path):
    script = tmp_path / "enc.ps1"
    script.write_text('Write-Output "ok"', encoding="utf-8")

    with patch.object(psr.subprocess, "Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("ok", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        psr.run_powershell_file(script)
        _, kwargs = mock_popen.call_args
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"


def test_run_powershell_json_request_passes_payload(tmp_path, project_root):
    script = tmp_path / "req.ps1"
    script.write_text("param([string]$RequestJson)", encoding="utf-8")

    with patch.object(psr, "run_powershell_file") as mock_run:
        mock_run.return_value = psr.PowerShellResult(
            success=True,
            exit_code=0,
            stdout="{}",
            stderr="",
            duration_ms=1,
        )
        payload = {"operation": "test"}
        psr.run_powershell_json_request(script, payload)
        args = mock_run.call_args
        assert "-RequestJson" in args[0][1]
