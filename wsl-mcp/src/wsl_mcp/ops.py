"""Operaciones WSL: ejecución de comandos y filesystem."""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from typing import Any, Optional

from wsl_mcp.config import MAX_COMMAND_TIMEOUT, get_settings
from wsl_mcp.util import (
    CREATE_NO_WINDOW,
    clamp_timeout,
    decode_output,
    decode_wsl_list,
    err,
    linux_path,
    ok,
    shell_path,
    truncate,
)

logger = logging.getLogger("wsl-mcp")

_TOTAL_MARK = "__WSL_MCP_TOTAL_LINES__="
_NL = "\n"


def build_shell_command(command: str, cwd: Optional[str] = None) -> str:
    settings = get_settings()
    workdir = linux_path(cwd) if cwd else (settings.default_cwd or None)
    if workdir:
        return f"cd {shell_path(workdir)} || exit 97\n{command}"
    return command


def _build_wsl_base_args(distro: str) -> list[str]:
    settings = get_settings()
    args = ["wsl.exe", "-d", distro]
    if settings.user:
        args.extend(["-u", settings.user])
    return args


def run_wsl_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    stdin_data: Optional[bytes] = None,
    distro: Optional[str] = None,
) -> dict[str, Any]:
    settings = get_settings()
    use_distro = settings.resolve_distro(distro)
    timeout_s = clamp_timeout(timeout, settings.command_timeout, MAX_COMMAND_TIMEOUT)
    workdir = linux_path(cwd) if cwd else (settings.default_cwd or None)
    shell_cmd = build_shell_command(command, cwd)
    full_cmd = _build_wsl_base_args(use_distro) + [
        "-e",
        "timeout",
        "-k",
        "5",
        str(timeout_s),
        settings.shell,
        "-lc",
        shell_cmd,
    ]
    shown_cmd = command if len(command) <= 500 else command[:500] + "... [recortado]"
    started = time.time()
    try:
        proc = subprocess.run(
            full_cmd,
            input=stdin_data,
            stdin=None if stdin_data is not None else subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s + 20,
            creationflags=CREATE_NO_WINDOW,
        )
        elapsed = time.time() - started
        stdout, stdout_trunc = truncate(decode_output(proc.stdout), settings.max_output)
        stderr, stderr_trunc = truncate(decode_output(proc.stderr), settings.max_output)
        result: dict[str, Any] = {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_trunc,
            "stderr_truncated": stderr_trunc,
            "execution_time_s": round(elapsed, 3),
            "distro": use_distro,
            "cwd": workdir,
            "command": shown_cmd,
        }
        if proc.returncode in (124, 137) and elapsed >= timeout_s:
            result["error"] = f"Timeout después de {timeout_s}s (proceso terminado en WSL)"
        elif proc.returncode == 97 and workdir and not stdout:
            result["error"] = f"No se pudo hacer cd a {workdir}"
        elif proc.returncode == 127 and "not found" in stderr.lower():
            result["error"] = stderr.strip() or "Comando no encontrado en la distro"
        return result
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - started
        stdout, _ = truncate(decode_output(e.stdout), settings.max_output)
        stderr, _ = truncate(decode_output(e.stderr), settings.max_output)
        return {
            "success": False,
            "exit_code": -1,
            "stdout": stdout,
            "stderr": stderr,
            "error": f"Timeout después de {timeout_s}s",
            "execution_time_s": round(elapsed, 3),
            "distro": use_distro,
            "cwd": workdir,
            "command": shown_cmd,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "exit_code": -1,
            "error": "Comando 'wsl' no encontrado. ¿Está WSL instalado?",
            "distro": use_distro,
            "command": shown_cmd,
        }
    except Exception as e:
        logger.exception("Error ejecutando comando WSL")
        return {
            "success": False,
            "exit_code": -1,
            "error": str(e),
            "distro": use_distro,
            "command": shown_cmd,
        }


def op_run_command(
    command: str,
    cwd: str = "",
    timeout: int = 0,
    stdin: str = "",
    distro: str = "",
) -> dict[str, Any]:
    if not command.strip():
        return err("Comando vacío")
    command = command.replace("\r\n", "\n")
    stdin_data = stdin.encode("utf-8") if stdin else None
    result = run_wsl_command(
        command,
        cwd=cwd or None,
        timeout=timeout or None,
        stdin_data=stdin_data,
        distro=distro or None,
    )
    return ok(result)


