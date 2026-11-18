"""MCP stdio server for Windows Server administration via PowerShell remoting (WinRM).

Launch from any cwd:  python -m windows_server_admin_mcp.server
(with PYTHONPATH=<project>/src or the package installed). Config and audit locations are
resolved from env vars or from the package location, never from the process cwd.
"""

from __future__ import annotations

import logging
import sys
import time
from functools import wraps
from typing import Any, Callable, TypeVar

try:  # mcp >= 2.0
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from mcp.types import ToolAnnotations

from windows_server_admin_mcp import __version__
from windows_server_admin_mcp.audit import AuditLogger
from windows_server_admin_mcp.config import load_config
from windows_server_admin_mcp.confirmations import ConfirmationManager
from windows_server_admin_mcp.exceptions import WsaMcpError
from windows_server_admin_mcp.models import ErrorCode, OperationResult
from windows_server_admin_mcp.operations import OperationRegistry, OperationStatus
from windows_server_admin_mcp.policies import PolicyEngine
from windows_server_admin_mcp.services.diagnostics import DiagnosticsService
from windows_server_admin_mcp.services.events import EventsService
from windows_server_admin_mcp.services.health import HealthService
from windows_server_admin_mcp.services.restart import RestartService
from windows_server_admin_mcp.services.scripts import ScriptsService
from windows_server_admin_mcp.services.services import ServicesService

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("windows-server-admin-mcp")

CONFIG = load_config()
POLICIES = PolicyEngine(CONFIG)
AUDIT = AuditLogger()
OPERATIONS = OperationRegistry()
CONFIRMATIONS = ConfirmationManager(CONFIG)

DIAGNOSTICS = DiagnosticsService(CONFIG, POLICIES)
HEALTH = HealthService(CONFIG, POLICIES)
SCRIPTS = ScriptsService(CONFIG, POLICIES)
SERVICES = ServicesService(CONFIG, POLICIES)
EVENTS = EventsService(CONFIG, POLICIES)
RESTART = RestartService(CONFIG, POLICIES, CONFIRMATIONS)

INSTRUCTIONS = """\
windows-server-admin-mcp: administration of PRODUCTION Windows servers over WinRM / PowerShell \
remoting, authenticated with the current Windows identity (Kerberos; no stored passwords).

Safety model:
- Inventory allowlist: only servers in config/servers*.yaml can be targeted. Call list_servers first \
and use the exact `server_id` it returns. Raw IPs, localhost, wildcards and UNC paths are refused.
- Per-server permissions (read_operations, restart_service, restart_server, cancel_restart, \
authorized_scripts) are enforced server-side; list_servers shows them so you can check before acting.
- Read-only tools: list_servers, diagnose_server_access, get_server_identity, get_server_health, \
get_disks, get_services, get_processes, get_windows_events, test_remote_execution, wait_for_server, \
get_operation_status, query_audit_log.
- Mutating tools (restart_service, run_authorized_script, prepare/confirm/cancel_server_restart) \
change production state. Never call them on your own initiative; get explicit user approval for the \
exact server and action first.
- Server restart is two-step: prepare_server_restart returns a one-time token (TTL-limited, bound to \
server + reason + delay); confirm_server_restart with the SAME parameters executes it. Show the user \
what will happen and wait for approval between the two steps. cancel_server_restart aborts a pending \
restart (shutdown /a).
- restart_service defaults to dry_run=true; protected services (denylist) are always refused.
- run_authorized_script only runs catalog scripts (config/scripts*.yaml) allowed for that server, \
with SHA256 verification.
- Every tool call is written to a sanitized JSONL audit log (query_audit_log); confirmation tokens \
are never logged.
"""

mcp = MCPServer(
    name="windows-server-admin-mcp",
    title="Windows Server Admin",
    instructions=INSTRUCTIONS,
    version=__version__,
)

READ_ONLY_LOCAL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
READ_ONLY_REMOTE = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
MUTATING_REMOTE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

F = TypeVar("F", bound=Callable[..., Any])


