#!/usr/bin/env python3
"""
WSL MCP Server (MCP SDK v2)
===========================
Ejecuta comandos y operaciones de archivos en WSL desde Windows.

Requiere: mcp>=2.0, python-dotenv

Variables de entorno:
  WSL_DISTRO           - Distribución WSL (default: mynoble24.04)
  WSL_USER             - Usuario Linux opcional
  WSL_DEFAULT_CWD      - Directorio de trabajo inicial (default: ~)
  WSL_COMMAND_TIMEOUT  - Timeout de comandos en segundos (default: 120)
  WSL_MAX_OUTPUT       - Máximo de caracteres por stream stdout/stderr (default: 30000).
                         Se conserva cabeza + cola. Claude Code limita la salida de una
                         tool MCP a ~25k tokens, así que no conviene subirlo mucho.
  WSL_SHELL            - Shell a usar (default: /bin/bash)

El .env se lee de la carpeta de este script (no del cwd), así que el servidor
arranca igual desde cualquier directorio (Cursor, Claude Code, Claude Desktop).
Las variables ya definidas en el entorno del proceso tienen prioridad sobre el .env.

Uso:
  python wsl_mcp_server.py              # MCP server (stdio)
  python wsl_mcp_server.py --test       # probar conexión WSL
  python wsl_mcp_server.py --handshake  # listar tools vía SDK in-process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=False)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wsl-mcp")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class Config:
    distro: str = field(default_factory=lambda: os.environ.get("WSL_DISTRO", "mynoble24.04"))
    user: str = field(default_factory=lambda: os.environ.get("WSL_USER", ""))
    default_cwd: str = field(default_factory=lambda: os.environ.get("WSL_DEFAULT_CWD", ""))
    command_timeout: int = field(
        default_factory=lambda: int(os.environ.get("WSL_COMMAND_TIMEOUT", "120"))
    )
    max_output: int = field(default_factory=lambda: int(os.environ.get("WSL_MAX_OUTPUT", "30000")))
    shell: str = field(default_factory=lambda: os.environ.get("WSL_SHELL", "/bin/bash"))


CFG = Config()

# Anotaciones MCP: los clientes (Claude Code, Cursor) las usan para decidir
# cuándo pedir confirmación. Todo corre dentro de WSL (no es "open world").
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
ADDITIVE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _instructions() -> str:
    return (
        f"Ejecuta comandos y operaciones de archivos dentro de la distro WSL '{CFG.distro}' "
        f"(shell {CFG.shell}, usuario {CFG.user or 'por defecto'}, "
        f"cwd por defecto {CFG.default_cwd or '~'}). "
        "Las rutas Windows (C:\\...) se convierten a /mnt/c/...; '~' se expande al HOME de Linux. "
        "Para inspeccionar usa las tools de solo lectura (wsl_status, list_directory, read_file, "
        "tail_file, file_info, find_files, grep_text, which, list_processes, list_distros) antes que "
        "run_command. run_command/run_script ejecutan bash arbitrario y write_file sobrescribe "
        "archivos: úsalas solo cuando haga falta modificar algo. "
        f"Timeout por defecto {CFG.command_timeout}s (parámetro timeout para ampliarlo). "
        f"stdout/stderr se truncan a {CFG.max_output} caracteres cada uno (se conserva inicio y "
        "final); para salidas grandes filtra con grep/head/tail o usa read_file con offset/limit. "
        "Los comandos no tienen stdin (no son interactivos): usa flags como -y / --no-pager."
    )


mcp = MCPServer("WSL", version="2.1.0", instructions=_instructions())


def _err(msg: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **extra}


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    if "ok" not in payload:
        payload = {"ok": True, **payload}
    return payload


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Trunca conservando inicio y final (los errores suelen estar al final)."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * 2 // 5
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [truncado: {omitted} chars omitidos] ...\n{text[-tail:]}", True


def _decode_output(raw: Optional[bytes]) -> str:
    """Decodifica salida de wsl.exe: UTF-8 de Linux o UTF-16 de los mensajes del propio wsl.exe."""
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:64]:
        return _decode_wsl_list(raw).replace("\x00", "")
    return raw.decode("utf-8", errors="replace")


def _build_wsl_base_args() -> list[str]:
    args = ["wsl.exe", "-d", CFG.distro]
    if CFG.user:
        args.extend(["-u", CFG.user])
    return args


def _shell_path(path: str) -> str:
    """Cita una ruta Linux para bash dejando '~' / '~/...' expandibles a $HOME."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        rest = path[2:]
        return '"$HOME"/' + shlex.quote(rest) if rest else '"$HOME"/'
    return shlex.quote(path)


