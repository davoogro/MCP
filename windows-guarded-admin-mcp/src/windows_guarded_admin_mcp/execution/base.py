from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from windows_guarded_admin_mcp.config import AppConfig
from windows_guarded_admin_mcp.models import ServerEntry


@dataclass
class RemoteRequest:
    operation: str
    server: ServerEntry
    parameters: dict[str, Any] = field(default_factory=dict)
    operation_id: str | None = None
    timeout_seconds: int | None = None


@dataclass
class ExecutionContext:
    config: AppConfig
    request: RemoteRequest

    @property
    def staging_path(self) -> str:
        op_id = self.request.operation_id or "default"
        root = self.config.defaults.staging_root
        return f"{root}\\{op_id}"
