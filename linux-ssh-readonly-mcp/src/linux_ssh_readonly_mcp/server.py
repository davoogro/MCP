"""MCP de SOLO LECTURA para servidores Linux vía SSH (paramiko) a través de un proxy SOCKS5.

Uso:
  python -m linux_ssh_readonly_mcp               # MCP server (stdio)
  python -m linux_ssh_readonly_mcp --test [host] # probar conexión
  python -m linux_ssh_readonly_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import functools
import json
import logging
import re
import shlex
import stat
import sys
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

import paramiko
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from linux_ssh_readonly_mcp import __version__
from linux_ssh_readonly_mcp.config import MAX_CHARS, STATE_DIR, TRANSFER_DIR, Host, Settings
from linux_ssh_readonly_mcp.readonly import ReadOnlyViolation, describe_policy, validate
from linux_ssh_readonly_mcp.transport import Connection, RemoteError, Result

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("linux-ssh-readonly-mcp")

SETTINGS = Settings.load()
CONNS: dict[str, Connection] = {}
_CONNS_LOCK = threading.Lock()
AUDIT_LOG = STATE_DIR / "audit.jsonl"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024

ANN = {
    "remote": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    "local": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
}

mcp = MCPServer(
    "linux-ssh-readonly",
    title="Linux vía SSH (solo lectura)",
    version=__version__,
    instructions=(
        "Acceso de SOLO LECTURA a servidores Linux por SSH (paramiko), con una conexión persistente por host. Hosts: "
        + "; ".join(f"'{h.id}' = {h.description or h.hostname}" for h in SETTINGS.hosts.values())
        + f". Host por defecto: '{SETTINGS.default}' (parámetro opcional 'host'). La conexión pasa por el proxy "
        "SOCKS5 local 127.0.0.1:18080, que debe estar levantado: si test_connection falla con conexión rechazada "
        "al proxy, pide al usuario que levante el proxy/túnel SOCKS y reintenta. Flujo recomendado: "
        "test_connection -> get_system_info -> tools específicas (list_directory, read_file, tail_file, "
        "search_files, grep_text, disk_usage, get_processes, get_listening_ports, get_services, service_status, "
        "get_journal, download_file) -> run_command solo si ninguna tool sirve. El usuario remoto no tiene sudo "
        "útil: archivos/logs de root pueden dar 'Permission denied' y journalctl puede salir vacío sin el grupo "
        "systemd-journal. Política de run_command: " + describe_policy() + " Todo queda en query_audit_log."
    ),
)


# ============================================================
# Infra
# ============================================================


def _conn(host: Optional[str]) -> tuple[Host, Connection]:
    h = SETTINGS.get(host)
    with _CONNS_LOCK:
        if h.id not in CONNS:
            CONNS[h.id] = Connection(h)
        return h, CONNS[h.id]


def _clip(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} caracteres omitidos] ...\n{text[-half:]}", True


def _fit(data: list) -> tuple[list, bool]:
    """Recorta listas grandes para que la respuesta no exceda MAX_CHARS."""
    if len(json.dumps(data, default=str)) <= MAX_CHARS:
        return data, False
    lo, hi = 0, len(data)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(json.dumps(data[:mid], default=str)) <= MAX_CHARS:
            lo = mid
        else:
            hi = mid - 1
    return data[:lo], True


def _run(c: Connection, command: str, timeout_s: float = 60) -> Result:
    """Ejecuta un comando construido internamente, validado igualmente (defensa en profundidad)."""
    validate(command)
    r = c.run(command, timeout_s)
    if r.timed_out:
        raise TimeoutError(f"Timeout de {timeout_s:.0f}s: se cerró el canal")
    return r


def _qpath(path: str) -> str:
    """Ruta remota entrecomillada para el shell; '~' y '~/x' se expanden vía $HOME."""
    path = (path or "~").strip()
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:]) if path[2:] else '"$HOME"/'
    if path.startswith("~"):
        raise ValueError("'~usuario' no está soportado: usa una ruta absoluta")
    return shlex.quote(path)


def _sftp_path(sftp: paramiko.SFTPClient, path: str) -> str:
    path = (path or "~").strip()
    if path == "~" or path.startswith("~/"):
        home = sftp.normalize(".")
        return home if path == "~" else home.rstrip("/") + path[1:]
    if path.startswith("~"):
        raise ValueError("'~usuario' no está soportado: usa una ruta absoluta")
    return path


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts is not None else None


def _attr(a: paramiko.SFTPAttributes, name: Optional[str] = None) -> dict[str, Any]:
    mode = a.st_mode or 0
    kind = ("dir" if stat.S_ISDIR(mode) else "link" if stat.S_ISLNK(mode) else
            "file" if stat.S_ISREG(mode) else "other")
    return {"name": name or a.filename, "type": kind, "size": a.st_size, "permissions": stat.filemode(mode),
            "mtime": _iso(a.st_mtime), "uid": a.st_uid, "gid": a.st_gid}


def _audit(tool: str, args: dict[str, Any], ok: bool, err: Optional[str], dur: float) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        safe = {}
        for k, v in args.items():
            s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
            if isinstance(s, str) and len(s) > 400:
                s = s[:400] + f"...(+{len(s) - 400})"
            safe[k] = s
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "tool": tool, "args": safe,
               "ok": ok, "error": err, "duration_s": round(dur, 2)}
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("No se pudo escribir el audit log")


_EXPECTED_ERRORS = (RemoteError, TimeoutError, ValueError, OSError, EOFError, paramiko.SSHException)


def tool(kind: Literal["remote", "local"] = "remote"):
    """Registra una tool síncrona (bloqueante) que se ejecuta en un hilo con asyncio.to_thread."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(**kwargs):
            t0 = time.monotonic()
            ok, err = True, None
            try:
                res = await asyncio.to_thread(fn, **kwargs)
                if isinstance(res, dict) and res.get("ok") is False:
                    ok, err = False, str(res.get("error") or "")[:300]
                return res
            except ReadOnlyViolation as exc:
                ok, err = False, f"Comando rechazado por la política de solo lectura: {exc}"
                return {"ok": False, "error": err}
            except Exception as exc:  # noqa: BLE001 - todo error vuelve como {"ok": false}
                if not isinstance(exc, _EXPECTED_ERRORS):
                    logger.exception("Error inesperado en %s", fn.__name__)
                ok, err = False, f"{type(exc).__name__}: {exc}"
                return {"ok": False, "error": err}
            finally:
                _audit(fn.__name__, kwargs, ok, err, time.monotonic() - t0)
        return mcp.tool(annotations=ANN[kind])(wrapper)
    return deco


