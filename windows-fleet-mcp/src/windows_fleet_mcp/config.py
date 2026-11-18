"""Configuración: inventario YAML, permisos y variables de entorno."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # C:\DVB\mcp\windows-fleet-mcp
MODULE_DIR = Path(__file__).resolve().parent

DEFAULT_PERMISSIONS = {
    "read": True,
    "manage_services": False,
    "manage_processes": False,
    "manage_tasks": False,
    "run_powershell": False,
    "file_transfer": False,
    "restart_server": False,
}

PERMISSION_HINTS = {
    "manage_services": "manage_services en config/servers.local.yaml",
    "manage_processes": "manage_processes en config/servers.local.yaml",
    "manage_tasks": "manage_tasks en config/servers.local.yaml",
    "run_powershell": "run_powershell en config/servers.local.yaml",
    "file_transfer": "file_transfer en config/servers.local.yaml",
    "restart_server": "restart_server en config/servers.local.yaml",
    "read": "read en config/servers.local.yaml",
}


def load_env_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("WINFLEET_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates += [PACKAGE_DIR / ".env", PACKAGE_DIR.parent / ".env"]
    loaded: list[Path] = []
    for path in candidates:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            loaded.append(path)
    return loaded


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _default_config_path() -> Path:
    return PACKAGE_DIR / "config" / "servers.yaml"


def _local_config_path(main_path: Path) -> Path:
    return main_path.parent / "servers.local.yaml"


def load_inventory_raw() -> tuple[dict[str, Any], Path, Optional[Path]]:
    config_env = os.environ.get("WINFLEET_CONFIG", "").strip()
    main_path = Path(config_env) if config_env else _default_config_path()
    if not main_path.is_file():
        raise FileNotFoundError(f"inventario no encontrado: {main_path}")
    with main_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    local_path = _local_config_path(main_path)
    if local_path.is_file():
        with local_path.open("r", encoding="utf-8") as fh:
            local = yaml.safe_load(fh) or {}
        data = deep_merge(data, local)
        return data, main_path, local_path
    return data, main_path, None


@dataclass
class ServerRecord:
    server_id: str
    hostname: str
    environment: str
    tags: list[str]
    port: int
    use_ssl: bool
    open_timeout_seconds: int
    operation_timeout_seconds: int
    permissions: dict[str, bool]
    file_read_denylist: list[str]

    def effective_permissions(self) -> dict[str, bool]:
        merged = dict(DEFAULT_PERMISSIONS)
        merged.update(self.permissions)
        return merged

    def public_dict(self, session_open: bool = False) -> dict[str, Any]:
        return {
            "id": self.server_id,
            "hostname": self.hostname,
            "environment": self.environment,
            "tags": list(self.tags),
            "port": self.port,
            "use_ssl": self.use_ssl,
            "permissions": self.effective_permissions(),
            "session_open": session_open,
        }


@dataclass(frozen=True, repr=False)
class Settings:
    max_response_chars: int
    worker_count: int
    powershell_exe: str
    audit_dir: Path
    config_path: Path
    local_config_path: Optional[Path]
    protected_services: list[str]
    servers: dict[str, ServerRecord]
    defaults: dict[str, Any]

    @classmethod
    def from_env(cls) -> "Settings":
        raw, config_path, local_path = load_inventory_raw()
        defaults = raw.get("defaults") or {}
        protected = list(raw.get("protected_services") or [])
        default_perms = deep_merge({"permissions": DEFAULT_PERMISSIONS}, defaults)
        default_denylist = list(defaults.get("file_read_denylist") or [])
        servers: dict[str, ServerRecord] = {}
        for sid, entry in (raw.get("servers") or {}).items():
            if not isinstance(entry, dict):
                continue
            merged = deep_merge(defaults, entry)
            perms = deep_merge(DEFAULT_PERMISSIONS, merged.get("permissions") or {})
            denylist = list(merged.get("file_read_denylist") or default_denylist)
            servers[sid] = ServerRecord(
                server_id=sid,
                hostname=str(merged.get("hostname") or sid),
                environment=str(merged.get("environment") or "unknown"),
                tags=list(merged.get("tags") or []),
                port=int(merged.get("port") or 5985),
                use_ssl=bool(merged.get("use_ssl", False)),
                open_timeout_seconds=int(merged.get("open_timeout_seconds") or 20),
                operation_timeout_seconds=int(merged.get("operation_timeout_seconds") or 180),
                permissions={k: bool(v) for k, v in perms.items()},
                file_read_denylist=denylist,
            )
        audit_env = os.environ.get("WINFLEET_AUDIT_DIR", "").strip()
        audit_dir = Path(audit_env) if audit_env else PACKAGE_DIR / "logs"
        ps_exe = os.environ.get("WINFLEET_POWERSHELL", "").strip() or "powershell.exe"
        return cls(
            max_response_chars=max(1, _env_int("WINFLEET_MAX_RESPONSE_CHARS", 100_000)),
            worker_count=max(1, _env_int("WINFLEET_WORKERS", 3)),
            powershell_exe=ps_exe,
            audit_dir=audit_dir,
            config_path=config_path,
            local_config_path=local_path,
            protected_services=protected,
            servers=servers,
            defaults=defaults,
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "local_config_path": str(self.local_config_path) if self.local_config_path else None,
            "server_count": len(self.servers),
            "server_ids": sorted(self.servers.keys()),
            "protected_services": self.protected_services,
            "max_response_chars": self.max_response_chars,
            "worker_count": self.worker_count,
            "powershell_exe": self.powershell_exe,
            "audit_dir": str(self.audit_dir),
            "env_files": [str(p) for p in LOADED_ENV_FILES],
        }


_settings: Optional[Settings] = None
_generation = 0


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    global _settings, _generation
    _settings = Settings.from_env()
    _generation += 1
    return _settings


def settings_generation() -> int:
    get_settings()
    return _generation


LOADED_ENV_FILES: list[Path] = load_env_files()


def get_server(server_id: str) -> ServerRecord:
    settings = get_settings()
    if server_id not in settings.servers:
        raise KeyError(server_id)
    return settings.servers[server_id]


def require_permission(server: ServerRecord, perm: str) -> None:
    effective = server.effective_permissions()
    if not effective.get(perm, False):
        hint = PERMISSION_HINTS.get(perm, perm)
        raise PermissionError(
            f"permiso '{perm}' no habilitado para {server.server_id}; habilita {hint}"
        )


ServersSelector = Union[str, list[str], None]


def resolve_server_ids(selector: ServersSelector) -> list[str]:
    settings = get_settings()
    valid = set(settings.servers.keys())
    if selector is None or selector == "":
        raise ValueError("se requiere selector de servidores")
    if isinstance(selector, str):
        items = [selector]
    else:
        items = list(selector)
    if not items:
        raise ValueError("lista de servidores vacía")
    result: set[str] = set()
    for item in items:
        token = str(item).strip()
        if not token:
            continue
        if token == "all":
            result.update(valid)
            continue
        if token.startswith("tag:"):
            tag = token[4:].strip().lower()
            for sid, srv in settings.servers.items():
                if any(t.lower() == tag for t in srv.tags):
                    result.add(sid)
            continue
        if token.startswith("env:"):
            env = token[4:].strip().lower()
            for sid, srv in settings.servers.items():
                if srv.environment.lower() == env:
                    result.add(sid)
            continue
        if token not in valid:
            raise ValueError(
                f"ID de servidor inválido: {token!r}; válidos: {sorted(valid)}"
            )
        result.add(token)
    if not result:
        raise ValueError("ningún servidor coincide con el selector")
    return sorted(result)
