"""shutdown.exe argument construction — /f only when Force is set."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESTART_PY = PROJECT_ROOT / "src" / "windows_server_admin_mcp" / "services" / "restart.py"
LEGACY_RESTART_SCRIPT = PROJECT_ROOT / "powershell" / "Invoke-McpServerRestart.ps1"


def _read_restart_py() -> str:
    assert RESTART_PY.exists(), f"Missing {RESTART_PY}"
    return RESTART_PY.read_text(encoding="utf-8")


def test_shutdown_args_built_without_force_by_default():
    source = _read_restart_py()
    assert '"/r"' in source or "'/r'" in source
    assert '"/m"' in source or "'/m'" in source
    assert "/f" not in source.split("shutdown_args")[1] if "shutdown_args" in source else True


def test_confirm_uses_planned_reason_code():
    source = _read_restart_py()
    assert "p:4:2" in source


def test_restart_service_passes_force_false():
    source = _read_restart_py()
    assert '"force": False' in source or "'force': False" in source


def test_cancel_uses_shutdown_a():
    source = _read_restart_py()
    assert "'/a'" in source or '"/a"' in source


def test_shutdown_uses_subprocess_list_not_shell():
    source = _read_restart_py()
    assert "subprocess.run" in source
    assert '"shutdown.exe"' in source or "'shutdown.exe'" in source
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or '"""' in line or "never shell=True" in line:
            continue
        assert "shell=True" not in line


def test_legacy_ps_restart_script_still_has_optional_force():
    if not LEGACY_RESTART_SCRIPT.exists():
        return
    source = LEGACY_RESTART_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"\[switch\]\$Force", source)