# ============================================================
# Conexión y ejecución
# ============================================================


@tool("local")
def list_hosts() -> dict[str, Any]:
    """Hosts configurados (config/hosts.json): dirección, usuario, proxy, descripción y si hay conexión abierta. Sin secretos."""
    return {"ok": True, "default": SETTINGS.default, "hosts": [
        {**h.public(), "connected": h.id in CONNS and CONNS[h.id].is_active()} for h in SETTINGS.hosts.values()]}


@tool()
def test_connection(host: Optional[str] = None) -> dict[str, Any]:
    """Conecta (o reutiliza la conexión) y devuelve banner/versión SSH, huella de la host key, hostname, usuario, uname y latencias."""
    h, c = _conn(host)
    reused = c.is_active()
    t0 = time.monotonic()
    c.ensure()
    connect_s = time.monotonic() - t0
    t1 = time.monotonic()
    r = _run(c, "hostname; id -un; uname -a", 30)
    latency_ms = round((time.monotonic() - t1) * 1000)
    lines = r.stdout.splitlines() + ["", "", ""]
    out: dict[str, Any] = {"ok": r.exit_code == 0, "host": h.id, "target": f"{h.hostname}:{h.port}",
                           "proxy": h.proxy, **c.info(), "hostname": lines[0], "user": lines[1], "uname": lines[2],
                           "reused_connection": reused, "connect_s": None if reused else round(connect_s, 2),
                           "command_latency_ms": latency_ms}
    if r.stderr.strip():
        out["stderr"] = _clip(r.stderr.strip(), 4000)[0]
    return out


@tool()
def run_command(command: str, host: Optional[str] = None, timeout_s: int = 60,
                max_chars: Optional[int] = None) -> dict[str, Any]:
    """
    Ejecuta una línea de shell de SOLO LECTURA en el servidor (bash, LC_ALL=C, sin stdin). Se valida antes
    contra la política de solo lectura: solo comandos de diagnóstico de la allowlist encadenados con
    | && || ; (sin &, sin redirecciones salvo 2>/dev/null, 2>&1, >/dev/null, &>/dev/null, sin $(...),
    backticks ni asignaciones). Si viola la política devuelve el motivo y no se ejecuta nada.
    El usuario remoto no tiene sudo útil: no uses sudo/su (están prohibidos) y espera 'Permission denied'
    en archivos de root. timeout_s máx. 600 (al vencer se cierra el canal). max_chars recorta stdout.
    Úsala solo cuando no exista una tool específica.
    """
    validate(command)
    h, c = _conn(host)
    timeout = min(max(int(timeout_s), 1), 600)
    limit = min(max(int(max_chars), 200), MAX_CHARS) if max_chars else MAX_CHARS
    r = c.run(command, timeout)
    stdout, t1 = _clip(r.stdout, limit)
    stderr, t2 = _clip(r.stderr, min(limit, 20000))
    out: dict[str, Any] = {"ok": r.exit_code == 0, "host": h.id, "exit_code": r.exit_code, "stdout": stdout,
                           "stderr": stderr, "duration_s": round(r.duration_s, 2),
                           "truncated": t1 or t2 or r.output_capped}
    if r.timed_out:
        out["error"] = f"Timeout de {timeout}s: se cerró el canal (salida parcial)."
    elif r.output_capped:
        out["error"] = "Salida > 20 MB: se cerró el canal (salida parcial). Filtra con grep/head."
    return out


