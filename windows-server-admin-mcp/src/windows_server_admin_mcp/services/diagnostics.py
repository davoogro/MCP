from __future__ import annotations

from typing import Any

from windows_server_admin_mcp.models import OperationResult, ServerEntry
from windows_server_admin_mcp.services import ServiceBase


class DiagnosticsService(ServiceBase):
    def list_servers(self) -> OperationResult:
        servers = []
        for sid, entry in sorted(self.config.servers.items()):
            servers.append(
                {
                    "server_id": sid,
                    "hostname": entry.hostname,
                    "environment": entry.environment,
                    "enabled": entry.enabled,
                    "description": entry.description,
                    "connection": entry.connection.model_dump(),
                    "limits": entry.limits.model_dump(),
                    "tags": entry.tags,
                    "permissions": entry.permissions.model_dump(),
                }
            )
        return OperationResult.ok({"servers": servers, "count": len(servers)})

    def diagnose_server_access(
        self, server_id: str, include_smb_test: bool = True
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        return self._remote(
            server,
            "diagnose_access",
            {"includeSmbTest": include_smb_test},
        )

    def get_server_identity(self, server_id: str) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        return self._remote(server, "get_identity")

    def test_remote_execution(self, server_id: str) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        return self._remote(server, "test_execution", {"probe": "echo MCP_OK"})

    def run_diagnostic(
        self,
        server_id: str,
        diagnostic: str,
        parameters: dict[str, Any] | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        self.policies.validate_diagnostic(diagnostic)
        if diagnostic == "tcp_port_check" and parameters and "port" in parameters:
            self.policies.validate_tcp_port(int(parameters["port"]))
        return self._remote(
            server,
            "diagnostic",
            {"diagnostic": diagnostic, "parameters": parameters or {}},
        )
