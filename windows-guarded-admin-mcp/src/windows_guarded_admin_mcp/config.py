from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from windows_guarded_admin_mcp.exceptions import ValidationError
from windows_guarded_admin_mcp.models import (
    GlobalDefaults,
    GlobalPolicies,
    ScriptEntry,
    ServerConnection,
    ServerEntry,
    ServerLimits,
    ServerPermissions,
)


def _project_root() -> Path:
    env_root = os.environ.get("WSA_MCP_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    pkg_dir = Path(__file__).resolve().parent
    dev_root = pkg_dir.parents[1]
    if (dev_root / "powershell").is_dir():
        return dev_root

    if (pkg_dir / "powershell").is_dir():
        return pkg_dir

    for parent in pkg_dir.parents:
        if (parent / "powershell").is_dir():
            return parent

    return dev_root


def _local_appdata_config() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "WindowsServerAdminMCP" / "config"
    return Path.home() / "AppData" / "Local" / "WindowsServerAdminMCP" / "config"


def resolve_config_dir() -> Path:
    env_dir = os.environ.get("WSA_MCP_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    user_override = _local_appdata_config()
    if user_override.is_dir() and any(user_override.glob("*.yaml")):
        return user_override

    project_config = _project_root() / "config"
    if project_config.is_dir():
        return project_config

    user_override.mkdir(parents=True, exist_ok=True)
    return user_override


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValidationError(f"Expected mapping in {path.name}")
    return data


def _merge_scripts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge scripts.local.yaml overlay (extends allowed_servers per script)."""
    merged = dict(base)
    base_scripts = merged.get("scripts") or {}
    overlay_scripts = overlay.get("scripts") or {}
    if not isinstance(base_scripts, dict):
        base_scripts = {}
    if not isinstance(overlay_scripts, dict):
        return merged

    merged_scripts = dict(base_scripts)
    for script_id, overlay_entry in overlay_scripts.items():
        if not isinstance(overlay_entry, dict):
            merged_scripts[script_id] = overlay_entry
            continue
        if script_id not in merged_scripts or not isinstance(merged_scripts[script_id], dict):
            merged_scripts[script_id] = overlay_entry
            continue
        entry = dict(merged_scripts[script_id])
        for key, value in overlay_entry.items():
            if key == "allowed_servers" and isinstance(value, list):
                existing = list(entry.get("allowed_servers") or [])
                entry["allowed_servers"] = list(dict.fromkeys(existing + value))
            else:
                entry[key] = value
        merged_scripts[script_id] = entry
    merged["scripts"] = merged_scripts
    return merged


def _merge_servers(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key == "defaults" and isinstance(value, dict):
            merged.setdefault("defaults", {})
            merged["defaults"].update(value)
        elif key == "servers":
            merged.setdefault("servers", {})
            if isinstance(value, dict):
                merged["servers"].update(value)
            elif isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    sid = item.get("id") or item.get("server_id")
                    if not sid:
                        raise ValidationError("List-style server entry missing id")
                    merged["servers"][str(sid)] = item
            else:
                raise ValidationError("servers must be a mapping or list")
        else:
            merged[key] = value
    return merged


class AppConfig:
    def __init__(
        self,
        config_dir: Path,
        defaults: GlobalDefaults,
        policies: GlobalPolicies,
        servers: dict[str, ServerEntry],
        scripts: dict[str, ScriptEntry],
        service_restart_denylist: list[str],
        approved_diagnostics: list[str],
        approved_tcp_ports: list[int],
        restart_reason_codes: dict[str, str],
    ) -> None:
        self.config_dir = config_dir
        self.defaults = defaults
        self.policies = policies
        self.servers = servers
        self.scripts = scripts
        self.service_restart_denylist = service_restart_denylist
        self.approved_diagnostics = approved_diagnostics
        self.approved_tcp_ports = approved_tcp_ports
        self.restart_reason_codes = restart_reason_codes

    @property
    def project_root(self) -> Path:
        return _project_root()

    @property
    def powershell_dir(self) -> Path:
        return self.project_root / "powershell"

    @property
    def authorized_scripts_dir(self) -> Path:
        return self.project_root / "authorized_scripts"


def _parse_connection(entry: dict[str, Any]) -> ServerConnection:
    """Parse nested connection block or legacy flat transport/port/use_ssl fields."""
    conn_raw = entry.get("connection")
    if isinstance(conn_raw, dict):
        return ServerConnection(
            transport=str(conn_raw.get("transport", "winrm")),
            authentication=str(conn_raw.get("authentication", "current_windows_identity")),
            powershell_executable=str(
                conn_raw.get("powershell_executable", "powershell.exe")
            ),
            use_pssession=bool(conn_raw.get("use_pssession", True)),
            allow_smb_fallback=bool(conn_raw.get("allow_smb_fallback", True)),
            port=int(conn_raw.get("port", 5985)),
            use_ssl=bool(conn_raw.get("use_ssl", False)),
        )

    return ServerConnection(
        transport=str(entry.get("transport", "winrm")),
        authentication=str(entry.get("authentication", "current_windows_identity")),
        powershell_executable=str(entry.get("powershell_executable", "powershell.exe")),
        use_pssession=bool(entry.get("use_pssession", True)),
        allow_smb_fallback=bool(entry.get("allow_smb_fallback", True)),
        port=int(entry.get("port", 5985)),
        use_ssl=bool(entry.get("use_ssl", False)),
    )


def _parse_limits(entry: dict[str, Any]) -> ServerLimits:
    limits_raw = entry.get("limits") or {}
    if not isinstance(limits_raw, dict):
        return ServerLimits()
    return ServerLimits(
        command_timeout_seconds=int(limits_raw.get("command_timeout_seconds", 120)),
        script_timeout_seconds=int(limits_raw.get("script_timeout_seconds", 600)),
        max_parallel_operations=int(limits_raw.get("max_parallel_operations", 2)),
    )


def _normalize_servers_block(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    servers_block = raw.get("servers") or {}
    if isinstance(servers_block, list):
        normalized: dict[str, dict[str, Any]] = {}
        for item in servers_block:
            if not isinstance(item, dict):
                raise ValidationError("Invalid list-style server entry")
            sid = item.get("id") or item.get("server_id")
            if not sid:
                raise ValidationError("List-style server entry missing id")
            normalized[str(sid)] = item
        return normalized
    if isinstance(servers_block, dict):
        return servers_block
    raise ValidationError("servers must be a mapping or list")


def _parse_servers(raw: dict[str, Any]) -> dict[str, ServerEntry]:
    servers_block = _normalize_servers_block(raw)
    result: dict[str, ServerEntry] = {}
    for server_id, entry in servers_block.items():
        if not isinstance(entry, dict):
            raise ValidationError(f"Invalid server entry for {server_id}")
        perms_raw = entry.get("permissions") or {}
        permissions = ServerPermissions(**perms_raw)
        hostname = str(entry.get("hostname", "")).strip()
        if not hostname:
            raise ValidationError(f"Server {server_id} missing hostname")
        if entry.get("credential"):
            raise ValidationError(
                f"Server {server_id}: credential config is not supported; "
                "use connection.authentication: current_windows_identity"
            )
        connection = _parse_connection(entry)
        if connection.authentication != "current_windows_identity":
            raise ValidationError(
                f"Server {server_id}: only current_windows_identity is supported in v1"
            )
        result[server_id] = ServerEntry(
            server_id=server_id,
            hostname=hostname,
            environment=str(entry.get("environment", "integration")),
            description=str(entry.get("description", "")),
            enabled=bool(entry.get("enabled", True)),
            connection=connection,
            permissions=permissions,
            limits=_parse_limits(entry),
            allow_ip_address=bool(entry.get("allow_ip_address", False)),
            tags=list(entry.get("tags") or []),
        )
    return result


def _parse_scripts(raw: dict[str, Any]) -> dict[str, ScriptEntry]:
    scripts_block = raw.get("scripts") or {}
    if not isinstance(scripts_block, dict):
        raise ValidationError("scripts must be a mapping")
    result: dict[str, ScriptEntry] = {}
    for script_id, entry in scripts_block.items():
        if not isinstance(entry, dict):
            raise ValidationError(f"Invalid script entry for {script_id}")
        result[script_id] = ScriptEntry(script_id=script_id, **entry)
    return result


def load_config(config_dir: Path | None = None) -> AppConfig:
    config_dir = config_dir or resolve_config_dir()

    def pick(name: str) -> Path:
        exact = config_dir / name
        if exact.exists():
            return exact
        example = config_dir / name.replace(".yaml", ".example.yaml")
        if example.exists():
            return example
        return exact

    servers_raw = _load_yaml(pick("servers.yaml"))
    local_path = config_dir / "servers.local.yaml"
    if local_path.exists():
        servers_raw = _merge_servers(servers_raw, _load_yaml(local_path))

    policies_raw = _load_yaml(pick("policies.yaml"))
    scripts_raw = _load_yaml(pick("scripts.yaml"))
    scripts_local_path = config_dir / "scripts.local.yaml"
    if scripts_local_path.exists():
        scripts_raw = _merge_scripts(scripts_raw, _load_yaml(scripts_local_path))

    defaults_raw = servers_raw.get("defaults") or {}
    defaults = GlobalDefaults(**defaults_raw) if defaults_raw else GlobalDefaults()

    global_raw = policies_raw.get("global") or {}
    policies = GlobalPolicies(**global_raw) if global_raw else GlobalPolicies()

    servers = _parse_servers(servers_raw)
    scripts = _parse_scripts(scripts_raw)

    try:
        return AppConfig(
            config_dir=config_dir,
            defaults=defaults,
            policies=policies,
            servers=servers,
            scripts=scripts,
            service_restart_denylist=list(policies_raw.get("service_restart_denylist") or []),
            approved_diagnostics=list(policies_raw.get("approved_diagnostics") or []),
            approved_tcp_ports=[int(p) for p in (policies_raw.get("approved_tcp_ports") or [])],
            restart_reason_codes=dict(policies_raw.get("restart_reason_codes") or {}),
        )
    except PydanticValidationError as exc:
        raise ValidationError(f"Configuration validation failed: {exc}") from exc


def compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
