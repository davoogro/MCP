from __future__ import annotations

from pathlib import Path

from windows_guarded_admin_mcp.config import AppConfig
from windows_guarded_admin_mcp.exceptions import TransportError
from windows_guarded_admin_mcp.execution.base import RemoteRequest
from windows_guarded_admin_mcp.execution.powershell_runner import (
    PowerShellResult,
    run_powershell_json_request,
)


class SmbFallback:
    """Copy files via administrative C$ share when PSSession staging is unavailable."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.script = config.powershell_dir / "Invoke-McpRemoteOperation.ps1"

    def copy_to_staging(
        self,
        request: RemoteRequest,
        local_path: Path,
        remote_relative: str,
    ) -> PowerShellResult:
        if not self.config.defaults.allow_smb_fallback and not request.server.connection.allow_smb_fallback:
            raise TransportError("SMB fallback is disabled by policy")
        payload = {
            "operation": "smb_copy",
            "operationId": request.operation_id,
            "server": {
                "hostname": request.server.hostname,
            },
            "parameters": {
                "localPath": str(local_path.resolve()),
                "remoteRelative": remote_relative,
            },
            "stagingPath": request.operation_id,
        }
        return run_powershell_json_request(
            self.script,
            payload,
            timeout_seconds=self.config.defaults.operation_timeout_seconds,
            max_output_chars=self.config.defaults.max_output_chars,
        )
