from __future__ import annotations

from windows_server_admin_mcp.models import OperationResult
from windows_server_admin_mcp.policies import validate_reason, validate_string_param
from windows_server_admin_mcp.services import ServiceBase


class EventsService(ServiceBase):
    def get_windows_events(
        self,
        server_id: str,
        log_name: str = "System",
        hours: int = 24,
        level: str = "Error",
        max_events: int = 50,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        validate_string_param(log_name, field_name="log_name")
        approved_levels = {"Critical", "Error", "Warning", "Information"}
        if level not in approved_levels:
            level = "Error"
        hours = max(1, min(hours, 168))
        max_events = max(1, min(max_events, 500))
        return self._remote(
            server,
            "get_events",
            {
                "logName": log_name,
                "hours": hours,
                "level": level,
                "maxEvents": max_events,
            },
        )