def _run_wsl_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    stdin_data: Optional[bytes] = None,
) -> dict[str, Any]:
    timeout = max(1, int(timeout or CFG.command_timeout))
    workdir = _linux_path(cwd) if cwd else (CFG.default_cwd or None)

    # Si el cd falla se aborta (antes se ocultaba el error y el comando no corría).
    shell_cmd = f"cd {_shell_path(workdir)} || exit 97\n{command}" if workdir else command
    # '-e' ejecuta sin pasar por el shell por defecto de WSL, así los argumentos llegan
    # literales (con '--' el comando se re-interpretaba: "$x", '$HOME', etc. se rompían).
    # 'timeout' en Linux mata el proceso real; el timeout de Windows es solo respaldo.
    full_cmd = _build_wsl_base_args() + [
        "-e", "timeout", "-k", "5", str(timeout), CFG.shell, "-lc", shell_cmd,
    ]
    shown_cmd = command if len(command) <= 500 else command[:500] + "... [recortado]"

    started = time.time()
    try:
        proc = subprocess.run(
            full_cmd,
            input=stdin_data,
            # Sin stdin heredado: el stdin de este proceso es el canal JSON-RPC del MCP.
            stdin=None if stdin_data is not None else subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout + 20,
            creationflags=CREATE_NO_WINDOW,
        )
        elapsed = time.time() - started
        stdout, stdout_trunc = _truncate(_decode_output(proc.stdout), CFG.max_output)
        stderr, stderr_trunc = _truncate(_decode_output(proc.stderr), CFG.max_output)

        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "execution_time_s": round(elapsed, 3),
            "distro": CFG.distro,
            "cwd": workdir,
            "command": shown_cmd,
        }
        if proc.returncode in (124, 137) and elapsed >= timeout:
            result["error"] = f"Timeout después de {timeout}s (proceso terminado en WSL)"
        elif proc.returncode == 97 and workdir and not stdout:
            result["error"] = f"No se pudo hacer cd a {workdir}"
        return result
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - started
        stdout, _ = _truncate(_decode_output(e.stdout), CFG.max_output)
        stderr, _ = _truncate(_decode_output(e.stderr), CFG.max_output)
        return {
            "success": False,
            "exit_code": -1,
            "stdout": stdout,
            "stderr": stderr,
            "error": f"Timeout después de {timeout}s",
            "execution_time_s": round(elapsed, 3),
            "distro": CFG.distro,
            "cwd": workdir,
            "command": shown_cmd,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "exit_code": -1,
            "error": "Comando 'wsl' no encontrado. ¿Está WSL instalado?",
            "distro": CFG.distro,
            "command": shown_cmd,
        }
    except Exception as e:
        logger.exception("Error ejecutando comando WSL")
        return {
            "success": False,
            "exit_code": -1,
            "error": str(e),
            "distro": CFG.distro,
            "command": shown_cmd,
        }


_TOTAL_MARK = "__WSL_MCP_TOTAL_LINES__="
_NL = "\n"


def _linux_path(path: str) -> str:
    value = path.strip()
    if not value:
        return value
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha() and (
        len(value) == 2 or value[2] in ("/", "\\")
    ):
        drive = value[0].lower()
        rest = value[3:].replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return value.replace("\\", "/")