# ============================================================
# Sistema
# ============================================================

_SYSINFO_CMD = (
    "echo @@hostname; hostname; echo @@fqdn; hostname -f 2>/dev/null; echo @@os; cat /etc/os-release 2>/dev/null; "
    "echo @@redhat; cat /etc/redhat-release 2>/dev/null; echo @@kernel; uname -srm; echo @@uptime; "
    "cat /proc/uptime; echo @@loadavg; cat /proc/loadavg; echo @@boot; uptime -s 2>/dev/null; echo @@nproc; nproc; "
    "echo @@lscpu; lscpu 2>/dev/null; echo @@free; free -m; echo @@df; df -hP 2>/dev/null; "
    "echo @@selinux; getenforce 2>/dev/null; echo @@date; date '+%Y-%m-%d %H:%M:%S %Z (%z)'"
)


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def _parse_df(lines: list[str]) -> list[dict[str, Any]]:
    rows = []
    for line in lines:
        parts = line.split(None, 5)
        if len(parts) < 6 or parts[0] == "Filesystem":
            continue
        rows.append({"filesystem": parts[0], "size": parts[1], "used": parts[2], "avail": parts[3],
                     "use_pct": int(parts[4].rstrip("%")) if parts[4].rstrip("%").isdigit() else parts[4],
                     "mount": parts[5]})
    return rows


def _parse_free(lines: list[str]) -> dict[str, Any]:
    header = lines[0].split() if lines else []
    out: dict[str, Any] = {}
    for line in lines[1:]:
        label, _, rest = line.partition(":")
        vals = rest.split()
        if label.strip() in ("Mem", "Swap") and vals:
            out[label.strip().lower() + "_mb"] = {k: int(v) if v.isdigit() else v for k, v in zip(header, vals)}
    return out


