from __future__ import annotations

from pathlib import Path

from windows_server_admin_mcp.config import AppConfig
from windows_server_admin_mcp.execution.base import RemoteRequest
from windows_server_admin_mcp.execution.powershell_runner import (
    PowerShellResult,
    run_powershell_json_request,
)


class PsSessionTransfer:
    """Stage scripts to remote servers via PSSession Copy-Item."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.script = config.powershell_dir / "Invoke-McpRemoteOperation.ps1"

    def stage_file(
        self,
        request: RemoteRequest,
        local_path: Path,
        remote_name: str,
    ) -> PowerShellResult:
        conn = request.server.connection
        payload = {
            "operation": "stage_file",
            "operationId": request.operation_id,
            "server": {
                "hostname": request.server.hostname,
                "port": conn.port,
                "useSsl": conn.use_ssl,
                "authentication": conn.authentication,
                "usePssession": conn.use_pssession,
                "allowSmbFallback": conn.allow_smb_fallback,
            },
            "parameters": {
                "localPath": str(local_path.resolve()),
                "remoteName": remote_name,
            },
            "stagingPath": request.operation_id,
        }
        return run_powershell_json_request(
            self.script,
            payload,
            timeout_seconds=self.config.defaults.operation_timeout_seconds,
            max_output_chars=self.config.defaults.max_output_chars,
        )