def audited_tool(tool_name: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            started = time.perf_counter()
            server_id = kwargs.get("server_id")
            safe_params = {
                k: v
                for k, v in kwargs.items()
                if k not in {"confirmation_token"}
            }
            try:
                raw = func(*args, **kwargs)
                result = raw if isinstance(raw, OperationResult) else OperationResult.ok(raw)
                duration_ms = int((time.perf_counter() - started) * 1000)
                if result.duration_ms is None:
                    result.duration_ms = duration_ms
                outcome = "success" if result.success else "failure"
                AUDIT.log_tool(
                    tool=tool_name,
                    outcome=outcome,
                    duration_ms=result.duration_ms,
                    server_id=server_id or result.server_id,
                    operation_id=result.operation_id,
                    parameters=safe_params,
                    error_code=result.error.code.value if result.error else None,
                    message=result.error.message if result.error else None,
                )
                return result.model_dump(mode="json")
            except WsaMcpError as exc:
                duration_ms = int((time.perf_counter() - started) * 1000)
                result = OperationResult.fail(
                    exc.code,
                    exc.message,
                    details=exc.details,
                    server_id=server_id,
                    duration_ms=duration_ms,
                )
                AUDIT.log_tool(
                    tool=tool_name,
                    outcome="failure",
                    duration_ms=duration_ms,
                    server_id=server_id,
                    parameters=safe_params,
                    error_code=exc.code.value,
                    message=exc.message,
                )
                return result.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001
                duration_ms = int((time.perf_counter() - started) * 1000)
                result = OperationResult.fail(
                    ErrorCode.INTERNAL_ERROR,
                    str(exc),
                    server_id=server_id,
                    duration_ms=duration_ms,
                )
                AUDIT.log_tool(
                    tool=tool_name,
                    outcome="failure",
                    duration_ms=duration_ms,
                    server_id=server_id,
                    parameters=safe_params,
                    error_code=ErrorCode.INTERNAL_ERROR.value,
                    message=str(exc),
                )
                return result.model_dump(mode="json")

        return wrapper  # type: ignore[return-value]

    return decorator


# --------------------------------------------------------------------------- read-only tools


@mcp.tool(title="List inventoried servers", annotations=READ_ONLY_LOCAL)
@audited_tool("list_servers")
def list_servers() -> dict[str, Any]:
    """List the inventoried (allowlisted) Windows servers with hostname, environment, connection
    settings and permission profile. Local config only, no remote calls. Use the returned
    `server_id` for every other tool and check `permissions` before proposing a mutating action."""
    return DIAGNOSTICS.list_servers()


@mcp.tool(title="Diagnose server access", annotations=READ_ONLY_REMOTE)
@audited_tool("diagnose_server_access")
def diagnose_server_access(server_id: str, include_smb_test: bool = True) -> dict[str, Any]:
    """Read-only connectivity diagnosis for an inventoried server: DNS, TCP ports 5985/5986/445/135,
    WinRM (Test-WSMan), PSSession + remote identity/PowerShell version, optional admin-share (SMB)
    read probe and remote Python presence. Start here when a server is new or other tools fail."""
    return DIAGNOSTICS.diagnose_server_access(server_id, include_smb_test=include_smb_test)


@mcp.tool(title="Get server identity", annotations=READ_ONLY_REMOTE)
@audited_tool("get_server_identity")
def get_server_identity(server_id: str) -> dict[str, Any]:
    """Read-only. Remote computer name, executing identity (whoami), domain and OS name/version."""
    return DIAGNOSTICS.get_server_identity(server_id)


@mcp.tool(title="Get server health", annotations=READ_ONLY_REMOTE)
@audited_tool("get_server_health")
def get_server_health(server_id: str) -> dict[str, Any]:
    """Read-only. OS, last boot time (UTC), total/free memory, CPU load and Windows Update
    pending-reboot flag of an inventoried server."""
    return HEALTH.get_server_health(server_id)


@mcp.tool(title="Get disks", annotations=READ_ONLY_REMOTE)
@audited_tool("get_disks")
def get_disks(server_id: str, drive_letters: list[str] | None = None) -> dict[str, Any]:
    """Read-only. Size, free space and used percent of fixed logical disks. Optional
    drive_letters filter, e.g. ["C:", "D:"]."""
    return HEALTH.get_disks(server_id, drive_letters)


@mcp.tool(title="Get services", annotations=READ_ONLY_REMOTE)
@audited_tool("get_services")
def get_services(
    server_id: str,
    service_names: list[str] | None = None,
    name_filter: str | None = None,
) -> dict[str, Any]:
    """Read-only. Windows service name/status/display name. Pass exact service_names, or a
    name_filter substring (matches name or display name; max 200 results), or neither for all."""
    return SERVICES.get_services(server_id, service_names, name_filter)


@mcp.tool(title="Get top processes", annotations=READ_ONLY_REMOTE)
@audited_tool("get_processes")
def get_processes(
    server_id: str,
    top: int = 10,
    name_filter: str | None = None,
) -> dict[str, Any]:
    """Read-only. Top processes by working set (1-100), with CPU seconds and memory MB.
    Optional process-name substring filter."""
    return SERVICES.get_processes(server_id, top, name_filter)


@mcp.tool(title="Get Windows events", annotations=READ_ONLY_REMOTE)
@audited_tool("get_windows_events")
def get_windows_events(
    server_id: str,
    log_name: str = "System",
    hours: int = 24,
    level: str = "Error",
    max_events: int = 50,
) -> dict[str, Any]:
    """Read-only. Recent event log entries (log_name e.g. System/Application), filtered by level
    (Critical|Error|Warning|Information), lookback hours (1-168) and max_events (1-500).
    Messages are truncated to 200 characters."""
    return EVENTS.get_windows_events(server_id, log_name, hours, level, max_events)


@mcp.tool(title="Test remote execution", annotations=READ_ONLY_REMOTE)
@audited_tool("test_remote_execution")
def test_remote_execution(server_id: str) -> dict[str, Any]:
    """Read-only. Runs a fixed no-op probe (echo + whoami + timestamp) through Invoke-Command to
    verify remote PowerShell execution works."""
    return DIAGNOSTICS.test_remote_execution(server_id)


@mcp.tool(title="Wait for server", annotations=READ_ONLY_REMOTE)
@audited_tool("wait_for_server")
def wait_for_server(
    server_id: str,
    timeout_seconds: int | None = None,
    poll_interval_seconds: int | None = None,
) -> dict[str, Any]:
    """Read-only polling until the server is reachable over WinRM + PSSession again (e.g. after a
    confirmed restart). Defaults come from config (reboot timeout / poll interval). Can block for
    many minutes."""
    return HEALTH.wait_for_server(server_id, timeout_seconds, poll_interval_seconds)


@mcp.tool(title="Get operation status", annotations=READ_ONLY_LOCAL)
@audited_tool("get_operation_status")
def get_operation_status(operation_id: str) -> dict[str, Any]:
    """Read-only. Status/result of a tracked mutating operation (in-memory, current server
    process only), by the operation_id returned from that tool."""
    try:
        record = OPERATIONS.get(operation_id)
        return OperationResult.ok(record.model_dump())
    except WsaMcpError as exc:
        return OperationResult.fail(exc.code, exc.message)


@mcp.tool(title="Query audit log", annotations=READ_ONLY_LOCAL)
@audited_tool("query_audit_log")
def query_audit_log(
    tool: str | None = None,
    server_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Read-only. Query the local sanitized JSONL audit log, newest first. Filter by tool name,
    server_id and/or `since` (ISO-8601, e.g. 2026-10-01T00:00:00Z)."""
    if server_id:
        POLICIES.get_server(server_id)
    limit = max(1, min(limit, CONFIG.policies.max_audit_query_rows))
    rows = AUDIT.query(tool=tool, server_id=server_id, since=since, limit=limit)
    return OperationResult.ok({"events": rows, "count": len(rows)})


# --------------------------------------------------------------------------- mutating tools


@mcp.tool(title="Run authorized script (MUTATING)", annotations=MUTATING_REMOTE)
@audited_tool("run_authorized_script")
def run_authorized_script(
    server_id: str,
    script_id: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MUTATING - executes a catalog-approved PowerShell script on a production server. Only
    scripts listed in config/scripts*.yaml whose allowed_servers include this server, under the
    project folder, with matching SHA256, and only if the server has authorized_scripts=true.
    String parameters are checked for shell metacharacters. Requires explicit user approval."""
    operation_id = OPERATIONS.create("run_authorized_script", server_id)
    OPERATIONS.update(operation_id, status=OperationStatus.RUNNING)
    result = SCRIPTS.run_authorized_script(
        server_id, script_id, parameters, operation_id=operation_id
    )
    result.operation_id = operation_id
    status = OperationStatus.COMPLETED if result.success else OperationStatus.FAILED
    OPERATIONS.update(operation_id, status=status, result=result)
    return result


@mcp.tool(title="Restart Windows service (MUTATING)", annotations=MUTATING_REMOTE)
@audited_tool("restart_service")
def restart_service(
    server_id: str,
    service_name: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """MUTATING - restarts a Windows service on a production server. dry_run defaults to true
    (policy validation only, nothing is restarted and the server is not contacted). A real restart
    (dry_run=false) requires restart_service=true in the server's permissions; protected services
    (denylist, MSExchange*) are always refused. Requires explicit user approval for dry_run=false."""
    operation_id = OPERATIONS.create("restart_service", server_id)
    result = SERVICES.restart_service(
        server_id, service_name, dry_run=dry_run, operation_id=operation_id
    )
    result.operation_id = operation_id
    OPERATIONS.update(
        operation_id,
        status=OperationStatus.COMPLETED if result.success else OperationStatus.FAILED,
        result=result,
    )
    return result


@mcp.tool(title="Prepare server restart (step 1 of 2)", annotations=MUTATING_REMOTE)
@audited_tool("prepare_server_restart")
def prepare_server_restart(
    server_id: str,
    reason_code: str,
    reason_detail: str,
    delay_seconds: int = 300,
) -> dict[str, Any]:
    """Step 1 of the two-step server restart. Validates policy (restart_server permission, known
    reason_code from policies, reason_detail 10-500 chars, local host blocked) and returns a
    one-time confirmation_token bound to server_id + reason + delay (30-3600 s). Does not restart
    anything. Present the plan to the user and only call confirm_server_restart after they approve."""
    operation_id = OPERATIONS.create("prepare_server_restart", server_id)
    result = RESTART.prepare_server_restart(
        server_id, reason_code, reason_detail, delay_seconds, operation_id=operation_id
    )
    result.operation_id = operation_id
    OPERATIONS.update(operation_id, status=OperationStatus.PENDING, result=result)
    return result


@mcp.tool(title="Confirm server restart (step 2 of 2, DESTRUCTIVE)", annotations=MUTATING_REMOTE)
@audited_tool("confirm_server_restart")
def confirm_server_restart(
    server_id: str,
    confirmation_token: str,
    reason_code: str,
    reason_detail: str,
    delay_seconds: int = 300,
) -> dict[str, Any]:
    """DESTRUCTIVE - reboots a production server (shutdown.exe /r /m \\\\host /t delay). Consumes the
    one-time token from prepare_server_restart; all parameters must match exactly. Only call after
    the user explicitly approved this restart. Use cancel_server_restart within the delay to abort."""
    operation_id = OPERATIONS.create("confirm_server_restart", server_id)
    OPERATIONS.update(operation_id, status=OperationStatus.RUNNING)
    result = RESTART.confirm_server_restart(
        server_id,
        confirmation_token,
        reason_code,
        reason_detail,
        delay_seconds,
        operation_id=operation_id,
    )
    result.operation_id = operation_id
    OPERATIONS.update(
        operation_id,
        status=OperationStatus.COMPLETED if result.success else OperationStatus.FAILED,
        result=result,
    )
    return result


@mcp.tool(title="Cancel server restart (MUTATING)", annotations=MUTATING_REMOTE)
@audited_tool("cancel_server_restart")
def cancel_server_restart(
    server_id: str,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """MUTATING - aborts a pending shutdown/restart on the server (shutdown.exe /a) and
    invalidates the confirmation token if given. Requires cancel_restart permission.
    `cancelled` is true only when shutdown.exe actually aborted something."""
    operation_id = OPERATIONS.create("cancel_server_restart", server_id)
    result = RESTART.cancel_server_restart(
        server_id, confirmation_token, operation_id=operation_id
    )
    result.operation_id = operation_id
    OPERATIONS.update(
        operation_id,
        status=OperationStatus.COMPLETED if result.success else OperationStatus.FAILED,
        result=result,
    )
    return result


def main() -> None:
    logger.info("Starting windows-server-admin-mcp %s (stdio)", __version__)
    logger.info("Config directory: %s", CONFIG.config_dir)
    logger.info("Audit directory: %s", AUDIT.log_dir)
    logger.info("Inventory servers: %s", ", ".join(CONFIG.servers.keys()) or "(none)")
    AUDIT.log_tool(tool="server", outcome="success", event_type="server.start")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