def _fmt_uptime(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h {rem // 60}m"


@tool()
def get_system_info(host: Optional[str] = None) -> dict[str, Any]:
    """Resumen del servidor: hostname, os-release, kernel, uptime/carga, CPU, memoria (free -m), discos (df -hP), SELinux y hora."""
    h, c = _conn(host)
    r = _run(c, _SYSINFO_CMD, 60)
    s = _sections(r.stdout)
    first = lambda k: (s.get(k) or [""])[0].strip() or None  # noqa: E731
    osr = {}
    for line in s.get("os", []):
        k, eq, v = line.partition("=")
        if eq:
            osr[k.strip()] = v.strip().strip('"')
    lscpu = {}
    for line in s.get("lscpu", []):
        k, sep, v = line.partition(":")
        if sep:
            lscpu[k.strip()] = v.strip()
    up = first("uptime")
    uptime_s = float(up.split()[0]) if up else None
    load = (first("loadavg") or "").split()
    nproc = first("nproc")
    return {
        "ok": True, "host": h.id, "hostname": first("hostname"), "fqdn": first("fqdn"),
        "os": {"pretty_name": osr.get("PRETTY_NAME"), "id": osr.get("ID"), "version_id": osr.get("VERSION_ID"),
               "redhat_release": first("redhat")},
        "kernel": first("kernel"),
        "uptime": {"seconds": uptime_s, "human": _fmt_uptime(uptime_s) if uptime_s is not None else None,
                   "boot": first("boot"), "load_avg": [float(x) for x in load[:3]] if len(load) >= 3 else None},
        "cpu": {"nproc": int(nproc) if nproc and nproc.isdigit() else nproc, "model": lscpu.get("Model name"),
                "architecture": lscpu.get("Architecture"), "sockets": lscpu.get("Socket(s)"),
                "cores_per_socket": lscpu.get("Core(s) per socket"),
                "threads_per_core": lscpu.get("Thread(s) per core")},
        "memory": _parse_free(s.get("free", [])),
        "disks": _parse_df(s.get("df", [])),
        "selinux": first("selinux"),
        "time": first("date"),
    }


# ============================================================
# Archivos
# ============================================================


@tool()
def list_directory(path: str = "~", host: Optional[str] = None, show_hidden: bool = False,
                   limit: int = 500) -> dict[str, Any]:
    """Lista un directorio vía SFTP: nombre, tipo (dir/file/link/other), tamaño, permisos, mtime ISO, uid/gid. '~' = home."""
    h, c = _conn(host)
    with c.sftp() as sftp:
        p = _sftp_path(sftp, path)
        entries = sftp.listdir_attr(p)
    items = [_attr(e) for e in sorted(entries, key=lambda e: e.filename)
             if show_hidden or not e.filename.startswith(".")]
    total = len(items)
    data, cut = _fit(items[:max(int(limit), 1)])
    return {"ok": True, "host": h.id, "path": p, "total": total, "count": len(data),
            "truncated": cut or total > len(data), "entries": data}


@tool()
def get_file_info(path: str, host: Optional[str] = None) -> dict[str, Any]:
    """Metadatos de un archivo/directorio (SFTP lstat/stat, destino si es enlace) + tipo de contenido (`file -b`) y dueño."""
    h, c = _conn(host)
    with c.sftp() as sftp:
        p = _sftp_path(sftp, path)
        info: dict[str, Any] = _attr(sftp.lstat(p), name=p)
        if info["type"] == "link":
            info["link_target"] = sftp.readlink(p)
            try:
                info["target"] = _attr(sftp.stat(p), name=info["link_target"])
            except OSError:
                info["target"] = None
                info["broken_link"] = True
    q = shlex.quote(p)
    r = _run(c, f"file -b -- {q}; stat -c '%U %G' -- {q}", 30)
    lines = r.stdout.splitlines()
    info["file_type"] = lines[0].strip() if lines else None
    if len(lines) > 1 and len(lines[1].split()) == 2:
        info["owner"], info["group"] = lines[1].split()
    return {"ok": True, "host": h.id, **info}


@tool()
def read_file(path: str, host: Optional[str] = None, offset_lines: int = 0, max_lines: int = 500,
              max_bytes: int = 2_000_000) -> dict[str, Any]:
    """
    Lee un archivo de texto vía SFTP (primeros max_bytes, tope 50 MB) y devuelve las líneas numeradas desde
    offset_lines (0 = inicio), hasta max_lines. Indica total de líneas (aprox. si no se leyó entero) y
    truncated/next_offset para paginar. Detecta binarios (NUL en los primeros 8 KB) y avisa. Para el final
    de logs grandes usa tail_file.
    """
    h, c = _conn(host)
    max_bytes = min(max(int(max_bytes), 1024), 50_000_000)
    offset, max_lines = max(int(offset_lines), 0), max(int(max_lines), 1)
    with c.sftp() as sftp:
        p = _sftp_path(sftp, path)
        st = sftp.stat(p)
        if stat.S_ISDIR(st.st_mode or 0):
            return {"ok": False, "host": h.id, "error": f"{p} es un directorio: usa list_directory"}
        size = st.st_size or 0
        with sftp.open(p, "rb") as f:
            if size:
                f.prefetch(min(size, max_bytes))
            data = f.read(max_bytes)
    base = {"host": h.id, "path": p, "size": size, "mtime": _iso(st.st_mtime)}
    if b"\x00" in data[:8192]:
        return {"ok": True, **base, "binary": True,
                "warning": "Archivo binario (NUL en los primeros 8 KB): no se muestra. Usa download_file o "
                           "run_command con strings/od/hexdump."}
    complete = len(data) >= size
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not complete and lines and not text.endswith("\n"):
        lines.pop()  # última línea cortada por max_bytes
    total = len(lines) if complete else int(len(lines) * size / max(len(data), 1))
    chunk = lines[offset:offset + max_lines]
    more = offset + len(chunk) < len(lines) or not complete
    content, cut = _clip("\n".join(f"{offset + i + 1}: {line}" for i, line in enumerate(chunk)))
    out: dict[str, Any] = {"ok": True, **base, "total_lines": total, "total_is_approx": not complete,
                           "first_line": offset + 1 if chunk else None, "last_line": offset + len(chunk) or None,
                           "truncated": more or cut, "content": content}
    if more and chunk:
        out["next_offset"] = offset + len(chunk)
    if not chunk and offset:
        out["warning"] = (f"offset_lines={offset} supera las {len(lines)} líneas leídas en max_bytes={max_bytes}: "
                          "sube max_bytes o usa tail_file.")
    return out


@tool()
def tail_file(path: str, host: Optional[str] = None, lines: int = 200, grep: Optional[str] = None) -> dict[str, Any]:
    """Últimas N líneas de un archivo (tail -n N), opcionalmente filtradas con una regex extendida (grep -E)."""
    h, c = _conn(host)
    n = min(max(int(lines), 1), 20000)
    cmd = f"tail -n {n} -- {_qpath(path)}"
    if grep:
        cmd += f" | grep -E -- {shlex.quote(grep)}"
    r = _run(c, cmd, 120)
    text, cut = _clip(r.stdout.rstrip("\n"))
    ok = r.exit_code == 0 or (bool(grep) and r.exit_code == 1 and not r.stderr.strip())
    out: dict[str, Any] = {"ok": ok, "host": h.id, "path": path, "exit_code": r.exit_code,
                           "lines": len(r.stdout.splitlines()), "truncated": cut, "content": text}
    if r.stderr.strip():
        out["error" if not ok else "stderr"] = _clip(r.stderr.strip(), 4000)[0]
    return out


_SIZE_RE = re.compile(r"^(\d+)\s*([kKmMgG]?)[bB]?$")


def _size_bytes(value: str | int) -> int:
    m = _SIZE_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"min_size inválido: {value!r} (usa bytes o sufijo k/M/G, p.ej. '10M')")
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]