def _decode_wsl_list(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace")
    if b"\x00" in raw[:64]:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


@mcp.tool(annotations=DESTRUCTIVE)
def run_command(command: str, cwd: str = "", timeout: int = 0) -> dict[str, Any]:
    """
    Ejecuta un comando shell en WSL y retorna stdout, stderr y exit code.

    Args:
        command: Comando bash a ejecutar (ej. 'ls -la', 'python3 --version').
        cwd: Directorio de trabajo Linux o Windows (opcional; '~' = HOME).
        timeout: Timeout en segundos (0 = usar WSL_COMMAND_TIMEOUT).
    """
    if not command.strip():
        return _err("Comando vacío")
    command = command.replace("\r\n", "\n")
    result = _run_wsl_command(command, cwd=cwd or None, timeout=timeout or None)
    return _ok(result)


@mcp.tool(annotations=DESTRUCTIVE)
def run_script(script: str, cwd: str = "", timeout: int = 0) -> dict[str, Any]:
    """
    Ejecuta un script bash multilínea en WSL.

    Args:
        script: Contenido del script bash.
        cwd: Directorio de trabajo (opcional).
        timeout: Timeout en segundos (0 = default).
    """
    if not script.strip():
        return _err("Script vacío")
    # CRLF de clientes Windows rompe bash ($'\r': command not found).
    script = script.replace("\r\n", "\n")
    result = _run_wsl_command(script, cwd=cwd or None, timeout=timeout or None)
    return _ok(result)


@mcp.tool(annotations=READ_ONLY)
def read_file(path: str, offset: int = 1, limit: int = 200) -> dict[str, Any]:
    """
    Lee un archivo del filesystem de WSL.

    Args:
        path: Ruta Linux (/home/...) o Windows (C:\\... convertida a /mnt/c/...).
        offset: Línea inicial (1-based, default: 1).
        limit: Máximo de líneas a retornar (default: 200, max: 2000).
    """
    linux_path = _linux_path(path)
    if not linux_path:
        return _err("Ruta vacía")
    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 2000))
    end = offset + limit - 1
    # El total va en una línea marcada aparte: si el archivo no termina en salto de
    # línea, antes el resultado de 'wc -l' se pegaba a la última línea del contenido.
    command = (
        f"p={_shell_path(linux_path)}; "
        'if [ ! -f "$p" ]; then echo "No existe o no es un archivo regular: $p" >&2; exit 1; fi; '
        f"sed -n '{offset},{end}p' \"$p\"; "
        f"printf '\\n{_TOTAL_MARK}%s\\n' \"$(awk 'END{{print NR}}' \"$p\")\""
    )
    result = _run_wsl_command(command)
    if not result.get("success"):
        return _ok({**result, "ok": False})

    stdout = result.get("stdout") or ""
    idx = stdout.rfind(_NL + _TOTAL_MARK)
    total: Optional[int] = None
    if idx >= 0:
        tail = stdout[idx + 1 + len(_TOTAL_MARK):].strip()
        total = int(tail) if tail.isdigit() else None
        stdout = stdout[:idx]
    content = stdout[:-1] if stdout.endswith(_NL) else stdout
    lines_returned = len(content.splitlines())
    if total is None:
        total = offset - 1 + lines_returned

    return _ok({
        "path": linux_path,
        "offset": offset,
        "limit": limit,
        "content": content,
        "lines_returned": lines_returned,
        "total_lines": total,
        "has_more": end < total,
        "truncated": bool(result.get("stdout_truncated")),
    })


