"""Servidor MCP WSL.

Uso:
  python -m wsl_mcp               # MCP server (stdio)
  python -m wsl_mcp --list        # configuración cargada
  python -m wsl_mcp --test        # probar conexión WSL
  python -m wsl_mcp --handshake   # listar tools vía SDK in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from wsl_mcp import __version__
from wsl_mcp import jobs as job_ops
from wsl_mcp import ops
from wsl_mcp.config import LOADED_ENV_FILES, get_settings

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wsl-mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
ADDITIVE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _instructions() -> str:
    s = get_settings()
    return (
        f"Ejecuta comandos y operaciones de archivos dentro de distros WSL (default '{s.distro}', "
        f"shell {s.shell}, usuario {s.user or 'por defecto'}, cwd por defecto {s.default_cwd or '~'}). "
        "Parámetro distro opcional en casi todas las tools; list_distros muestra las instaladas. "
        "Las rutas Windows (C:\\...) se convierten a /mnt/c/...; '~' se expande al HOME de Linux. "
        "Para inspeccionar usa wsl_status, list_directory, read_file (líneas o offset_bytes/max_bytes), "
        "tail_file, file_info, find_files, grep_text, which, list_processes, docker_ps, "
        "systemctl_status, disk_usage y list_distros antes que run_command. "
        "run_command acepta timeout hasta 3600 s y stdin opcional; run_script ejecuta bash multilínea. "
        "Jobs largos: start_job → get_job/list_jobs → stop_job; la salida persiste en "
        "%LOCALAPPDATA%\\wsl-mcp\\jobs\\ entre llamadas pero no si reinicias el proceso MCP. "
        "copy_between copia archivos o carpetas (cp -a) entre rutas Windows y Linux. "
        "write_file sobrescribe archivos; úsalo solo cuando haga falta modificar algo. "
        f"Timeout por defecto {s.command_timeout}s. stdout/stderr se truncan a {s.max_output} chars "
        "(inicio + final); filtra con grep/head/tail o read_file con offset."
    )


mcp = MCPServer("WSL", version=__version__, instructions=_instructions())


@mcp.tool(annotations=DESTRUCTIVE)
def run_command(
    command: str,
    cwd: str = "",
    timeout: int = 0,
    stdin: str = "",
    distro: str = "",
) -> dict[str, Any]:
    """
    Ejecuta un comando shell en WSL y retorna stdout, stderr y exit code.

    Args:
        command: Comando bash (ej. 'ls -la', 'python3 --version').
        cwd: Directorio de trabajo Linux o Windows (opcional; '~' = HOME).
        timeout: Segundos (0 = WSL_COMMAND_TIMEOUT; máximo 3600).
        stdin: Texto opcional enviado al stdin del comando.
        distro: Distro WSL (vacío = WSL_DISTRO).
    """
    return ops.op_run_command(command, cwd=cwd, timeout=timeout, stdin=stdin, distro=distro)


@mcp.tool(annotations=DESTRUCTIVE)
def run_script(script: str, cwd: str = "", timeout: int = 0, distro: str = "") -> dict[str, Any]:
    """
    Ejecuta un script bash multilínea en WSL.

    Args:
        script: Contenido del script bash.
        cwd: Directorio de trabajo (opcional).
        timeout: Timeout en segundos (0 = default, máx. 3600).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_run_script(script, cwd=cwd, timeout=timeout, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def read_file(
    path: str,
    offset: int = 1,
    limit: int = 200,
    offset_bytes: int = 0,
    max_bytes: int = 0,
    distro: str = "",
) -> dict[str, Any]:
    """
    Lee un archivo del filesystem de WSL por líneas o por bytes.

    Args:
        path: Ruta Linux o Windows (C:\\... → /mnt/c/...).
        offset: Línea inicial 1-based (modo líneas).
        limit: Máximo de líneas (modo líneas, max 2000).
        offset_bytes: Desplazamiento en bytes (modo bytes; activa si max_bytes > 0 o offset_bytes > 0).
        max_bytes: Bytes a leer (modo bytes, max 2_000_000).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_read_file(
        path,
        offset=offset,
        limit=limit,
        offset_bytes=offset_bytes,
        max_bytes=max_bytes,
        distro=distro,
    )


@mcp.tool(annotations=DESTRUCTIVE)
def write_file(path: str, content: str, append: bool = False, distro: str = "") -> dict[str, Any]:
    """
    Escribe contenido a un archivo en WSL.

    Args:
        path: Ruta del archivo en WSL.
        content: Texto a escribir.
        append: Si True, agrega al final.
        distro: Distro WSL (vacío = default).
    """
    return ops.op_write_file(path, content, append=append, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def list_directory(path: str = ".", show_hidden: bool = False, distro: str = "") -> dict[str, Any]:
    """
    Lista el contenido de un directorio en WSL.

    Args:
        path: Ruta del directorio (default: directorio por defecto o ~).
        show_hidden: Incluir archivos ocultos.
        distro: Distro WSL (vacío = default).
    """
    return ops.op_list_directory(path=path, show_hidden=show_hidden, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def file_info(path: str, distro: str = "") -> dict[str, Any]:
    """
    Obtiene metadatos de un archivo o directorio en WSL.

    Args:
        path: Ruta en WSL.
        distro: Distro WSL (vacío = default).
    """
    return ops.op_file_info(path, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def find_files(
    path: str = ".",
    pattern: str = "*",
    max_depth: int = 5,
    distro: str = "",
) -> dict[str, Any]:
    """
    Busca archivos por patrón glob en WSL.

    Args:
        path: Directorio raíz de búsqueda.
        pattern: Patrón glob (ej. '*.py').
        max_depth: Profundidad máxima (max 10).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_find_files(path=path, pattern=pattern, max_depth=max_depth, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def which(command_name: str, distro: str = "") -> dict[str, Any]:
    """
    Localiza un ejecutable en el PATH de WSL.

    Args:
        command_name: Nombre del comando (ej. python3, git, docker).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_which(command_name, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def grep_text(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    max_results: int = 80,
    distro: str = "",
) -> dict[str, Any]:
    """
    Busca texto con grep -n en WSL (máx. 200 coincidencias).

    Args:
        pattern: Expresión regular de grep.
        path: Directorio o archivo raíz.
        glob: Filtro --include (ej. '*.py').
        max_results: Límite de líneas (max 200).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_grep_text(
        pattern,
        path=path,
        glob=glob,
        max_results=max_results,
        distro=distro,
    )


@mcp.tool(annotations=READ_ONLY)
def tail_file(path: str, lines: int = 50, distro: str = "") -> dict[str, Any]:
    """
    Lee las últimas N líneas de un archivo en WSL.

    Args:
        path: Ruta del archivo.
        lines: Número de líneas (max 2000).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_tail_file(path, lines=lines, distro=distro)


@mcp.tool(annotations=ADDITIVE_IDEMPOTENT)
def make_directory(path: str, distro: str = "") -> dict[str, Any]:
    """
    Crea un directorio en WSL (mkdir -p).

    Args:
        path: Ruta Linux o Windows.
        distro: Distro WSL (vacío = default).
    """
    return ops.op_make_directory(path, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def list_processes(filter: str = "", limit: int = 40, distro: str = "") -> dict[str, Any]:
    """
    Lista procesos en WSL (ps aux).

    Args:
        filter: Substring opcional para filtrar COMMAND/USER.
        limit: Máximo de filas (max 200).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_list_processes(filter=filter, limit=limit, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def wsl_status(distro: str = "") -> dict[str, Any]:
    """
    Retorna estado de WSL: distro, usuario, kernel, uptime y recursos.

    Args:
        distro: Distro WSL a consultar (vacío = WSL_DISTRO).
    """
    return ops.op_wsl_status(distro=distro)


@mcp.tool(annotations=READ_ONLY)
def list_distros() -> dict[str, Any]:
    """Lista todas las distribuciones WSL instaladas en Windows."""
    return ops.op_list_distros()


@mcp.tool(annotations=DESTRUCTIVE)
def start_job(command: str, cwd: str = "", distro: str = "", timeout: int = 0) -> dict[str, Any]:
    """
    Inicia un comando WSL en segundo plano y devuelve job_id.

    La salida se escribe en %%LOCALAPPDATA%%\\wsl-mcp\\jobs\\<job_id>\\.
    El job sobrevive entre llamadas MCP pero no al reiniciar el proceso del servidor.

    Args:
        command: Comando bash a ejecutar.
        cwd: Directorio de trabajo opcional.
        distro: Distro WSL (vacío = default).
        timeout: Límite en segundos dentro de WSL (0 = default, máx. 3600).
    """
    return job_ops.start_job(command, cwd=cwd or None, distro=distro or None, timeout=timeout)


@mcp.tool(annotations=READ_ONLY)
def get_job(
    job_id: str,
    tail_lines: int = 200,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
) -> dict[str, Any]:
    """
    Consulta estado y salida acumulada de un job en segundo plano.

    Args:
        job_id: Identificador devuelto por start_job.
        tail_lines: Si > 0, devuelve las últimas N líneas de stdout/stderr (ignora offsets).
        stdout_offset: Byte offset en stdout.log (si tail_lines=0).
        stderr_offset: Byte offset en stderr.log (si tail_lines=0).
    """
    return job_ops.get_job(
        job_id,
        tail_lines=tail_lines,
        stdout_offset=stdout_offset,
        stderr_offset=stderr_offset,
    )


@mcp.tool(annotations=READ_ONLY)
def list_jobs() -> dict[str, Any]:
    """Lista jobs conocidos en %%LOCALAPPDATA%%\\wsl-mcp\\jobs\\."""
    return job_ops.list_jobs()


@mcp.tool(annotations=DESTRUCTIVE)
def stop_job(job_id: str) -> dict[str, Any]:
    """
    Detiene un job en ejecución (taskkill del proceso wsl.exe en Windows).

    Args:
        job_id: Identificador del job.
    """
    return job_ops.stop_job(job_id)


@mcp.tool(annotations=ADDITIVE_IDEMPOTENT)
def copy_between(src: str, dst: str, distro: str = "") -> dict[str, Any]:
    """
    Copia archivos o carpetas entre rutas Windows y Linux vía cp -a en WSL.

    Args:
        src: Origen (C:\\... o /home/...).
        dst: Destino.
        distro: Distro WSL (vacío = default).
    """
    return ops.op_copy_between(src, dst, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def docker_ps(all_containers: bool = False, distro: str = "") -> dict[str, Any]:
    """
    Atajo: docker ps en WSL. Error claro si docker no está instalado.

    Args:
        all_containers: Equivalente a docker ps -a.
        distro: Distro WSL (vacío = default).
    """
    return ops.op_docker_ps(all_containers=all_containers, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def systemctl_status(unit: Optional[str] = None, distro: str = "") -> dict[str, Any]:
    """
    Atajo: systemctl status de una unidad o resumen de servicios.

    Args:
        unit: Nombre de unidad systemd; vacío = unidades failed + running (recortado).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_systemctl_status(unit=unit, distro=distro)


@mcp.tool(annotations=READ_ONLY)
def disk_usage(path: str = "/", distro: str = "") -> dict[str, Any]:
    """
    Atajo: df -h para una ruta en WSL.

    Args:
        path: Ruta Linux o Windows (default /).
        distro: Distro WSL (vacío = default).
    """
    return ops.op_disk_usage(path=path, distro=distro)


def cli_list() -> int:
    print(json.dumps(get_settings().public_dict(), ensure_ascii=False, indent=2))
    print("env_files:", [str(p) for p in LOADED_ENV_FILES])
    return 0


def cli_test() -> int:
    print(f"Probando WSL distro={get_settings().distro}...")
    output = wsl_status()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output.get("status") == "running" and output.get("ok") is not False:
        print("OK: WSL respondió correctamente")
        return 0
    print("ERROR: WSL no respondió como se esperaba")
    return 1


async def _handshake() -> int:
    from mcp import Client

    async with Client(mcp) as client:
        tools_result = await client.list_tools()
        names = sorted(t.name for t in tools_result.tools)
        print(json.dumps({"ok": True, "tool_count": len(names), "tools": names}, indent=2))
        ping = await client.call_tool("list_distros", {})
        sc = ping.structured_content
        print("list_distros:", json.dumps(sc, ensure_ascii=False, indent=2, default=str)[:2000])
        return 0 if names else 1


def cli_handshake() -> int:
    print("Handshake MCP in-process...")
    return asyncio.run(_handshake())


def main() -> int:
    parser = argparse.ArgumentParser(description="WSL MCP Server")
    parser.add_argument("--list", action="store_true", help="Muestra configuración")
    parser.add_argument("--test", action="store_true", help="Prueba conexión WSL y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía SDK y sale")
    args = parser.parse_args()

    if args.list:
        return cli_list()
    if args.test:
        return cli_test()
    if args.handshake:
        return cli_handshake()

    s = get_settings()
    logger.info("Iniciando WSL MCP Server v%s", __version__)
    logger.info("  Distro:    %s", s.distro)
    logger.info("  User:      %s", s.user or "(default)")
    logger.info("  CWD:       %s", s.default_cwd or "(home)")
    logger.info("  Timeout:   %ss", s.command_timeout)
    logger.info("  Max output:%s chars", s.max_output)
    logger.info("  Env:       %s", LOADED_ENV_FILES)

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
