from __future__ import annotations

import subprocess
import time

from windows_guarded_admin_mcp.confirmations import ConfirmationManager
from windows_guarded_admin_mcp.models import ErrorCode, OperationResult
from windows_guarded_admin_mcp.policies import validate_reason
from windows_guarded_admin_mcp.services import ServiceBase


def _run_shutdown(args: list[str], *, timeout_seconds: int = 60) -> tuple[int, str, str]:
    """Invoke shutdown.exe via explicit argument list — never shell=True."""
    proc = subprocess.run(
        ["shutdown.exe", *args],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class RestartService(ServiceBase):
    def __init__(
        self,
        config,
        policies,
        confirmations: ConfirmationManager,
    ) -> None:  # noqa: ANN001
        super().__init__(config, policies)
        self.confirmations = confirmations

    def prepare_server_restart(
        self,
        server_id: str,
        reason_code: str,
        reason_detail: str,
        delay_seconds: int = 300,
        *,
        operation_id: str | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_server_restart(server)
        description = self.policies.validate_reason_code(reason_code)
        validate_reason(reason_detail)
        delay_seconds = max(30, min(delay_seconds, 3600))
        params = {
            "reasonCode": reason_code,
            "reasonDescription": description,
            "reasonDetail": reason_detail,
            "delaySeconds": delay_seconds,
        }
        token, record = self.confirmations.issue(
            server_id=server_id,
            action="server_restart",
            params=params,
        )
        return OperationResult.ok(
            {
                "confirmation_required": True,
                "confirmation_token_prefix": record.token_prefix,
                "expires_at": record.expires_at.isoformat().replace("+00:00", "Z"),
                "server_id": server_id,
                "reason_code": reason_code,
                "reason_description": description,
                "delay_seconds": delay_seconds,
                "confirmation_token": token,
                "message": (
                    "Call confirm_server_restart with the confirmation_token within the TTL. "
                    "Token is shown once and will not appear in audit logs."
                ),
            },
            operation_id=operation_id,
            operation="prepare_server_restart",
            server_id=server_id,
        )

    def confirm_server_restart(
        self,
        server_id: str,
        confirmation_token: str,
        reason_code: str,
        reason_detail: str,
        delay_seconds: int = 300,
        *,
        operation_id: str | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_server_restart(server)
        description = self.policies.validate_reason_code(reason_code)
        validate_reason(reason_detail)
        delay_seconds = max(30, min(delay_seconds, 3600))
        params = {
            "reasonCode": reason_code,
            "reasonDescription": description,
            "reasonDetail": reason_detail,
            "delaySeconds": delay_seconds,
        }
        self.confirmations.consume(
            confirmation_token,
            server_id=server_id,
            action="server_restart",
            params=params,
        )
        full_reason = f"[{reason_code}] {description} — {reason_detail}"
        started = time.perf_counter()
        shutdown_args = [
            "/r",
            "/m",
            f"\\\\{server.hostname}",
            "/t",
            str(delay_seconds),
            "/c",
            full_reason,
            "/d",
            "p:4:2",
        ]
        exit_code, stdout, stderr = _run_shutdown(shutdown_args)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if exit_code != 0:
            return OperationResult.fail(
                ErrorCode.TRANSPORT_ERROR,
                f"shutdown.exe failed with exit code {exit_code}",
                details={"stdout": stdout[:500], "stderr": stderr[:500]},
                operation_id=operation_id,
                operation="confirm_server_restart",
                server_id=server_id,
                duration_ms=duration_ms,
                exit_code=exit_code,
            )
        return OperationResult.ok(
            {
                "initiated": True,
                "reboot_command_sent": True,
                "hostname": server.hostname,
                "delay_seconds": delay_seconds,
                "force": False,
                # shutdown.exe implies /f whenever /t > 0: running apps are closed without prompting.
                "apps_force_closed_by_timeout": delay_seconds > 0,
                "method": "shutdown.exe",
                "reason_code": reason_code,
            },
            operation_id=operation_id,
            operation="confirm_server_restart",
            server_id=server_id,
            duration_ms=duration_ms,
            exit_code=exit_code,
            transport="local_shutdown",
        )

    def cancel_server_restart(
        self,
        server_id: str,
        confirmation_token: str | None = None,
        *,
        operation_id: str | None = None,
    ) -> OperationResult:
        server = self.policies.get_server(server_id)
        self.policies.check_cancel_restart(server)
        if confirmation_token:
            self.confirmations.cancel(confirmation_token)
        started = time.perf_counter()
        exit_code, stdout, stderr = _run_shutdown(["/a", "/m", f"\\\\{server.hostname}"])
        duration_ms = int((time.perf_counter() - started) * 1000)
        # Exit code 1116 = no shutdown was in progress; anything non-zero means nothing was aborted.
        return OperationResult.ok(
            {
                "cancelled": exit_code == 0,
                "no_shutdown_in_progress": exit_code == 1116,
                "token_invalidated": bool(confirmation_token),
                "hostname": server.hostname,
                "exit_code": exit_code,
                "stdout": stdout[:500] if stdout else None,
                "stderr": stderr[:500] if stderr else None,
            },
            operation_id=operation_id,
            operation="cancel_server_restart",
            server_id=server_id,
            duration_ms=duration_ms,
            exit_code=exit_code,
            transport="local_shutdown",
        )