@mcp.tool(annotations=DESTRUCTIVE)
def write_file(path: str, content: str, append: bool = False) -> dict[str, Any]:
    """
    Escribe contenido a un archivo en WSL.

    Args:
        path: Ruta del archivo en WSL.
        content: Texto a escribir.
        append: Si True, agrega al final del archivo.
    """
    linux_path = _linux_path(path)
    if not linux_path or linux_path.endswith("/"):
        return _err("Ruta de archivo vacía o inválida", path=path)
    op = ">>" if append else ">"
    # El contenido va por stdin (sin límite de longitud de la línea de comandos de Windows).
    # dirname evita que una ruta relativa ('foo.txt') cree un directorio 'foo.txt'.
    command = (
        f"p={_shell_path(linux_path)}; "
        'mkdir -p -- "$(dirname -- "$p")" && '
        f'cat {op} "$p" && wc -c < "$p"'
    )
    result = _run_wsl_command(command, stdin_data=content.encode("utf-8"))
    if not result.get("success"):
        return _ok({**result, "ok": False})

    bytes_written = (result.get("stdout") or "").strip().splitlines()
    size = int(bytes_written[-1]) if bytes_written and bytes_written[-1].isdigit() else None
    return _ok({
        "path": linux_path,
        "append": append,
        "bytes": size,
        "status": "written",
    })


@mcp.tool(annotations=READ_ONLY)
def list_directory(path: str = ".", show_hidden: bool = False) -> dict[str, Any]:
    """
    Lista el contenido de un directorio en WSL.

    Args:
        path: Ruta del directorio (default: directorio por defecto o ~).
        show_hidden: Incluir archivos ocultos (default: false).
    """
    linux_path = _linux_path(path) if path != "." else (CFG.default_cwd or ".")
    flags = "-la" if show_hidden else "-l"
    command = f"ls {flags} --time-style=long-iso {_shell_path(linux_path)} 2>&1"
    result = _run_wsl_command(command)
    if not result.get("success"):
        return _ok({**result, "ok": False})

    entries = []
    kinds = {"d": "dir", "l": "link", "-": "file"}
    for line in (result.get("stdout") or "").splitlines():
        if line.startswith("total "):
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        entries.append({
            "permissions": parts[0],
            "type": kinds.get(parts[0][:1], "other"),
            "size": parts[4],
            "modified": f"{parts[5]} {parts[6]}",
            "name": parts[7],
        })

    return _ok({
        "path": linux_path,
        "entry_count": len(entries),
        "entries": entries,
    })


@mcp.tool(annotations=READ_ONLY)
def file_info(path: str) -> dict[str, Any]:
    """
    Obtiene metadatos de un archivo o directorio en WSL.

    Args:
        path: Ruta en WSL.
    """
    linux_path = _linux_path(path)
    command = f"stat -c '%F|%s|%a|%U|%G|%y|%n' {_shell_path(linux_path)} 2>&1"
    result = _run_wsl_command(command)
    if not result.get("success"):
        return _ok({**result, "ok": False})

    line = (result.get("stdout") or "").strip().splitlines()[0]
    parts = line.split("|")
    if len(parts) < 7:
        return _ok({**result, "parse_error": True, "ok": False})

    return _ok({
        "path": parts[6],
        "file_type": parts[0],
        "size_bytes": int(parts[1]),
        "permissions": parts[2],
        "owner": parts[3],
        "group": parts[4],
        "modified": parts[5],
    })


@mcp.tool(annotations=READ_ONLY)
def find_files(path: str = ".", pattern: str = "*", max_depth: int = 5) -> dict[str, Any]:
    """
    Busca archivos por patrón glob en WSL.

    Args:
        path: Directorio raíz de búsqueda.
        pattern: Patrón glob (ej. '*.py', 'docker-compose*').
        max_depth: Profundidad máxima (default: 5, max: 10).
    """
    linux_path = _linux_path(path) if path != "." else (CFG.default_cwd or ".")
    max_depth = max(1, min(int(max_depth), 10))
    command = (
        f"find {_shell_path(linux_path)} -mindepth 1 -maxdepth {max_depth} "
        f"-name '.*' -prune -o -name {shlex.quote(pattern)} -print 2>/dev/null | head -200"
    )
    result = _run_wsl_command(command)
    paths = [p for p in (result.get("stdout") or "").splitlines() if p.strip()]
    return _ok({
        "root": linux_path,
        "pattern": pattern,
        "max_depth": max_depth,
        "count": len(paths),
        "paths": paths,
        "truncated": len(paths) >= 200,
        "success": result.get("success", False),
    })


