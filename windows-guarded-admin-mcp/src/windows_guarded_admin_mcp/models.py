from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _utc_now_z() -> str:
    """Naive-UTC ISO timestamp with trailing Z (same format as the former utcnow() call)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


class ErrorCode(str, Enum):
    ALLOWLIST_REJECTED = "ALLOWLIST_REJECTED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    SERVER_NOT_FOUND = "SERVER_NOT_FOUND"
    SCRIPT_NOT_AUTHORIZED = "SCRIPT_NOT_AUTHORIZED"
    SCRIPT_HASH_MISMATCH = "SCRIPT_HASH_MISMATCH"
    HASH_INVALID = "HASH_INVALID"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    CONFIRMATION_USED = "CONFIRMATION_USED"
    CONNECTION_TIMEOUT = "CONNECTION_TIMEOUT"
    HOST_UNREACHABLE = "HOST_UNREACHABLE"
    DNS_FAILED = "DNS_FAILED"
    WINRM_UNAVAILABLE = "WINRM_UNAVAILABLE"
    PSSESSION_FAILED = "PSSESSION_FAILED"
    SMB_UNAVAILABLE = "SMB_UNAVAILABLE"
    REMOTE_PYTHON_UNAVAILABLE = "REMOTE_PYTHON_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    TIMEOUT = "TIMEOUT"
    LOCALHOST_BLOCKED = "LOCALHOST_BLOCKED"
    REBOOT_NOT_ALLOWED = "REBOOT_NOT_ALLOWED"
    SERVICE_NOT_AUTHORIZED = "SERVICE_NOT_AUTHORIZED"
    ACTION_DISABLED = "ACTION_DISABLED"
    INVALID_JSON = "INVALID_JSON"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    RESTART_SCHEDULED = "RESTART_SCHEDULED"
    SERVER_NOT_RECOVERED = "SERVER_NOT_RECOVERED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class OperationError(BaseModel):
    code: ErrorCode
    message: str
    details: Optional[dict[str, Any]] = None


class OperationResult(BaseModel):
    """Uniform JSON envelope returned by every MCP tool."""

    success: bool
    operation_id: Optional[str] = None
    operation: Optional[str] = None
    server: Optional[str] = None
    transport: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    data: Optional[Any] = None
    warnings: list[str] = Field(default_factory=list)
    error: Optional[OperationError] = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Legacy alias retained for audit/operation tracking callers
    server_id: Optional[str] = None
    timestamp: Optional[str] = None

    @classmethod
    def ok(
        cls,
        data: Any = None,
        *,
        operation_id: str | None = None,
        operation: str | None = None,
        server: str | None = None,
        server_id: str | None = None,
        duration_ms: int | None = None,
        transport: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        exit_code: int | None = None,
        warnings: list[str] | None = None,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> OperationResult:
        resolved_server = server or server_id
        completed = completed_at or _utc_now_z()
        return cls(
            success=True,
            data=data,
            operation_id=operation_id,
            operation=operation,
            server=resolved_server,
            server_id=resolved_server,
            duration_ms=duration_ms,
            transport=transport,
            started_at=started_at,
            completed_at=completed,
            timestamp=completed,
            exit_code=exit_code,
            warnings=warnings or [],
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    @classmethod
    def fail(
        cls,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        operation_id: str | None = None,
        operation: str | None = None,
        server: str | None = None,
        server_id: str | None = None,
        duration_ms: int | None = None,
        transport: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        exit_code: int | None = None,
        warnings: list[str] | None = None,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> OperationResult:
        resolved_server = server or server_id
        completed = completed_at or _utc_now_z()
        return cls(
            success=False,
            error=OperationError(code=code, message=message, details=details),
            operation_id=operation_id,
            operation=operation,
            server=resolved_server,
            server_id=resolved_server,
            duration_ms=duration_ms,
            transport=transport,
            started_at=started_at,
            completed_at=completed,
            timestamp=completed,
            exit_code=exit_code,
            warnings=warnings or [],
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


class ServerPermissions(BaseModel):
    restart_service: bool = False
    restart_server: bool = False
    cancel_restart: bool = False
    read_operations: bool = True
    authorized_scripts: bool = False


class ServerConnection(BaseModel):
    transport: str = "winrm"
    authentication: str = "current_windows_identity"
    powershell_executable: str = "powershell.exe"
    use_pssession: bool = True
    allow_smb_fallback: bool = True
    port: int = 5985
    use_ssl: bool = False


class ServerLimits(BaseModel):
    command_timeout_seconds: int = 120
    script_timeout_seconds: int = 600
    max_parallel_operations: int = 2


class ServerEntry(BaseModel):
    server_id: str
    hostname: str
    environment: str = "integration"
    description: str = ""
    enabled: bool = True
    connection: ServerConnection = Field(default_factory=ServerConnection)
    permissions: ServerPermissions = Field(default_factory=ServerPermissions)
    limits: ServerLimits = Field(default_factory=ServerLimits)
    allow_ip_address: bool = False
    tags: list[str] = Field(default_factory=list)


class GlobalDefaults(BaseModel):
    connection_timeout_seconds: int = 15
    operation_timeout_seconds: int = 120
    reboot_timeout_seconds: int = 900
    poll_interval_seconds: int = 10
    max_output_chars: int = 100_000
    staging_root: str = r"C:\ProgramData\WindowsServerAdminMCP\staging"
    prefer_pssession: bool = True
    allow_smb_fallback: bool = True


class GlobalPolicies(BaseModel):
    block_shell_metacharacters: bool = True
    block_unc_paths: bool = True
    block_wildcards: bool = True
    block_localhost: bool = True
    block_raw_ips_unless_declared: bool = True
    confirmation_token_ttl_seconds: int = 600
    confirmation_token_max_ttl_seconds: int = 900
    max_audit_query_rows: int = 500


class ScriptParameterSpec(BaseModel):
    type: str = "string"
    required: bool = False
    description: str = ""


class ScriptEntry(BaseModel):
    script_id: str
    description: str = ""
    path: str
    sha256: str = "auto"
    timeout_seconds: int = 120
    allowed_servers: list[str] = Field(default_factory=list)
    parameters: dict[str, ScriptParameterSpec] = Field(default_factory=dict)


class OperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationRecord(BaseModel):
    operation_id: str
    tool_name: str
    server_id: str | None = None
    status: OperationStatus
    created_at: str
    updated_at: str
    message: str = ""
    result: OperationResult | None = None


class ConfirmationRecord(BaseModel):
    token_hash: str
    token_prefix: str
    server_id: str
    action: str
    params_hash: str
    created_at: datetime
    expires_at: datetime
    used: bool = False


class AuditEvent(BaseModel):
    timestamp: str
    event_type: str
    tool: str
    server_id: str | None = None
    outcome: str
    duration_ms: int = 0
    operation_id: str | None = None
    parameters: dict[str, Any] | None = None
    error_code: str | None = None
    message: str | None = None
    actor: str | None = None
