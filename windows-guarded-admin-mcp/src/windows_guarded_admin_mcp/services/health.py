from __future__ import annotations

from windows_guarded_admin_mcp.models import OperationResult
from windows_guarded_admin_mcp.services import ServiceBase


class HealthService(ServiceBase):
    def get_server_health(self, server_id: str) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        return self._remote(server, "get_health")

    def get_disks(self, server_id: str, drive_letters: list[str] | None = None) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        params: dict = {}
        if drive_letters:
            for letter in drive_letters:
                self.policies.validate_string_param(letter, field_name="drive_letter")
            params["driveLetters"] = drive_letters
        return self._remote(server, "get_disks", params)

    def wait_for_server(
        self,
        server_id: str,
        timeout_seconds: int | None = None,
        poll_interval_seconds: int | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_read_operations(server)
        timeout = timeout_seconds or self.config.defaults.reboot_timeout_seconds
        poll = poll_interval_seconds or self.config.defaults.poll_interval_seconds
        return self._remote(
            server,
            "wait_for_online",
            {"timeoutSeconds": timeout, "pollIntervalSeconds": poll},
            timeout_seconds=timeout + 30,
        )