@mcp.tool(annotations=READ_ONLY)
def which(command_name: str) -> dict[str, Any]:
    """
    Localiza un ejecutable en el PATH de WSL.

    Args:
        command_name: Nombre del comando (ej. python3, git, docker).
    """
    if not command_name.strip():
        return _err("Nombre de comando vacío")
    result = _run_wsl_command(f"command -v {shlex.quote(command_name.strip())}")
    path = (result.get("stdout") or "").strip()
    if result.get("success") and path:
        return _ok({"command": command_name, "path": path, "found": True})
    return _ok({"command": command_name, "found": False, "stderr": result.get("stderr", "")})


@mcp.tool(annotations=READ_ONLY)
def grep_text(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    max_results: int = 80,
) -> dict[str, Any]:
    """
    Busca texto con grep -n en WSL (máx. 80 coincidencias).

    Args:
        pattern: Expresión regular de grep.
        path: Directorio o archivo raíz.
        glob: Filtro --include (ej. '*.py').
        max_results: Límite de líneas (default 80, max 200).
    """
    if not pattern.strip():
        return _err("Patrón vacío")
    linux_path = _linux_path(path) if path != "." else (CFG.default_cwd or ".")
    limit = max(1, min(int(max_results), 200))
    include = f"--include={shlex.quote(glob)}" if glob and glob != "*" else ""
    command = (
        f"grep -RIn --binary-files=without-match {include} "
        f"-e {shlex.quote(pattern)} {_shell_path(linux_path)} 2>/dev/null | head -n {limit}"
    )
    result = _run_wsl_command(command)
    matches = [ln for ln in (result.get("stdout") or "").splitlines() if ln.strip()]
    return _ok({
        "pattern": pattern,
        "path": linux_path,
        "count": len(matches),
        "matches": matches,
        "truncated": len(matches) >= limit,
    })


@mcp.tool(annotations=READ_ONLY)
def tail_file(path: str, lines: int = 50) -> dict[str, Any]:
    """
    Lee las últimas N líneas de un archivo en WSL.

    Args:
        path: Ruta del archivo.
        lines: Número de líneas (default 50, max 2000).
    """
    linux_path = _linux_path(path)
    n = max(1, min(int(lines), 2000))
    result = _run_wsl_command(f"tail -n {n} {_shell_path(linux_path)}")
    if not result.get("success"):
        return _ok({**result, "ok": False})
    content = result.get("stdout") or ""
    return _ok({
        "path": linux_path,
        "lines_requested": n,
        "lines_returned": len(content.splitlines()),
        "content": content,
    })


@mcp.tool(annotations=ADDITIVE_IDEMPOTENT)
def make_directory(path: str) -> dict[str, Any]:
    """
    Crea un directorio en WSL (mkdir -p).

    Args:
        path: Ruta Linux o Windows.
    """
    linux_path = _linux_path(path)
    if not linux_path:
        return _err("Ruta vacía")
    result = _run_wsl_command(f"mkdir -p {_shell_path(linux_path)} && ls -ld {_shell_path(linux_path)}")
    if not result.get("success"):
        return _ok({**result, "ok": False})
    return _ok({"path": linux_path, "status": "created", "ls": (result.get("stdout") or "").strip()})


@mcp.tool(annotations=READ_ONLY)
def list_processes(filter: str = "", limit: int = 40) -> dict[str, Any]:
    """
    Lista procesos en WSL (ps aux).

    Args:
        filter: Substring opcional para filtrar COMMAND/USER.
        limit: Máximo de filas (default 40, max 200).
    """
    limit = max(1, min(int(limit), 200))
    if filter.strip():
        command = (
            f"ps aux | head -n 1; ps aux | grep -i -- {shlex.quote(filter.strip())} "
            f"| grep -v grep | head -n {limit}"
        )
    else:
        command = f"ps aux --sort=-%cpu | head -n {limit + 1}"
    result = _run_wsl_command(command)
    if not result.get("success"):
        return _ok({**result, "ok": False})
    lines = [ln for ln in (result.get("stdout") or "").splitlines() if ln.strip()]
    return _ok({"count": max(0, len(lines) - 1), "filter": filter or None, "output": "\n".join(lines)})


