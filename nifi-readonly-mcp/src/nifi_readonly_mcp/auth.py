"""Autenticación NiFi: tokens en memoria, sin registrar secretos."""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass, field
from typing import Any

import httpx

from nifi_readonly_mcp.exceptions import NiFiAuthError
from nifi_readonly_mcp.models import AuthType, GlobalConfig, ServerConfig

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "secret",
        "access_token",
        "authorization",
        "api_key",
        "apikey",
        "private_key",
        "client_secret",
    }
)


def sanitize_for_log(value: Any) -> str:
    if value is None:
        return "<none>"
    # Algunas excepciones httpx (p.ej. ConnectTimeout) tienen str() vacío.
    text = str(value) or type(value).__name__
    if len(text) > 200:
        return text[:200] + "…"
    return text


def looks_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(part in lower for part in SENSITIVE_KEYS)


def redact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if looks_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_mapping(v) if isinstance(v, dict) else v for v in value]
    return value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if looks_sensitive_key(k):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = redact_mapping(v)
        elif isinstance(v, list):
            out[k] = [
                redact_mapping(item) if isinstance(item, dict) else item for item in v
            ]
        else:
            out[k] = v
    return out


@dataclass
class AuthState:
    bearer_token: str | None = None
    token_expires_at: float | None = None


@dataclass
class AuthManager:
    """Gestiona credenciales por servidor; tokens solo en memoria."""

    _states: dict[str, AuthState] = field(default_factory=dict)

    def _state(self, server_id: str) -> AuthState:
        if server_id not in self._states:
            self._states[server_id] = AuthState()
        return self._states[server_id]

    def resolve_credentials(self, server: ServerConfig) -> dict[str, str | None]:
        auth = server.auth
        username = password = token = None
        if auth.username_env:
            username = os.environ.get(auth.username_env)
        if auth.password_env:
            password = os.environ.get(auth.password_env)
        if auth.bearer_token_env:
            token = os.environ.get(auth.bearer_token_env)
        return {"username": username, "password": password, "token": token}

    def build_ssl_context(self, server: ServerConfig) -> ssl.SSLContext | bool | None:
        tls = server.tls
        if not tls.verify and not tls.ca_cert and not tls.client_cert:
            return False
        ctx = ssl.create_default_context()
        if tls.ca_cert:
            ctx.load_verify_locations(tls.ca_cert)
        if not tls.verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif not tls.check_hostname:
            ctx.check_hostname = False
        if tls.client_cert:
            ctx.load_cert_chain(tls.client_cert, tls.client_key)
        return ctx

    def httpx_verify(self, server: ServerConfig) -> ssl.SSLContext | bool:
        result = self.build_ssl_context(server)
        if result is None:
            return True
        return result

    async def ensure_auth_headers(
        self,
        client: httpx.AsyncClient,
        server: ServerConfig,
        base_url: str,
        global_cfg: GlobalConfig,
    ) -> dict[str, str]:
        auth = server.auth
        creds = self.resolve_credentials(server)
        headers: dict[str, str] = {"Accept": "application/json"}

        if auth.type == AuthType.NONE:
            return headers

        if auth.type == AuthType.CLIENT_CERTIFICATE:
            return headers

        if auth.type == AuthType.BEARER_TOKEN:
            token = creds.get("token")
            if not token:
                raise NiFiAuthError(
                    f"Falta token bearer (env {auth.bearer_token_env}) para {server.id}"
                )
            headers["Authorization"] = f"Bearer {token}"
            return headers

        if auth.type == AuthType.USERNAME_PASSWORD:
            state = self._state(server.id)
            if state.bearer_token:
                headers["Authorization"] = f"Bearer {state.bearer_token}"
                return headers
            username, password = creds.get("username"), creds.get("password")
            if not username or not password:
                raise NiFiAuthError(
                    f"Faltan credenciales (env {auth.username_env}/{auth.password_env}) "
                    f"para {server.id}"
                )
            token_url = f"{base_url.rstrip('/')}/access/token"
            logger.debug("Solicitando token para servidor %s", server.id)
            try:
                resp = await client.post(
                    token_url,
                    data={"username": username, "password": password},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=server.effective_timeout(global_cfg),
                )
            except httpx.HTTPError as exc:
                raise NiFiAuthError(
                    f"Error de red al autenticar en {server.id}: {sanitize_for_log(exc)}"
                ) from exc
            if resp.status_code >= 400:
                detail = (resp.text or "").strip().replace("\n", " ")[:180]
                raise NiFiAuthError(
                    f"Autenticación fallida en {server.id}: HTTP {resp.status_code}"
                    + (f" ({detail})" if detail else "")
                )
            token_text = resp.text.strip()
            if not token_text:
                raise NiFiAuthError(f"Token vacío recibido de {server.id}")
            state.bearer_token = token_text
            headers["Authorization"] = f"Bearer {token_text}"
            return headers

        raise NiFiAuthError(f"Tipo de auth no soportado: {auth.type}")

    def clear_token(self, server_id: str) -> None:
        self._states.pop(server_id, None)