@tool()
def search_files(path: str, host: Optional[str] = None, name: Optional[str] = None,
                 type: Optional[Literal["f", "d", "l"]] = None, max_depth: int = 5,
                 newer_than_minutes: Optional[int] = None, min_size: Optional[str] = None,
                 limit: int = 500) -> dict[str, Any]:
    """
    Busca archivos con find: name (patrón glob, p.ej. '*.log'), type f|d|l, max_depth, modificados hace menos de
    newer_than_minutes, tamaño >= min_size (bytes o '10M'/'1G'). Devuelve tipo, tamaño, mtime y ruta.
    Errores de permisos se ignoran (2>/dev/null).
    """
    if (path or "").strip().startswith("-"):
        raise ValueError("path no puede empezar por '-'")
    h, c = _conn(host)
    limit = min(max(int(limit), 1), 5000)
    parts = ["find", _qpath(path), "-maxdepth", str(min(max(int(max_depth), 0), 50))]
    if name:
        parts += ["-name", shlex.quote(name)]
    if type:
        if type not in ("f", "d", "l"):
            raise ValueError("type debe ser f, d o l")
        parts += ["-type", type]
    if newer_than_minutes:
        parts += ["-mmin", f"-{int(newer_than_minutes)}"]
    if min_size:
        parts += ["-size", f"+{max(_size_bytes(min_size) - 1, 0)}c"]
    parts += ["-printf", r"'%y\t%s\t%TY-%Tm-%Td %TH:%TM\t%p\n'"]
    r = _run(c, " ".join(parts) + f" 2>/dev/null | head -n {limit + 1}", 300)
    kinds = {"f": "file", "d": "dir", "l": "link"}
    rows = []
    for line in r.stdout.splitlines():
        f = line.split("\t", 3)
        if len(f) == 4:
            rows.append({"type": kinds.get(f[0], f[0]), "size": int(f[1]) if f[1].isdigit() else f[1],
                         "mtime": f[2], "path": f[3]})
    more = len(rows) > limit
    data, cut = _fit(rows[:limit])
    return {"ok": True, "host": h.id, "count": len(data), "truncated": more or cut, "files": data}


