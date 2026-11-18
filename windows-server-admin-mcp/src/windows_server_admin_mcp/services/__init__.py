from __future__ import annotations

from typing import Any

from windows_server_admin_mcp.config import AppConfig
from windows_server_admin_mcp.exceptions import WsaMcpError
from windows_server_admin_mcp.execution.base import RemoteRequest
from windows_server_admin_mcp.execution.winrm_transport import WinRmTransport
from windows_server_admin_mcp.models import ErrorCode, OperationResult, ServerEntry
from windows_server_admin_mcp.policies import PolicyEngine


class ServiceBase:
    def __init__(self, config: AppConfig, policies: PolicyEngine) -> None:
        self.config = config
        self.policies = policies
        self.transport = WinRmTransport(config)

    def _remote(
        self,
        server: ServerEntry,
        operation: str,
        parameters: dict[str, Any] | None = None,
        *,
        operation_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> OperationResult:
        if not server.enabled:
            return OperationResult.fail(
                ErrorCode.ACTION_DISABLED,
                f"Server {server.server_id} is disabled in inventory",
                operation_id=operation_id,
                operation=operation,
                server_id=server.server_id,
            )
        request = RemoteRequest(
            operation=operation,
            server=server,
            parameters=parameters or {},
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )
        try:
            ps_result = self.transport.invoke(request)
            payload = self.transport.parse_data(ps_result)
            if payload.get("success") is False:
                code_raw = payload.get("errorCode") or ErrorCode.TRANSPORT_ERROR
                try:
                    code = ErrorCode(code_raw)
                except ValueError:
                    code = ErrorCode.TRANSPORT_ERROR
                remote_data = payload.get("data")
                return OperationResult.fail(
                    code,
                    str(payload.get("sanitizedError") or payload.get("message") or "Remote failed"),
                    details={"data": remote_data} if remote_data is not None else None,
                    operation_id=operation_id,
                    operation=operation,
                    server_id=server.server_id,
                    duration_ms=payload.get("durationMs") or ps_result.duration_ms,
                    transport=payload.get("transportUsed") or server.connection.transport,
                    exit_code=ps_result.exit_code,
                    warnings=list(payload.get("warnings") or []),
                    stdout_truncated=ps_result.stdout_truncated,
                    stderr_truncated=ps_result.stderr_truncated,
                )
            return OperationResult.ok(
                payload.get("data"),
                operation_id=operation_id,
                operation=operation,
                server_id=server.server_id,
                duration_ms=payload.get("durationMs") or ps_result.duration_ms,
                transport=payload.get("transportUsed") or server.connection.transport,
                exit_code=ps_result.exit_code,
                warnings=list(payload.get("warnings") or []),
                stdout_truncated=ps_result.stdout_truncated,
                stderr_truncated=ps_result.stderr_truncated,
            )
        except WsaMcpError as exc:
            return OperationResult.fail(
                exc.code,
                exc.message,
                details=exc.details,
                operation_id=operation_id,
                operation=operation,
                server_id=server.server_id,
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult.fail(
                ErrorCode.INTERNAL_ERROR,
                str(exc),
                operation_id=operation_id,
                operation=operation,
                server_id=server.server_id,
            )
