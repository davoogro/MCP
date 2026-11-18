from __future__ import annotations

from windows_server_admin_mcp.models import OperationResult
from windows_server_admin_mcp.policies import validate_service_name
from windows_server_admin_mcp.services import ServiceBase


class ServicesService(ServiceBase):
    def get_services(
        self,
        server_id: str,
        service_names: list[str] | None = None,
        name_filter: str | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        params: dict = {}
        if service_names:
            for name in service_names:
                validate_service_name(name)
            params["serviceNames"] = service_names
        if name_filter:
            self.policies.validate_string_param(name_filter, field_name="name_filter")
            params["nameFilter"] = name_filter
        return self._remote(server, "get_services", params)

    def get_processes(
        self,
        server_id: str,
        top: int = 10,
        name_filter: str | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        params: dict = {"top": max(1, min(top, 100))}
        if name_filter:
            self.policies.validate_string_param(name_filter, field_name="name_filter")
            params["nameFilter"] = name_filter
        return self._remote(server, "get_processes", params)

    def restart_service(
        self,
        server_id: str,
        service_name: str,
        *,
        dry_run: bool = False,
        operation_id: str | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_service_restart(server, service_name)
        return self._remote(
            server,
            "restart_service",
            {"serviceName": service_name, "dryRun": dry_run},
            operation_id=operation_id,
        )