@tool()
def grep_text(pattern: str, path: str, host: Optional[str] = None, recursive: bool = True,
              ignore_case: bool = False, fixed: bool = False, include: Optional[str] = None,
              max_matches: int = 500, context: int = 0) -> dict[str, Any]:
    """
    Busca texto con grep (regex extendida, o literal con fixed=true) en un archivo o, con recursive, en un
    directorio. include filtra nombres ('*.log'). context = líneas de contexto. Ignora binarios (-I).
    Devuelve archivo, nº de línea y texto de cada coincidencia.
    """
    h, c = _conn(host)
    max_matches = min(max(int(max_matches), 1), 5000)
    context = min(max(int(context), 0), 10)
    parts = ["grep", "-n", "-I", "-H", "-Z", "-F" if fixed else "-E", "-m", str(max_matches)]
    if recursive:
        parts.append("-r")
    if ignore_case:
        parts.append("-i")
    if include:
        parts.append(shlex.quote(f"--include={include}"))
    if context:
        parts += ["-C", str(context)]
    parts += ["--", shlex.quote(pattern), _qpath(path)]
    head = max_matches * (2 * context + 2) + 1
    r = _run(c, " ".join(parts) + f" | head -n {head}", 300)
    matches, rows = 0, []
    for line in r.stdout.splitlines():
        fname, sep, rest = line.partition("\x00")
        m = re.match(r"(\d+)([:-])(.*)", rest, re.S) if sep else None
        if not m:
            continue
        is_match = m.group(2) == ":"
        if is_match:
            matches += 1
            if matches > max_matches:
                break
        row: dict[str, Any] = {"file": fname, "line": int(m.group(1)), "text": m.group(3)[:500]}
        if context:
            row["match"] = is_match
        rows.append(row)
    data, cut = _fit(rows)
    out: dict[str, Any] = {"ok": True, "host": h.id, "matches": min(matches, max_matches),
                           "truncated": matches > max_matches or cut, "results": data}
    if r.stderr.strip():
        out["stderr"] = _clip("\n".join(r.stderr.strip().splitlines()[:30]), 4000)[0]
    return out


@tool()
def disk_usage(path: Optional[str] = None, host: Optional[str] = None, depth: int = 1) -> dict[str, Any]:
    """Uso de sistemas de archivos (df -hP) y, si se da path, los 50 subdirectorios más grandes (du -h --max-depth)."""
    h, c = _conn(host)
    out: dict[str, Any] = {"ok": True, "host": h.id,
                           "filesystems": _parse_df(_run(c, "df -hP 2>/dev/null", 60).stdout.splitlines())}
    if path:
        d = min(max(int(depth), 0), 5)
        r = _run(c, f"du -h --max-depth={d} -- {_qpath(path)} 2>/dev/null | sort -rh | head -50", 300)
        out["path"] = path
        out["usage"] = [{"size": s, "path": p} for s, _, p in (line.partition("\t") for line in r.stdout.splitlines())
                        if p]
    return out


# ============================================================
# Procesos, red y servicios
# ============================================================


@tool()
def get_processes(host: Optional[str] = None, sort_by: Literal["cpu", "mem"] = "cpu", limit: int = 30,
                  filter: Optional[str] = None) -> dict[str, Any]:
    """Procesos (ps) ordenados por %CPU o %MEM: pid, ppid, usuario, %cpu, %mem, rss_kb, tiempo, estado y comando. filter = subcadena (usuario/comando)."""
    h, c = _conn(host)
    key = "-%cpu" if sort_by == "cpu" else "-%mem"
    r = _run(c, f"ps -ww -eo pid,ppid,user:32,%cpu,%mem,rss,etime,stat,args --sort={key}", 60)
    procs = []
    for line in r.stdout.splitlines()[1:]:
        f = line.split(None, 8)
        if len(f) < 9:
            continue
        if filter and filter.lower() not in (f[2] + " " + f[8]).lower():
            continue
        procs.append({"pid": int(f[0]), "ppid": int(f[1]), "user": f[2], "cpu_pct": float(f[3]),
                      "mem_pct": float(f[4]), "rss_kb": int(f[5]), "etime": f[6], "stat": f[7],
                      "command": f[8][:500]})
    data, cut = _fit(procs[:max(int(limit), 1)])
    return {"ok": True, "host": h.id, "total": len(procs), "count": len(data),
            "truncated": cut or len(procs) > len(data), "processes": data}


_SS_USER_RE = re.compile(r'\("([^"]*)",(?:pid=)?(\d+)')


def _split_port(addr: str) -> tuple[str, Any]:
    a, _, p = addr.rpartition(":")
    return a, int(p) if p.isdigit() else p


@tool()
def get_listening_ports(host: Optional[str] = None) -> dict[str, Any]:
    """Puertos TCP/UDP en escucha (ss -tulpn, o netstat -tulpn si no hay ss). Sin root solo se ve el proceso de los sockets propios."""
    h, c = _conn(host)
    r = _run(c, "ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null", 60)
    lines = r.stdout.splitlines()
    ports = []
    if lines and lines[0].startswith("Netid"):
        source = "ss"
        for line in lines[1:]:
            f = line.split()
            if len(f) < 6:
                continue
            addr, port = _split_port(f[4])
            procs = _SS_USER_RE.findall(" ".join(f[6:]))
            ports.append({"proto": f[0], "state": f[1], "address": addr, "port": port,
                          "process": ",".join(sorted({n for n, _ in procs})) or None,
                          "pids": sorted({int(p) for _, p in procs}) or None})
    else:
        source = "netstat"
        for line in lines:
            f = line.split()
            if not f or not f[0].startswith(("tcp", "udp")) or len(f) < 5:
                continue
            addr, port = _split_port(f[3])
            is_tcp = f[0].startswith("tcp")
            prog = (f[6] if len(f) > 6 else "") if is_tcp else (f[5] if len(f) > 5 else "")
            pid, _, name = prog.partition("/")
            ports.append({"proto": f[0], "state": f[5] if is_tcp and len(f) > 5 else None, "address": addr,
                          "port": port, "process": name or None, "pids": [int(pid)] if pid.isdigit() else None})
    ports.sort(key=lambda p: (p["proto"], p["port"] if isinstance(p["port"], int) else 0))
    return {"ok": True, "host": h.id, "source": source, "count": len(ports), "ports": ports}


