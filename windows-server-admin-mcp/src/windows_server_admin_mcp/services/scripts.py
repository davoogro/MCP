from __future__ import annotations

from typing import Any

from windows_server_admin_mcp.catalog import ScriptCatalog
from windows_server_admin_mcp.models import OperationResult
from windows_server_admin_mcp.policies import validate_script_id, validate_string_param
from windows_server_admin_mcp.services import ServiceBase


class ScriptsService(ServiceBase):
    def __init__(self, config, policies) -> None:  # noqa: ANN001
        super().__init__(config, policies)
        self.catalog = ScriptCatalog(config)

    def run_authorized_script(
        self,
        server_id: str,
        script_id: str,
        parameters: dict[str, Any] | None = None,
        *,
        operation_id: str | None = None,
    ) -> OperationResult:
        validate_script_id(script_id)
        server = self.policies.get_server(server_id)
        self.policies.check_authorized_scripts(server)
        entry, path, digest = self.catalog.assert_allowed(script_id, server_id)

        params = parameters or {}
        for key, value in params.items():
            if isinstance(value, str):
                validate_string_param(value, field_name=key)

        return self._remote(
            server,
            "run_authorized_script",
            {
                "scriptId": script_id,
                "scriptPath": str(path),
                "sha256": digest,
                "timeoutSeconds": entry.timeout_seconds,
                "parameters": params,
            },
            operation_id=operation_id,
            timeout_seconds=entry.timeout_seconds + 30,
        )
