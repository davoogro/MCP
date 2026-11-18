from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from windows_guarded_admin_mcp.config import AppConfig
from windows_guarded_admin_mcp.exceptions import TransportError
from windows_guarded_admin_mcp.execution.base import ExecutionContext, RemoteRequest
from windows_guarded_admin_mcp.execution.powershell_runner import (
    PowerShellResult,
    run_powershell_json_request,
)


def build_remote_request(config: AppConfig, request: RemoteRequest) -> dict[str, Any]:
    conn = request.server.connection
    limits = request.server.limits
    return {
        "operation": request.operation,
        "operationId": request.operation_id,
        "server": {
            "serverId": request.server.server_id,
            "hostname": request.server.hostname,
            "transport": conn.transport,
            "port": conn.port,
            "useSsl": conn.use_ssl,
            "authentication": conn.authentication,
            "powershellExecutable": conn.powershell_executable,
            "usePssession": conn.use_pssession,
            "allowSmbFallback": conn.allow_smb_fallback,
        },
        "defaults": config.defaults.model_dump(),
        "limits": limits.model_dump(),
        "parameters": request.parameters,
        "stagingPath": ExecutionContext(config, request).staging_path,
        "preferPssession": conn.use_pssession,
        "allowSmbFallback": conn.allow_smb_fallback,
    }


class WinRmTransport:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dispatcher = config.powershell_dir / "Invoke-McpRemoteOperation.ps1"

    def invoke(self, request: RemoteRequest) -> PowerShellResult:
        if not self.dispatcher.exists():
            raise TransportError(f"Remote dispatcher missing: {self.dispatcher}")
        payload = build_remote_request(self.config, request)
        timeout = (
            request.timeout_seconds
            or request.server.limits.command_timeout_seconds
            or self.config.defaults.operation_timeout_seconds
        )
        ps_exe = request.server.connection.powershell_executable
        result = run_powershell_json_request(
            self.dispatcher,
            payload,
            timeout_seconds=timeout,
            max_output_chars=self.config.defaults.max_output_chars,
            powershell_executable=ps_exe,
        )
        if not result.success and result.data is None:
            raise TransportError(
                result.stderr or "Remote operation failed",
                details={"exit_code": result.exit_code, "stdout": result.stdout[:500]},
            )
        return result

    def parse_data(self, result: PowerShellResult) -> dict[str, Any]:
        if isinstance(result.data, dict):
            return result.data
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout.strip())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        raise TransportError("Invalid JSON response from remote dispatcher")