@tool()
def get_services(host: Optional[str] = None, state: Optional[str] = None,
                 pattern: Optional[str] = None) -> dict[str, Any]:
    """
    Servicios systemd (systemctl list-units --type=service --all): unidad, load, active, sub, descripción.
    state filtra por estado active o sub ('running', 'failed', 'active', 'inactive', 'exited'...).
    pattern filtra por nombre/descripción (subcadena o glob '*java*').
    """
    h, c = _conn(host)
    r = _run(c, "systemctl list-units --type=service --all --no-pager --no-legend", 60)
    services = []
    for line in r.stdout.splitlines():
        f = line.strip().lstrip("●*").split(None, 4)
        if len(f) < 4:
            continue
        svc = {"unit": f[0], "load": f[1], "active": f[2], "sub": f[3], "description": f[4] if len(f) > 4 else ""}
        if state and state.lower() not in (svc["active"].lower(), svc["sub"].lower()):
            continue
        if pattern:
            hay = f"{svc['unit']} {svc['description']}".lower()
            pat = pattern.lower()
            if not (fnmatch.fnmatch(svc["unit"].lower(), pat) if any(ch in pat for ch in "*?[") else pat in hay):
                continue
        services.append(svc)
    summary: dict[str, int] = {}
    for s in services:
        summary[s["sub"]] = summary.get(s["sub"], 0) + 1
    data, cut = _fit(services)
    out: dict[str, Any] = {"ok": r.exit_code == 0, "host": h.id, "count": len(services), "by_sub_state": summary,
                           "truncated": cut, "services": data}
    if r.exit_code != 0 and r.stderr.strip():
        out["error"] = _clip(r.stderr.strip(), 4000)[0]
    return out


_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]+$")


@tool()
def service_status(name: str, host: Optional[str] = None, lines: int = 30) -> dict[str, Any]:
    """systemctl status de un servicio con sus últimas N líneas de log; extrae Loaded/Active/Main PID."""
    if not _UNIT_RE.match(name) or name.startswith("-"):
        raise ValueError(f"Nombre de unidad inválido: {name!r}")
    h, c = _conn(host)
    n = min(max(int(lines), 0), 1000)
    r = _run(c, f"systemctl status {shlex.quote(name)} --no-pager -l -n {n}", 60)
    fields = {}
    for line in r.stdout.splitlines()[:15]:
        k, sep, v = line.strip().partition(":")
        if sep and k in ("Loaded", "Active", "Main PID", "Tasks", "Memory", "CGroup"):
            fields[k.lower().replace(" ", "_")] = v.strip()
    text, cut = _clip(r.stdout.rstrip())
    # exit 3 = unidad inactiva/fallida (no es error de la tool); 4 = no existe
    out: dict[str, Any] = {"ok": r.exit_code in (0, 3), "host": h.id, "unit": name, "exit_code": r.exit_code,
                           **fields, "truncated": cut, "status": text}
    if r.stderr.strip():
        out["error" if r.exit_code not in (0, 3) else "stderr"] = _clip(r.stderr.strip(), 4000)[0]
    return out


_PRIO = "emerg|alert|crit|err|warning|notice|info|debug|[0-7]"
_PRIO_RE = re.compile(rf"^({_PRIO})(\.\.({_PRIO}))?$")


