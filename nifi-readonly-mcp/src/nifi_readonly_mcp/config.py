"""Carga de configuración YAML y resolución de rutas."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from nifi_readonly_mcp.exceptions import ConfigError
from nifi_readonly_mcp.models import AppConfig, AuthConfig, AuthType, GlobalConfig, ServerConfig, TlsConfig

INLINE_SECRET_KEYS = re.compile(
    r"^(password|token|secret|api_key|apikey|private_key|client_secret)$", re.I
)


def project_root() -> Path:
    """Raíz del proyecto (…/nifi-readonly-mcp), derivada de este archivo y no del cwd.

    Así ``.env`` y ``config/`` se encuentran aunque el cliente MCP lance el proceso
    desde cualquier directorio. ``NIFI_MCP_PROJECT_ROOT`` permite sobrescribirla.
    """
    env_root = os.environ.get("NIFI_MCP_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    # src/nifi_readonly_mcp/config.py -> parents[1] = raíz del proyecto
    return Path(__file__).resolve().parents[2]


def env_file() -> Path:
    env = os.environ.get("NIFI_MCP_ENV_FILE")
    return Path(env).expanduser() if env else project_root() / ".env"


def resolve_config_dir() -> Path:
    env_dir = os.environ.get("NIFI_MCP_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return project_root() / "config"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Se esperaba un mapa YAML en {path.name}")
    return data


def _assert_no_inline_secrets(entry: dict[str, Any], server_id: str) -> None:
    for key in entry:
        if INLINE_SECRET_KEYS.match(key):
            raise ConfigError(
                f"Secreto inline prohibido en servidor {server_id}: clave '{key}'. "
                "Use *_env en auth."
            )
    auth = entry.get("auth") or {}
    if isinstance(auth, dict):
        for key in auth:
            if INLINE_SECRET_KEYS.match(key) and not key.endswith("_env"):
                raise ConfigError(
                    f"Secreto inline prohibido en auth de {server_id}: '{key}'"
                )


def _build_base_url(entry: dict[str, Any]) -> str | None:
    if entry.get("base_url"):
        return str(entry["base_url"]).rstrip("/")
    scheme = entry.get("scheme")
    host = entry.get("host")
    if scheme and host:
        port = entry.get("port")
        api_path = (entry.get("api_path") or "/nifi-api").strip("/")
        if port:
            return f"{scheme}://{host}:{port}/{api_path}".rstrip("/")
        return f"{scheme}://{host}/{api_path}".rstrip("/")
    return None


def _parse_auth(entry: dict[str, Any]) -> AuthConfig:
    auth_raw = dict(entry.get("auth") or {})
    auth_type = (
        auth_raw.get("type")
        or entry.get("auth_type")
        or "none"
    )
    try:
        at = AuthType(auth_type)
    except ValueError as exc:
        raise ConfigError(f"auth_type inválido: {auth_type}") from exc
    return AuthConfig(
        type=at,
        username_env=auth_raw.get("username_env") or entry.get("username_env"),
        password_env=auth_raw.get("password_env") or entry.get("password_env"),
        bearer_token_env=(
            auth_raw.get("bearer_token_env")
            or auth_raw.get("token_env")
            or entry.get("token_env")
            or entry.get("bearer_token_env")
        ),
    )


def _parse_tls(entry: dict[str, Any]) -> TlsConfig:
    tls_raw = dict(entry.get("tls") or {})
    verify = tls_raw.get("verify")
    if verify is None and "verify_ssl" in entry:
        verify = entry.get("verify_ssl")
    ca = tls_raw.get("ca_cert") or entry.get("ca_bundle")
    client_cert = tls_raw.get("client_cert") or entry.get("client_cert")
    client_key = tls_raw.get("client_key") or entry.get("client_key")
    check_hostname = tls_raw.get("check_hostname")
    if check_hostname is None and "check_hostname" in entry:
        check_hostname = entry.get("check_hostname")
    return TlsConfig(
        verify=True if verify is None else bool(verify),
        check_hostname=True if check_hostname is None else bool(check_hostname),
        ca_cert=_resolve_project_path(ca),
        client_cert=_resolve_project_path(client_cert),
        client_key=_resolve_project_path(client_key),
    )


def _resolve_project_path(value: Any) -> str | None:
    """Rutas relativas del YAML se resuelven contra la raíz del proyecto, nunca el cwd."""
    if not value:
        return None
    p = Path(str(value)).expanduser()
    return str(p if p.is_absolute() else (project_root() / p).resolve())


def _parse_servers(raw: dict[str, Any]) -> dict[str, ServerConfig]:
    servers: dict[str, ServerConfig] = {}
    for sid, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            raise ConfigError(f"Entrada inválida para servidor {sid}")
        _assert_no_inline_secrets(entry, sid)
        servers[sid] = ServerConfig(
            id=sid,
            name=entry.get("name") or sid,
            host=entry["host"],
            enabled=entry.get("enabled", True),
            description=entry.get("description"),
            environment=entry.get("environment"),
            base_url=_build_base_url(entry),
            scheme=entry.get("scheme"),
            port=entry.get("port"),
            api_path=entry.get("api_path"),
            tls=_parse_tls(entry),
            auth=_parse_auth(entry),
            timeout_seconds=entry.get("timeout_seconds") or entry.get("timeout"),
            retries=entry.get("retries"),
            tags=list(entry.get("tags") or []),
        )
    return servers


def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv(env_file(), override=False)
    cfg_dir = resolve_config_dir()
    path = config_path or cfg_dir / "nifi_servers.yaml"
    if not path.exists():
        alt = cfg_dir / "servers.example.yaml"
        if alt.exists():
            path = alt
        else:
            raise ConfigError(f"No se encontró configuración en {cfg_dir}")

    raw = _load_yaml(path)
    global_raw = raw.get("global") or {}
    global_cfg = GlobalConfig(**global_raw)
    servers = _parse_servers(raw.get("servers") or {})
    if not servers:
        raise ConfigError("No hay servidores definidos en la configuración")
    return AppConfig(global_config=global_cfg, servers=servers)


def list_server_ids(include_disabled: bool = False) -> list[str]:
    cfg = load_config()
    if include_disabled:
        return list(cfg.servers.keys())
    return list(cfg.enabled_servers().keys())


def resolve_server_ids(server_id: str | list[str] | None = None) -> list[str]:
    cfg = load_config()
    if server_id is None:
        return list(cfg.enabled_servers().keys())
    if isinstance(server_id, list):
        parts = [p.strip() for p in server_id if p and str(p).strip()]
    else:
        text = str(server_id).strip()
        if text.lower() in ("all", "*", ""):
            return list(cfg.enabled_servers().keys())
        parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return list(cfg.enabled_servers().keys())
    unknown = [p for p in parts if p not in cfg.servers]
    if unknown:
        raise ConfigError(f"Servidores desconocidos: {', '.join(unknown)}")
    disabled = [p for p in parts if not cfg.servers[p].enabled]
    if disabled:
        raise ConfigError(f"Servidores deshabilitados: {', '.join(disabled)}")
    return parts
