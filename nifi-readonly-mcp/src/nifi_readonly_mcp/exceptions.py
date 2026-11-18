"""Excepciones sanitizadas (sin secretos)."""

from __future__ import annotations


class NiFiMcpError(Exception):
    pass


class ConfigError(NiFiMcpError):
    pass


class NiFiClientError(NiFiMcpError):
    def __init__(self, message: str, *, server_id: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.server_id = server_id
        self.status_code = status_code


class DebugEndpointDenied(NiFiMcpError):
    pass


class NiFiAuthError(NiFiMcpError):
    """Fallo de autenticación/credenciales (mensaje sin secretos, apto para el cliente)."""
