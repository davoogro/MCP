from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POWERSHELL_EXE = os.environ.get("WSA_MCP_POWERSHELL", "powershell.exe")


@dataclass
class PowerShellResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    data: Any | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated, {len(text) - limit} chars omitted]", True


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()


def run_powershell_file(
    script_path: Path,
    args: list[str] | None = None,
    *,
    timeout_seconds: int = 120,
    max_output_chars: int = 100_000,
    cwd: Path | None = None,
    powershell_executable: str | None = None,
) -> PowerShellResult:
    """Run a PowerShell script via explicit argument list — never shell=True."""
    if not script_path.exists():
        return PowerShellResult(
            success=False,
            exit_code=1,
            stdout="",
            stderr=f"Script not found: {script_path}",
            duration_ms=0,
        )

    cmd = [
        powershell_executable or POWERSHELL_EXE,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path.resolve()),
    ]
    if args:
        cmd.extend(args)

    started = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        exit_code = proc.returncode or 0
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        stdout, stderr = proc.communicate(timeout=5)
        duration_ms = int((time.perf_counter() - started) * 1000)
        out, out_trunc = _truncate(stdout or "", max_output_chars)
        err, err_trunc = _truncate(
            (stderr or "") + "\nOperation timed out",
            max_output_chars,
        )
        return PowerShellResult(
            success=False,
            exit_code=-1,
            stdout=out,
            stderr=err,
            duration_ms=duration_ms,
            stdout_truncated=out_trunc,
            stderr_truncated=err_trunc,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    out, out_trunc = _truncate(stdout or "", max_output_chars)
    err, err_trunc = _truncate(stderr or "", max_output_chars)

    data = None
    success = exit_code == 0
    stripped = (stdout or "").strip()
    if stripped:
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "success" in data:
                success = bool(data.get("success"))
        except json.JSONDecodeError:
            pass

    return PowerShellResult(
        success=success,
        exit_code=exit_code,
        stdout=out,
        stderr=err,
        duration_ms=duration_ms,
        data=data,
        stdout_truncated=out_trunc,
        stderr_truncated=err_trunc,
    )


def run_powershell_json_request(
    script_path: Path,
    request: dict[str, Any],
    *,
    timeout_seconds: int = 120,
    max_output_chars: int = 100_000,
    powershell_executable: str | None = None,
) -> PowerShellResult:
    payload = json.dumps(request, default=str)
    return run_powershell_file(
        script_path,
        ["-RequestJson", payload],
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        powershell_executable=powershell_executable,
    )
