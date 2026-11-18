#!/usr/bin/env python3
"""Local smoke test for windows-server-admin-mcp package imports and config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from windows_server_admin_mcp.config import load_config
    from windows_server_admin_mcp.execution.powershell_runner import run_powershell_file
    from windows_server_admin_mcp.models import OperationResult

    config = load_config()
    script = ROOT / "authorized_scripts" / "smoke_test.ps1"
    result = run_powershell_file(script, timeout_seconds=60)

    payload = OperationResult.ok(
        {
            "config_dir": str(config.config_dir),
            "server_count": len(config.servers),
            "powershell_success": result.success,
            "powershell_data": result.data,
        }
    )
    print(json.dumps(payload.model_dump(), indent=2, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
