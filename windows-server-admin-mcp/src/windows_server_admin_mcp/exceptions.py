from __future__ import annotations

from windows_server_admin_mcp.models import ErrorCode


class WsaMcpError(Exception):
    """Base exception for Windows Server Admin MCP."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(WsaMcpError):
    code = ErrorCode.VALIDATION_ERROR


class PolicyDeniedError(WsaMcpError):
    code = ErrorCode.POLICY_DENIED


class ServerNotFoundError(WsaMcpError):
    code = ErrorCode.SERVER_NOT_FOUND


class AllowlistRejectedError(WsaMcpError):
    code = ErrorCode.ALLOWLIST_REJECTED


class ScriptNotAuthorizedError(WsaMcpError):
    code = ErrorCode.SCRIPT_NOT_AUTHORIZED


class ScriptHashMismatchError(WsaMcpError):
    code = ErrorCode.SCRIPT_HASH_MISMATCH


class ConfirmationError(WsaMcpError):
    code = ErrorCode.CONFIRMATION_INVALID


class ConfirmationExpiredError(WsaMcpError):
    code = ErrorCode.CONFIRMATION_EXPIRED


class ConfirmationUsedError(WsaMcpError):
    code = ErrorCode.CONFIRMATION_USED


class TransportError(WsaMcpError):
    code = ErrorCode.TRANSPORT_ERROR


class OperationNotFoundError(WsaMcpError):
    code = ErrorCode.OPERATION_NOT_FOUND


class RebootNotAllowedError(WsaMcpError):
    code = ErrorCode.REBOOT_NOT_ALLOWED


class LocalhostBlockedError(WsaMcpError):
    code = ErrorCode.LOCALHOST_BLOCKED