@tool()
def get_journal(host: Optional[str] = None, unit: Optional[str] = None, since: Optional[str] = None,
                until: Optional[str] = None, priority: Optional[str] = None, grep: Optional[str] = None,
                lines: int = 200) -> dict[str, Any]:
    """
    Últimas N líneas del journal (journalctl --no-pager): filtros unit, since/until (p.ej. '2026-10-29 10:00',
    'yesterday', '1 hour ago'), priority ('err', 'warning', '0..3') y grep (regex extendida; en RHEL 7
    journalctl no tiene -g, se filtra con grep -E). Sin el grupo systemd-journal/adm el usuario solo ve sus
    propios logs (o nada).
    """
    h, c = _conn(host)
    n = min(max(int(lines), 1), 20000)
    parts = ["journalctl", "--no-pager"]
    if unit:
        if not _UNIT_RE.match(unit) or unit.startswith("-"):
            raise ValueError(f"Nombre de unidad inválido: {unit!r}")
        parts += ["-u", shlex.quote(unit)]
    if since:
        parts += ["--since", shlex.quote(since)]
    if until:
        parts += ["--until", shlex.quote(until)]
    if priority:
        if not _PRIO_RE.match(priority):
            raise ValueError(f"priority inválida: {priority!r} (emerg..debug, 0-7 o rango 'err..warning')")
        parts += ["-p", priority]
    if grep:
        cmd = " ".join(parts) + f" | grep -E -- {shlex.quote(grep)} | tail -n {n}"
    else:
        cmd = " ".join(parts + ["-n", str(n)])
    r = _run(c, cmd, 180)
    text, cut = _clip(r.stdout.rstrip("\n"))
    body = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("-- ")]
    out: dict[str, Any] = {"ok": True, "host": h.id, "lines": len(body), "truncated": cut, "content": text}
    if r.stderr.strip():
        out["stderr"] = _clip(r.stderr.strip(), 4000)[0]
    if not body:
        out["hint"] = ("Sin entradas. Si esperabas logs: el usuario puede no estar en el grupo systemd-journal/adm "
                       "(solo ve sus propios logs) o el journal no es persistente; prueba tail_file /var/log/messages.")
    return out


# ============================================================
# Transferencia y auditoría
# ============================================================


@tool()
def download_file(remote_path: str, host: Optional[str] = None, local_dir: Optional[str] = None) -> dict[str, Any]:
    """Descarga un archivo remoto vía SFTP (máx. 500 MB). Sin local_dir se guarda en C:\\DVB\\exports\\linux-ssh-readonly\\<host>\\."""
    h, c = _conn(host)
    t0 = time.monotonic()
    with c.sftp() as sftp:
        p = _sftp_path(sftp, remote_path)
        st = sftp.stat(p)
        if not stat.S_ISREG(st.st_mode or 0):
            return {"ok": False, "host": h.id, "error": f"{p} no es un archivo regular"}
        if (st.st_size or 0) > MAX_DOWNLOAD_BYTES:
            return {"ok": False, "host": h.id,
                    "error": f"{p} ocupa {st.st_size} bytes (> 500 MB): usa tail_file/grep_text o comprímelo tú"}
        target = Path(local_dir) if local_dir else TRANSFER_DIR / h.id
        target.mkdir(parents=True, exist_ok=True)
        local = target / (re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", PurePosixPath(p).name) or "archivo")
        sftp.get(p, str(local))
    return {"ok": True, "host": h.id, "remote": p, "local": str(local), "bytes": local.stat().st_size,
            "duration_s": round(time.monotonic() - t0, 1)}


@tool("local")
def query_audit_log(limit: int = 50, tool: Optional[str] = None) -> dict[str, Any]:
    """Últimas llamadas a este MCP (JSONL local): tool, argumentos recortados, resultado y duración."""
    if not AUDIT_LOG.exists():
        return {"ok": True, "log": str(AUDIT_LOG), "entries": []}
    rows = [json.loads(line) for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    if tool:
        rows = [r for r in rows if r.get("tool") == tool]
    data, cut = _fit(rows[-max(int(limit), 1):])
    return {"ok": True, "log": str(AUDIT_LOG), "truncated": cut, "entries": data}


# ============================================================
# CLI
# ============================================================


async def _handshake() -> int:
    from mcp.client import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        for t in tools.tools:
            ro = t.annotations.read_only_hint if t.annotations else None
            print(f"- {t.name} (readOnly={ro})")
        print(f"{len(tools.tools)} tools")
    return 0


def _close_all() -> None:
    for c in CONNS.values():
        c.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Linux vía SSH (solo lectura) - MCP server")
    parser.add_argument("--test", nargs="?", const="", metavar="HOST", help="Prueba la conexión y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía cliente MCP")
    args = parser.parse_args()

    if args.test is not None:
        try:
            print(json.dumps(asyncio.run(test_connection(host=args.test or None)), indent=2, ensure_ascii=False))
        finally:
            _close_all()
        return 0
    if args.handshake:
        return asyncio.run(_handshake())

    logger.info(f"linux-ssh-readonly MCP v{__version__} — hosts: {', '.join(SETTINGS.hosts)}")
    try:
        mcp.run()
    finally:
        _close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
