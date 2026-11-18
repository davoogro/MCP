from __future__ import annotations

import ipaddress
import os
import re
import socket

from windows_server_admin_mcp.config import AppConfig
from windows_server_admin_mcp.exceptions import (
    AllowlistRejectedError,
    LocalhostBlockedError,
    PolicyDeniedError,
    RebootNotAllowedError,
    ServerNotFoundError,
    ValidationError,
)
from windows_server_admin_mcp.models import ServerEntry

SHELL_METACHAR_RE = re.compile(r"[;&|`$<>]")
WILDCARD_RE = re.compile(r"[\*\?]")
UNC_PATH_RE = re.compile(r"^\\\\|^//", re.IGNORECASE)
LOCALHOST_NAMES = frozenset(
    {"localhost", "127.0.0.1", "::1", ".", "*", "0.0.0.0", "127.0.0.0/8"}
)
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SCRIPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _local_host_names() -> set[str]:
    names: set[str] = set()
    for value in (
        os.environ.get("COMPUTERNAME"),
        os.environ.get("WSA_MCP_LOCAL_HOSTNAME"),
        os.environ.get("WSA_MCP_LOCAL_FQDN"),
    ):
        if value:
            names.add(value.strip().lower())
    try:
        names.add(socket.gethostname().strip().lower())
        names.add(socket.getfqdn().strip().lower())
    except OSError:
        pass
    names.discard("")
    return names


def is_local_host(hostname: str) -> bool:
    """True when the inventory hostname refers to the machine running this MCP server."""
    host = hostname.strip().lower()
    if host in LOCALHOST_NAMES:
        return True
    local = _local_host_names()
    return host in local


def validate_server_id(server_id: str) -> None:
    if not server_id or not SERVER_ID_RE.match(server_id):
        raise ValidationError(f"Invalid server_id: {server_id!r}")


def validate_service_name(name: str) -> None:
    if not SERVICE_NAME_RE.match(name):
        raise ValidationError(f"Invalid service name: {name!r}")


def validate_script_id(script_id: str) -> None:
    if not SCRIPT_ID_RE.match(script_id):
        raise ValidationError(f"Invalid script_id: {script_id!r}")


def validate_reason(reason: str) -> None:
    if len(reason) < 10 or len(reason) > 500:
        raise ValidationError("Reason must be between 10 and 500 characters")
    if SHELL_METACHAR_RE.search(reason):
        raise ValidationError("Reason contains forbidden shell metacharacters")


def validate_hostname(hostname: str, *, allow_ip: bool = False) -> None:
    host = hostname.strip().lower()
    if not host:
        raise ValidationError("Hostname is required")
    if host in LOCALHOST_NAMES:
        raise LocalhostBlockedError(f"Localhost reference blocked: {hostname}")
    if WILDCARD_RE.search(host):
        raise AllowlistRejectedError("Wildcards are not allowed in hostnames")
    if UNC_PATH_RE.search(host):
        raise AllowlistRejectedError("UNC paths are not allowed")
    if SHELL_METACHAR_RE.search(host):
        raise AllowlistRejectedError("Shell metacharacters are not allowed in hostnames")
    try:
        ipaddress.ip_address(host.strip("[]"))
        if not allow_ip:
            raise AllowlistRejectedError(
                f"Raw IP addresses are blocked unless allow_ip_address is set: {hostname}"
            )
    except ValueError:
        pass


def validate_string_param(value: str, *, field_name: str = "value") -> None:
    if SHELL_METACHAR_RE.search(value):
        raise ValidationError(f"{field_name} contains forbidden shell metacharacters")
    if WILDCARD_RE.search(value):
        raise ValidationError(f"{field_name} contains wildcards")
    if UNC_PATH_RE.search(value):
        raise ValidationError(f"{field_name} contains UNC path")


class PolicyEngine:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def get_server(self, server_id: str) -> ServerEntry:
        validate_server_id(server_id)
        server = self.config.servers.get(server_id)
        if not server:
            raise ServerNotFoundError(f"Server not in inventory: {server_id}")
        validate_hostname(server.hostname, allow_ip=server.allow_ip_address)
        return server

    def require_permission(self, server: ServerEntry, permission: str) -> None:
        if not getattr(server.permissions, permission, False):
            raise PolicyDeniedError(
                f"Permission '{permission}' denied for server {server.server_id}",
                details={"server_id": server.server_id, "permission": permission},
            )

    def check_service_restart(self, server: ServerEntry, service_name: str) -> None:
        self.require_permission(server, "restart_service")
        validate_service_name(service_name)
        upper = service_name.upper()
        for denied in self.config.service_restart_denylist:
            d = denied.upper()
            if upper == d or upper.startswith("MSEXCHANGE"):
                raise PolicyDeniedError(f"Service restart denied for protected service: {service_name}")

    def check_server_restart(self, server: ServerEntry) -> None:
        self.require_permission(server, "restart_server")
        if is_local_host(server.hostname):
            raise LocalhostBlockedError(
                f"Restart blocked: {server.server_id} resolves to the local host running this MCP server"
            )

    def check_cancel_restart(self, server: ServerEntry) -> None:
        self.require_permission(server, "cancel_restart")

    def check_read_operations(self, server: ServerEntry) -> None:
        self.require_permission(server, "read_operations")

    def check_authorized_scripts(self, server: ServerEntry) -> None:
        self.require_permission(server, "authorized_scripts")

    def validate_diagnostic(self, diagnostic: str) -> None:
        if diagnostic not in self.config.approved_diagnostics:
            raise ValidationError(f"Diagnostic not approved: {diagnostic}")

    def validate_tcp_port(self, port: int) -> None:
        if port not in self.config.approved_tcp_ports:
            raise ValidationError(f"TCP port {port} not in approved list")

    def validate_reason_code(self, reason_code: str) -> str:
        if reason_code not in self.config.restart_reason_codes:
            raise ValidationError(f"Unknown restart reason code: {reason_code}")
        return self.config.restart_reason_codes[reason_code]

    def validate_string_param(self, value: str, *, field_name: str = "value") -> None:
        validate_string_param(value, field_name=field_name)

    def is_service_denied(self, service_name: str) -> bool:
        upper = service_name.upper()
        for denied in self.config.service_restart_denylist:
            if upper == denied.upper() or upper.startswith("MSEXCHANGE"):
                return True
        return False
