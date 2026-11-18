"""Servidor MCP windows-winrm-fleet-mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional, Union

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from windows_winrm_fleet_mcp import __version__
from windows_winrm_fleet_mcp.config import LOADED_ENV_FILES, get_settings
from windows_winrm_fleet_mcp import ops
from windows_winrm_fleet_mcp.ops import ToolError
from windows_winrm_fleet_mcp.util import bound_response

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("windows-winrm-fleet-mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, openWorldHint=True, destructiveHint=True)

ServersArg = Union[str, list[str], None]


def _instructions() -> str:
    settings = get_settings()
    ids = ", ".join(sorted(settings.servers.keys()))
    return (
        "Administración de flota Windows Server vía WinRM con sesiones PowerShell persistentes. "
        f"Servidores en inventario: {ids}. "
        "Selector `servers`: ID, lista, `all`, `tag:<etiqueta>` o `env:<entorno>`. "
        "Flujo: fleet_overview → herramienta específica → run_powershell solo si no hay tool de lectura. "
        "Las tools que modifican requieren permisos opt-in en config/servers.local.yaml y que el usuario "
        "lo haya pedido explícitamente. Reinicio de servidor: prepare_server_restart → confirm_server_restart "
        "(token TTL 10 min). Respuestas acotadas por WINFLEET_MAX_RESPONSE_CHARS."
    )


mcp = MCPServer(
    "windows-winrm-fleet",
    title="Windows Fleet Admin",
    version=__version__,
    instructions=_instructions(),
)


def _invoke(fn) -> dict[str, Any]:
    try:
        data = fn()
    except ToolError as exc:
        logger.warning("error %s: %s", exc.code, exc)
        body: dict[str, Any] = {"ok": False, "error": str(exc), "error_code": exc.code}
        body.update(exc.extra)
        return body
    except Exception as exc:
        logger.warning("error inesperado %s", type(exc).__name__)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_code": "RemoteError"}
    if not isinstance(data, dict):
        data = {"result": data}
    if "ok" not in data:
        payload = {"ok": True}
        payload.update(data)
        return bound_response(payload)
    return bound_response(data)


@mcp.tool(annotations=READ_ONLY)
def list_servers() -> dict[str, Any]:
    """Inventario: id, hostname, entorno, tags, permisos efectivos y sesión WinRM abierta."""
    return _invoke(ops.list_servers)


@mcp.tool(annotations=READ_ONLY)
def test_connection(servers: ServersArg = "all") -> dict[str, Any]:
    """DNS, TCP WinRM (3 s), sesión remota, whoami, versión PS y latencia (paralelo)."""
    return _invoke(lambda: ops.test_connection(servers=servers))


@mcp.tool(annotations=READ_ONLY)
def fleet_overview(
    servers: ServersArg = "all",
    disk_free_pct_warn: float = 10,
    mem_used_pct_warn: float = 90,
) -> dict[str, Any]:
    """Snapshot de salud: OS, CPU, memoria, discos, reinicio pendiente, servicios Auto detenidos, eventos 24 h."""
    return _invoke(
        lambda: ops.fleet_overview(
            servers=servers,
            disk_free_pct_warn=disk_free_pct_warn,
            mem_used_pct_warn=mem_used_pct_warn,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def get_server_health(servers: ServersArg) -> dict[str, Any]:
    """Salud detallada: OS, build, dominio, uptime, CPU, memoria, todos los indicadores de reinicio pendiente."""
    return _invoke(lambda: ops.get_server_health(servers=servers))


@mcp.tool(annotations=READ_ONLY)
def get_disks(servers: ServersArg, include_volumes: bool = False) -> dict[str, Any]:
    """Discos lógicos y volúmenes opcionales con porcentaje libre."""
    return _invoke(lambda: ops.get_disks(servers=servers, include_volumes=include_volumes))


@mcp.tool(annotations=READ_ONLY)
def get_services(
    servers: ServersArg,
    name: Optional[str] = None,
    state: Optional[str] = None,
    start_mode: Optional[str] = None,
    problems_only: bool = False,
    include_trigger_start: bool = False,
    limit: int = 300,
) -> dict[str, Any]:
    """Servicios Windows; name admite comodines *; problems_only = Auto y no Running (excluye trigger/delay por defecto)."""
    return _invoke(
        lambda: ops.get_services(
            servers=servers,
            name=name,
            state=state,
            start_mode=start_mode,
            problems_only=problems_only,
            include_trigger_start=include_trigger_start,
            limit=limit,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def get_processes(
    servers: ServersArg,
    name: Optional[str] = None,
    sort_by: str = "cpu",
    top: int = 25,
    include_command_line: bool = False,
    include_owner: bool = False,
) -> dict[str, Any]:
    """Procesos ordenados por CPU o memoria."""
    return _invoke(
        lambda: ops.get_processes(
            servers=servers,
            name=name,
            sort_by=sort_by,
            top=top,
            include_command_line=include_command_line,
            include_owner=include_owner,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def get_events(
    servers: ServersArg,
    log: str = "System",
    level: Optional[str] = None,
    since_hours: float = 24,
    provider: Optional[str] = None,
    event_id: Optional[int] = None,
    contains: Optional[str] = None,
    max_events: int = 50,
) -> dict[str, Any]:
    """Eventos Get-WinEvent; mensajes recortados a 2000 caracteres."""
    return _invoke(
        lambda: ops.get_events(
            servers=servers,
            log=log,
            level=level,
            since_hours=since_hours,
            provider=provider,
            event_id=event_id,
            contains=contains,
            max_events=max_events,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def list_event_logs(server: str, contains: Optional[str] = None) -> dict[str, Any]:
    """Canales de eventos disponibles con conteo y tamaño."""
    return _invoke(lambda: ops.list_event_logs(server=server, contains=contains))


@mcp.tool(annotations=READ_ONLY)
def get_scheduled_tasks(
    servers: ServersArg,
    path: str = "\\",
    name: Optional[str] = None,
    failed_only: bool = False,
    include_microsoft: bool = False,
) -> dict[str, Any]:
    """Tareas programadas con estado y LastTaskResult."""
    return _invoke(
        lambda: ops.get_scheduled_tasks(
            servers=servers,
            path=path,
            name=name,
            failed_only=failed_only,
            include_microsoft=include_microsoft,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def get_installed_updates(servers: ServersArg, last: int = 20) -> dict[str, Any]:
    """Hotfixes instalados ordenados por fecha."""
    return _invoke(lambda: ops.get_installed_updates(servers=servers, last=last))


@mcp.tool(annotations=READ_ONLY)
def get_installed_software(servers: ServersArg, name: Optional[str] = None) -> dict[str, Any]:
    """Software del registro Uninstall 32/64 bits."""
    return _invoke(lambda: ops.get_installed_software(servers=servers, name=name))


@mcp.tool(annotations=READ_ONLY)
def get_network(server: str) -> dict[str, Any]:
    """IPs, gateways, DNS, puertos en escucha y resumen de conexiones establecidas."""
    return _invoke(lambda: ops.get_network(server=server))


@mcp.tool(annotations=READ_ONLY)
def test_network_from_server(
    servers: ServersArg,
    target: str,
    port: Optional[int] = None,
) -> dict[str, Any]:
    """Ping/Test-NetConnection desde el servidor hacia target (hostname o IP)."""
    return _invoke(lambda: ops.test_network_from_server(servers=servers, target=target, port=port))


@mcp.tool(annotations=READ_ONLY)
def get_sessions(servers: ServersArg) -> dict[str, Any]:
    """Sesiones de usuario (quser) y RDP."""
    return _invoke(lambda: ops.get_sessions(servers=servers))


@mcp.tool(annotations=READ_ONLY)
def get_certificates(
    servers: ServersArg,
    store: str = "My",
    expiring_days: Optional[int] = None,
) -> dict[str, Any]:
    """Certificados LocalMachine con días restantes."""
    return _invoke(
        lambda: ops.get_certificates(servers=servers, store=store, expiring_days=expiring_days)
    )


@mcp.tool(annotations=READ_ONLY)
def get_local_admins(servers: ServersArg) -> dict[str, Any]:
    """Miembros del grupo Administradores local."""
    return _invoke(lambda: ops.get_local_admins(servers=servers))


@mcp.tool(annotations=READ_ONLY)
def get_shares(servers: ServersArg) -> dict[str, Any]:
    """Recursos compartidos SMB."""
    return _invoke(lambda: ops.get_shares(servers=servers))


@mcp.tool(annotations=READ_ONLY)
def get_iis(servers: ServersArg) -> dict[str, Any]:
    """Sitios IIS, bindings y app pools si WebAdministration está instalado."""
    return _invoke(lambda: ops.get_iis(servers=servers))


@mcp.tool(annotations=READ_ONLY)
def get_performance(servers: ServersArg, samples: int = 3, interval_seconds: int = 1) -> dict[str, Any]:
    """Contadores de rendimiento promediados (CPU, disco, memoria, red)."""
    return _invoke(
        lambda: ops.get_performance(servers=servers, samples=samples, interval_seconds=interval_seconds)
    )


@mcp.tool(annotations=READ_ONLY)
def get_firewall_rules(
    server: str,
    enabled_only: bool = True,
    direction: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Reglas de firewall de un servidor."""
    return _invoke(
        lambda: ops.get_firewall_rules(
            server=server,
            enabled_only=enabled_only,
            direction=direction,
            name=name,
            limit=limit,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def list_remote_dir(
    server: str,
    path: str,
    pattern: str = "*",
    recurse: bool = False,
    max_entries: int = 500,
) -> dict[str, Any]:
    """Directorio remoto (ruta absoluta en el servidor)."""
    return _invoke(
        lambda: ops.list_remote_dir(
            server=server,
            path=path,
            pattern=pattern,
            recurse=recurse,
            max_entries=max_entries,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def read_remote_file(
    server: str,
    path: str,
    offset: int = 0,
    max_bytes: int = 1048576,
    encoding: str = "auto",
) -> dict[str, Any]:
    """Lee texto remoto; respeta file_read_denylist; rechaza binario."""
    return _invoke(
        lambda: ops.read_remote_file(
            server=server,
            path=path,
            offset=offset,
            max_bytes=max_bytes,
            encoding=encoding,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def tail_remote_file(
    server: str,
    path: str,
    lines: int = 200,
    contains: Optional[str] = None,
) -> dict[str, Any]:
    """Últimas líneas de un archivo remoto."""
    return _invoke(lambda: ops.tail_remote_file(server=server, path=path, lines=lines, contains=contains))


@mcp.tool(annotations=READ_ONLY)
def search_remote_files(
    server: str,
    path: str,
    name_pattern: str = "*",
    contains: Optional[str] = None,
    max_results: int = 200,
    max_depth: int = 8,
) -> dict[str, Any]:
    """Busca archivos por nombre y opcionalmente contenido."""
    return _invoke(
        lambda: ops.search_remote_files(
            server=server,
            path=path,
            name_pattern=name_pattern,
            contains=contains,
            max_results=max_results,
            max_depth=max_depth,
        )
    )


@mcp.tool(annotations=READ_ONLY)
def compare_servers(
    servers: ServersArg,
    aspect: str = "services",
) -> dict[str, Any]:
    """Diferencias entre servidores: services, hotfixes, software o scheduled_tasks."""
    return _invoke(lambda: ops.compare_servers(servers=servers, aspect=aspect))


@mcp.tool(annotations=READ_ONLY)
def query_audit_log(
    since_hours: float = 24,
    tool: Optional[str] = None,
    server: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Consulta el log de auditoría JSONL local."""
    return _invoke(
        lambda: ops.query_audit_log(since_hours=since_hours, tool=tool, server=server, limit=limit)
    )


@mcp.tool(annotations=READ_ONLY)
def wait_for_server(server: str, timeout_seconds: int = 900, poll_seconds: int = 15) -> dict[str, Any]:
    """Espera a que WinRM vuelva tras un reinicio."""
    return _invoke(
        lambda: ops.wait_for_server(
            server=server,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def run_powershell(servers: ServersArg, script: str, timeout_seconds: int = 180) -> dict[str, Any]:
    """
    PowerShell arbitrario en uno o varios servidores (requiere run_powershell).
    Prefiere las tools específicas de lectura; usa esta solo para lo no cubierto.
    """
    return _invoke(lambda: ops.run_powershell(servers=servers, script=script, timeout_seconds=timeout_seconds))


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def manage_service(
    servers: ServersArg,
    name: str,
    action: str = "start",
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Inicia, detiene o reinicia un servicio (nombre exacto; servicios protegidos rechazados)."""
    return _invoke(
        lambda: ops.manage_service(
            servers=servers,
            name=name,
            action=action,
            dry_run=dry_run,
            force=force,
        )
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def set_service_start_mode(
    server: str,
    name: str,
    start_mode: str = "Automatic",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Cambia el tipo de inicio de un servicio."""
    return _invoke(
        lambda: ops.set_service_start_mode(
            server=server,
            name=name,
            start_mode=start_mode,
            dry_run=dry_run,
        )
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def stop_process(
    server: str,
    pid: Optional[int] = None,
    name: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Detiene un proceso por PID o nombre (procesos del sistema rechazados)."""
    return _invoke(
        lambda: ops.stop_process(server=server, pid=pid, name=name, dry_run=dry_run)
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def manage_scheduled_task(
    server: str,
    task_path: str,
    task_name: str,
    action: str = "run",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Ejecuta, detiene, habilita o deshabilita una tarea programada."""
    return _invoke(
        lambda: ops.manage_scheduled_task(
            server=server,
            task_path=task_path,
            task_name=task_name,
            action=action,
            dry_run=dry_run,
        )
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def write_remote_file(
    server: str,
    path: str,
    content: str,
    append: bool = False,
    create_dirs: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Escribe texto UTF-8 remoto con backup opcional."""
    return _invoke(
        lambda: ops.write_remote_file(
            server=server,
            path=path,
            content=content,
            append=append,
            create_dirs=create_dirs,
            backup=backup,
        )
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def copy_to_server(server: str, local_path: str, remote_path: str) -> dict[str, Any]:
    """Copia archivo o carpeta al servidor (Copy-Item -ToSession)."""
    return _invoke(lambda: ops.copy_to_server(server=server, local_path=local_path, remote_path=remote_path))


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def copy_from_server(server: str, remote_path: str, local_path: str) -> dict[str, Any]:
    """Copia archivo o carpeta desde el servidor (Copy-Item -FromSession)."""
    return _invoke(
        lambda: ops.copy_from_server(server=server, remote_path=remote_path, local_path=local_path)
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def prepare_server_restart(server: str, reason: str, delay_seconds: int = 60) -> dict[str, Any]:
    """Paso 1: emite token de un solo uso (TTL 10 min) y contexto de reinicio."""
    return _invoke(
        lambda: ops.prepare_server_restart(server=server, reason=reason, delay_seconds=delay_seconds)
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def confirm_server_restart(
    server: str,
    reason: str,
    delay_seconds: int,
    token: str,
) -> dict[str, Any]:
    """Paso 2: confirma reinicio con token válido (shutdown /r)."""
    return _invoke(
        lambda: ops.confirm_server_restart(
            server=server,
            reason=reason,
            delay_seconds=delay_seconds,
            token=token,
        )
    )


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def cancel_server_restart(server: str) -> dict[str, Any]:
    """Cancela un reinicio programado (shutdown /a)."""
    return _invoke(lambda: ops.cancel_server_restart(server=server))


@mcp.tool(annotations=WRITE_DESTRUCTIVE)
def disconnect_sessions(servers: ServersArg = None) -> dict[str, Any]:
    """Cierra PSSession cacheadas localmente (no afecta servidores)."""
    return _invoke(lambda: ops.disconnect_sessions(servers=servers))


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cli_test() -> int:
    out = test_connection(servers="all")
    _print(out)
    return 0 if out.get("ok") else 1


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        read_only = sum(
            1 for t in listed.tools if t.annotations and t.annotations.read_only_hint
        )
        destructive = sum(
            1 for t in listed.tools if t.annotations and t.annotations.destructive_hint
        )
        expected = 40
        _print(
            {
                "ok": True,
                "tool_count": len(names),
                "expected": expected,
                "read_only_hint": read_only,
                "destructive_hint": destructive,
                "tools": names,
            }
        )
        return 0 if len(names) == expected else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor MCP Windows Fleet")
    parser.add_argument("--list", action="store_true", help="Muestra configuración e inventario")
    parser.add_argument("--test", action="store_true", help="test_connection a all (JSON)")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía cliente MCP")
    args = parser.parse_args()

    if args.list:
        settings = get_settings()
        body = settings.public_dict()
        body["tools"] = 40
        _print(body)
        return 0
    if args.test:
        return cli_test()
    if args.handshake:
        return asyncio.run(_handshake())

    settings = get_settings()
    logger.info(
        "windows-winrm-fleet-mcp v%s — env: %s — servidores: %s",
        __version__,
        [str(p) for p in LOADED_ENV_FILES],
        ",".join(settings.servers.keys()),
    )
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