def op_run_script(
    script: str,
    cwd: str = "",
    timeout: int = 0,
    distro: str = "",
) -> dict[str, Any]:
    if not script.strip():
        return err("Script vacío")
    script = script.replace("\r\n", "\n")
    result = run_wsl_command(
        script,
        cwd=cwd or None,
        timeout=timeout or None,
        distro=distro or None,
    )
    return ok(result)


def op_read_file(
    path: str,
    offset: int = 1,
    limit: int = 200,
    offset_bytes: int = 0,
    max_bytes: int = 0,
    distro: str = "",
) -> dict[str, Any]:
    linux_p = linux_path(path)
    if not linux_p:
        return err("Ruta vacía")
    if max_bytes > 0 or offset_bytes > 0:
        max_b = max(1, min(int(max_bytes or 65536), 2_000_000))
        off_b = max(0, int(offset_bytes))
        command = (
            f"p={shell_path(linux_p)}; "
            'if [ ! -f "$p" ]; then echo "No existe o no es un archivo regular: $p" >&2; exit 1; fi; '
            f"sz=$(wc -c < \"$p\"); "
            f"tail -c +$(({off_b}+1)) \"$p\" | head -c {max_b}; "
            f"printf '\\n{_TOTAL_MARK}%s\\n' \"$sz\""
        )
        result = run_wsl_command(command, distro=distro or None)
        if not result.get("success"):
            return ok({**result, "ok": False})
        stdout = result.get("stdout") or ""
        idx = stdout.rfind(_NL + _TOTAL_MARK)
        total_size: Optional[int] = None
        if idx >= 0:
            tail = stdout[idx + 1 + len(_TOTAL_MARK) :].strip()
            total_size = int(tail) if tail.isdigit() else None
            stdout = stdout[:idx]
        content_bytes = stdout.encode("utf-8", errors="surrogateescape")
        return ok({
            "path": linux_p,
            "mode": "bytes",
            "offset_bytes": off_b,
            "max_bytes": max_b,
            "bytes_returned": len(content_bytes),
            "total_bytes": total_size,
            "has_more": total_size is not None and off_b + len(content_bytes) < total_size,
            "content": stdout,
            "truncated": bool(result.get("stdout_truncated")),
        })

    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 2000))
    end = offset + limit - 1
    command = (
        f"p={shell_path(linux_p)}; "
        'if [ ! -f "$p" ]; then echo "No existe o no es un archivo regular: $p" >&2; exit 1; fi; '
        f"sed -n '{offset},{end}p' \"$p\"; "
        f"printf '\\n{_TOTAL_MARK}%s\\n' \"$(awk 'END{{print NR}}' \"$p\")\""
    )
    result = run_wsl_command(command, distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    stdout = result.get("stdout") or ""
    idx = stdout.rfind(_NL + _TOTAL_MARK)
    total: Optional[int] = None
    if idx >= 0:
        tail = stdout[idx + 1 + len(_TOTAL_MARK) :].strip()
        total = int(tail) if tail.isdigit() else None
        stdout = stdout[:idx]
    content = stdout[:-1] if stdout.endswith(_NL) else stdout
    lines_returned = len(content.splitlines())
    if total is None:
        total = offset - 1 + lines_returned
    return ok({
        "path": linux_p,
        "mode": "lines",
        "offset": offset,
        "limit": limit,
        "content": content,
        "lines_returned": lines_returned,
        "total_lines": total,
        "has_more": end < total,
        "truncated": bool(result.get("stdout_truncated")),
    })


def op_write_file(path: str, content: str, append: bool = False, distro: str = "") -> dict[str, Any]:
    linux_p = linux_path(path)
    if not linux_p or linux_p.endswith("/"):
        return err("Ruta de archivo vacía o inválida", path=path)
    op = ">>" if append else ">"
    command = (
        f"p={shell_path(linux_p)}; "
        'mkdir -p -- "$(dirname -- "$p")" && '
        f'cat {op} "$p" && wc -c < "$p"'
    )
    result = run_wsl_command(command, stdin_data=content.encode("utf-8"), distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    bytes_written = (result.get("stdout") or "").strip().splitlines()
    size = int(bytes_written[-1]) if bytes_written and bytes_written[-1].isdigit() else None
    return ok({"path": linux_p, "append": append, "bytes": size, "status": "written"})


def op_list_directory(path: str = ".", show_hidden: bool = False, distro: str = "") -> dict[str, Any]:
    settings = get_settings()
    linux_p = linux_path(path) if path != "." else (settings.default_cwd or ".")
    flags = "-la" if show_hidden else "-l"
    command = f"ls {flags} --time-style=long-iso {shell_path(linux_p)} 2>&1"
    result = run_wsl_command(command, distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
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
    return ok({"path": linux_p, "entry_count": len(entries), "entries": entries})


def op_file_info(path: str, distro: str = "") -> dict[str, Any]:
    linux_p = linux_path(path)
    command = f"stat -c '%F|%s|%a|%U|%G|%y|%n' {shell_path(linux_p)} 2>&1"
    result = run_wsl_command(command, distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    line = (result.get("stdout") or "").strip().splitlines()[0]
    parts = line.split("|")
    if len(parts) < 7:
        return ok({**result, "parse_error": True, "ok": False})
    return ok({
        "path": parts[6],
        "file_type": parts[0],
        "size_bytes": int(parts[1]),
        "permissions": parts[2],
        "owner": parts[3],
        "group": parts[4],
        "modified": parts[5],
    })


def op_find_files(
    path: str = ".",
    pattern: str = "*",
    max_depth: int = 5,
    distro: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    linux_p = linux_path(path) if path != "." else (settings.default_cwd or ".")
    max_depth = max(1, min(int(max_depth), 10))
    command = (
        f"find {shell_path(linux_p)} -mindepth 1 -maxdepth {max_depth} "
        f"-name '.*' -prune -o -name {shlex.quote(pattern)} -print 2>/dev/null | head -200"
    )
    result = run_wsl_command(command, distro=distro or None)
    paths = [p for p in (result.get("stdout") or "").splitlines() if p.strip()]
    return ok({
        "root": linux_p,
        "pattern": pattern,
        "max_depth": max_depth,
        "count": len(paths),
        "paths": paths,
        "truncated": len(paths) >= 200,
        "success": result.get("success", False),
    })


def op_which(command_name: str, distro: str = "") -> dict[str, Any]:
    if not command_name.strip():
        return err("Nombre de comando vacío")
    result = run_wsl_command(f"command -v {shlex.quote(command_name.strip())}", distro=distro or None)
    path = (result.get("stdout") or "").strip()
    if result.get("success") and path:
        return ok({"command": command_name, "path": path, "found": True})
    return ok({"command": command_name, "found": False, "stderr": result.get("stderr", "")})


def op_grep_text(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    max_results: int = 80,
    distro: str = "",
) -> dict[str, Any]:
    if not pattern.strip():
        return err("Patrón vacío")
    settings = get_settings()
    linux_p = linux_path(path) if path != "." else (settings.default_cwd or ".")
    limit = max(1, min(int(max_results), 200))
    include = f"--include={shlex.quote(glob)}" if glob and glob != "*" else ""
    command = (
        f"grep -RIn --binary-files=without-match {include} "
        f"-e {shlex.quote(pattern)} {shell_path(linux_p)} 2>/dev/null | head -n {limit}"
    )
    result = run_wsl_command(command, distro=distro or None)
    matches = [ln for ln in (result.get("stdout") or "").splitlines() if ln.strip()]
    return ok({
        "pattern": pattern,
        "path": linux_p,
        "count": len(matches),
        "matches": matches,
        "truncated": len(matches) >= limit,
    })


def op_tail_file(path: str, lines: int = 50, distro: str = "") -> dict[str, Any]:
    linux_p = linux_path(path)
    n = max(1, min(int(lines), 2000))
    result = run_wsl_command(f"tail -n {n} {shell_path(linux_p)}", distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    content = result.get("stdout") or ""
    return ok({
        "path": linux_p,
        "lines_requested": n,
        "lines_returned": len(content.splitlines()),
        "content": content,
    })


def op_make_directory(path: str, distro: str = "") -> dict[str, Any]:
    linux_p = linux_path(path)
    if not linux_p:
        return err("Ruta vacía")
    result = run_wsl_command(
        f"mkdir -p {shell_path(linux_p)} && ls -ld {shell_path(linux_p)}",
        distro=distro or None,
    )
    if not result.get("success"):
        return ok({**result, "ok": False})
    return ok({"path": linux_p, "status": "created", "ls": (result.get("stdout") or "").strip()})


def op_list_processes(filter: str = "", limit: int = 40, distro: str = "") -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    if filter.strip():
        command = (
            f"ps aux | head -n 1; ps aux | grep -i -- {shlex.quote(filter.strip())} "
            f"| grep -v grep | head -n {limit}"
        )
    else:
        command = f"ps aux --sort=-%cpu | head -n {limit + 1}"
    result = run_wsl_command(command, distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    lines = [ln for ln in (result.get("stdout") or "").splitlines() if ln.strip()]
    return ok({"count": max(0, len(lines) - 1), "filter": filter or None, "output": "\n".join(lines)})


def op_wsl_status(distro: str = "") -> dict[str, Any]:
    settings = get_settings()
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
    result = run_wsl_command(script.strip(), distro=distro or None)
    if not result.get("success"):
        return ok({
            **result,
            "ok": False,
            "distro_config": settings.resolve_distro(distro or None),
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
    return ok({
        "status": "running",
        "distro_config": settings.resolve_distro(distro or None),
        "shell": settings.shell,
        "default_cwd": settings.default_cwd or None,
        "command_timeout": settings.command_timeout,
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


def op_list_distros() -> dict[str, Any]:
    settings = get_settings()
    try:
        proc = subprocess.run(
            ["wsl.exe", "--list", "--verbose"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        raw = decode_wsl_list(proc.stdout or proc.stderr or b"")
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
        return ok({
            "count": len(distros),
            "distros": distros,
            "configured_distro": settings.distro,
        })
    except Exception as e:
        logger.exception("Error listando distros WSL")
        return err(str(e))


def op_copy_between(src: str, dst: str, distro: str = "") -> dict[str, Any]:
    src_p = linux_path(src)
    dst_p = linux_path(dst)
    if not src_p or not dst_p:
        return err("Rutas src y dst requeridas")
    command = (
        f"s={shell_path(src_p)}; d={shell_path(dst_p)}; "
        'if [ ! -e "$s" ]; then echo "Origen no existe: $s" >&2; exit 1; fi; '
        'if [ -d "$s" ] && [ -e "$d" ] && [ ! -d "$d" ]; then echo "Destino no es directorio" >&2; exit 2; fi; '
        'parent=$(dirname -- "$d"); mkdir -p -- "$parent"; '
        'cp -a -- "$s" "$d"'
    )
    result = run_wsl_command(command, distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    return ok({"src": src_p, "dst": dst_p, "status": "copied"})


def _require_cmd(name: str, run_line: str, distro: Optional[str]) -> dict[str, Any]:
    wrapper = (
        f"if ! command -v {shlex.quote(name)} >/dev/null 2>&1; then "
        f'echo "{name} no está instalado o no está en PATH en esta distro" >&2; exit 127; fi; '
        f"{run_line}"
    )
    result = run_wsl_command(wrapper, distro=distro)
    if result.get("exit_code") == 127:
        result["error"] = result.get("stderr", "").strip() or f"{name} no disponible"
        result["success"] = False
    return result


def op_docker_ps(all_containers: bool = False, distro: str = "") -> dict[str, Any]:
    flag = "-a" if all_containers else ""
    result = _require_cmd("docker", f"docker ps {flag}".strip(), distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    return ok({"output": result.get("stdout", ""), "stderr": result.get("stderr", "")})


def op_systemctl_status(unit: Optional[str] = None, distro: str = "") -> dict[str, Any]:
    if unit and unit.strip():
        run = f"systemctl status {shlex.quote(unit.strip())} --no-pager"
    else:
        run = "systemctl --failed --no-pager 2>/dev/null; systemctl list-units --type=service --state=running --no-pager | head -40"
    result = _require_cmd("systemctl", run, distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    return ok({"unit": unit, "output": result.get("stdout", ""), "stderr": result.get("stderr", "")})


def op_disk_usage(path: str = "/", distro: str = "") -> dict[str, Any]:
    linux_p = linux_path(path) if path else "/"
    result = run_wsl_command(f"df -h {shell_path(linux_p)} 2>&1", distro=distro or None)
    if not result.get("success"):
        return ok({**result, "ok": False})
    return ok({"path": linux_p, "output": result.get("stdout", "")})
