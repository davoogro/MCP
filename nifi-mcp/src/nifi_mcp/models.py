"""Modelos de configuración y respuestas."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AuthType(str, Enum):
    NONE = "none"
    USERNAME_PASSWORD = "username_password"
    BEARER_TOKEN = "bearer_token"
    CLIENT_CERTIFICATE = "client_certificate"


class TlsConfig(BaseModel):
    verify: bool = True
    check_hostname: bool = True
    ca_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None


class AuthConfig(BaseModel):
    type: AuthType = AuthType.NONE
    username_env: str | None = None
    password_env: str | None = None
    bearer_token_env: str | None = None


class CacheConfig(BaseModel):
    inventory_ttl_seconds: int = 300
    status_ttl_seconds: int = 60


class HealthConfig(BaseModel):
    queue_flowfiles_warning: int = 10_000
    queue_flowfiles_critical: int = 50_000
    queue_bytes_warning: int = 1_073_741_824
    queue_bytes_critical: int = 5_368_709_120
    bulletin_age_minutes: int = 60
    active_threads_warning: int | None = None
    stopped_processor_severity: Literal["informational", "warning"] = "informational"


class GlobalConfig(BaseModel):
    max_parallel_servers: int = 4
    default_timeout_seconds: int = 30
    default_retries: int = 3
    retry_backoff_seconds: float = 0.5
    default_limit: int = 100
    max_limit: int = 10_000
    cache: CacheConfig = Field(default_factory=CacheConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)


class ServerConfig(BaseModel):
    id: str
    name: str
    host: str
    enabled: bool = True
    description: str | None = None
    environment: str | None = None
    base_url: str | None = None
    scheme: str | None = None
    port: int | None = None
    api_path: str | None = None
    tls: TlsConfig = Field(default_factory=TlsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    timeout_seconds: int | None = None
    retries: int | None = None
    tags: list[str] = Field(default_factory=list)

    def effective_timeout(self, global_cfg: GlobalConfig) -> int:
        return self.timeout_seconds or global_cfg.default_timeout_seconds

    def effective_retries(self, global_cfg: GlobalConfig) -> int:
        return self.retries if self.retries is not None else global_cfg.default_retries

    def discovery_urls(self) -> list[str]:
        api = (self.api_path or "/nifi-api").strip("/")
        host = self.host
        urls: list[str] = []

        def add(url: str) -> None:
            cleaned = url.rstrip("/")
            if cleaned not in urls:
                urls.append(cleaned)

        if self.base_url:
            add(self.base_url)
            return urls

        if self.scheme:
            port = f":{self.port}" if self.port else ""
            add(f"{self.scheme}://{host}{port}/{api}")
        elif self.port:
            add(f"https://{host}:{self.port}/{api}")
            add(f"http://{host}:{self.port}/{api}")

        add(f"https://{host}/{api}")
        add(f"https://{host}:8443/{api}")
        add(f"https://{host}:8445/{api}")
        add(f"http://{host}:8080/{api}")
        return urls


class AppConfig(BaseModel):
    global_config: GlobalConfig = Field(alias="global")
    servers: dict[str, ServerConfig]

    model_config = {"populate_by_name": True}

    def enabled_servers(self) -> dict[str, ServerConfig]:
        return {sid: s for sid, s in self.servers.items() if s.enabled}

    def get_server(self, server_id: str) -> ServerConfig:
        if server_id not in self.servers:
            raise KeyError(f"Servidor desconocido: {server_id}")
        srv = self.servers[server_id]
        if not srv.enabled:
            raise ValueError(f"Servidor deshabilitado: {server_id}")
        return srv


class DiscoveryAttempt(BaseModel):
    url: str
    ok: bool = False
    status_code: int | None = None
    error: str | None = None
    latency_ms: float | None = None
    nifi_candidate: bool = False
    authenticated: bool = False
    warning: str | None = None


class ToolMeta(BaseModel):
    ok: bool = True
    total: int | None = None
    returned: int | None = None
    truncated: bool = False
    errors: list[dict[str, Any]] = Field(default_factory=list)