@mcp.tool(annotations=READ_ONLY)
def wsl_status() -> dict[str, Any]:
    """
    Retorna estado de WSL: distro, usuario, kernel, uptime y recursos.
    """
    script = r"""
echo "===DISTRO==="
cat /etc/os-release 2>/dev/null | head -5
echo "===USER==="
whoami
echo "===HOME==="
echo $HOME
echo "===PWD==="
pwd
echo "===KERNEL==="
uname -a
echo "===UPTIME==="
uptime -p 2>/dev/null || uptime
echo "===DISK==="
df -h / 2>/dev/null | tail -1
echo "===MEM==="
free -h 2>/dev/null | sed -n '2p'
"""
    result = _run_wsl_command(script.strip())
    if not result.get("success"):
        return _ok({
            **result,
            "ok": False,
            "distro_config": CFG.distro,
            "status": "error",
        })

    stdout = result.get("stdout") or ""
    sections: dict[str, str] = {}
    current = "_"
    lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("===") and line.endswith("==="):
            if lines:
                sections[current] = "\n".join(lines).strip()
            current = line.strip("=")
            lines = []
        else:
            lines.append(line)
    if lines:
        sections[current] = "\n".join(lines).strip()

    return _ok({
        "status": "running",
        "distro_config": CFG.distro,
        "shell": CFG.shell,
        "default_cwd": CFG.default_cwd or None,
        "command_timeout": CFG.command_timeout,
        "os_release": sections.get("DISTRO", ""),
        "user": sections.get("USER", ""),
        "home": sections.get("HOME", ""),
        "pwd": sections.get("PWD", ""),
        "kernel": sections.get("KERNEL", ""),
        "uptime": sections.get("UPTIME", ""),
        "disk": sections.get("DISK", ""),
        "memory": sections.get("MEM", ""),
        "execution_time_s": result.get("execution_time_s"),
    })


@mcp.tool(annotations=READ_ONLY)
def list_distros() -> dict[str, Any]:
    """
    Lista todas las distribuciones WSL instaladas en Windows.
    """
    try:
        proc = subprocess.run(
            ["wsl.exe", "--list", "--verbose"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = _decode_wsl_list(proc.stdout or proc.stderr or b"")
        raw = raw.replace("\x00", "")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        distros = []
        for line in lines[1:]:
            default = line.startswith("*")
            parts = line.lstrip("*").split()
            if len(parts) >= 3:
                distros.append({
                    "name": parts[0],
                    "state": parts[1],
                    "wsl_version": parts[2],
                    "default": default,
                })
            elif len(parts) == 2:
                distros.append({
                    "name": parts[0],
                    "state": parts[1],
                    "wsl_version": None,
                    "default": default,
                })
        return _ok({
            "count": len(distros),
            "distros": distros,
            "configured_distro": CFG.distro,
        })
    except Exception as e:
        logger.exception("Error listando distros WSL")
        return _err(str(e))


def cli_test() -> int:
    print(f"Probando WSL distro={CFG.distro}...")
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
        if not names:
            return 1
        return 0


def cli_handshake() -> int:
    print("Handshake MCP in-process...")
    return asyncio.run(_handshake())


def main() -> int:
    parser = argparse.ArgumentParser(description="WSL MCP Server")
    parser.add_argument("--test", action="store_true", help="Prueba conexión WSL y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía SDK y sale")
    args = parser.parse_args()

    if args.test:
        return cli_test()
    if args.handshake:
        return cli_handshake()

    logger.info("Iniciando WSL MCP Server")
    logger.info(f"  Distro:    {CFG.distro}")
    logger.info(f"  User:      {CFG.user or '(default)'}")
    logger.info(f"  CWD:       {CFG.default_cwd or '(home)'}")
    logger.info(f"  Timeout:   {CFG.command_timeout}s")
    logger.info(f"  Max output:{CFG.max_output} chars")

    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
