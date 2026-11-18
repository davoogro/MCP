"""Túnel SSH bajo demanda hacia winsrv01 (192.0.2.38 solo es alcanzable vía ProxyJump jumphost).

Si el puerto local del servidor (p. ej. 127.0.0.1:15439) no escucha, se lanza
``ssh -N -L 127.0.0.1:<local>:<remoto> <host_ssh>`` como proceso independiente usando
~/.ssh/config (ProxyJump y llave ya configurados). El proceso se deja vivo para que lo
reutilicen otras instancias del MCP (Claude Code y Claude Desktop); si muere, la siguiente
conexión lo vuelve a abrir.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
import time

from sqlserver_readwrite_mcp.config import Server, Settings

logger = logging.getLogger("sqlserver-readwrite-mcp")

_LOCK = threading.Lock()
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def parse_tunnel(spec: str) -> tuple[str, str, int]:
    """'winsrv01:127.0.0.1:49152' -> ('winsrv01', '127.0.0.1', 49152)."""
    parts = spec.split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValueError(f"OPT_SSH_TUNNEL inválido (usa host_ssh:host_remoto:puerto): {spec!r}")
    try:
        return parts[0].strip(), parts[1].strip(), int(parts[2])
    except ValueError:
        raise ValueError(f"Puerto inválido en OPT_SSH_TUNNEL: {spec!r}") from None


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_tunnel(settings: Settings, server: Server, wait_s: float = 25.0) -> None:
    """Garantiza que server.host:server.port escuche, abriendo el túnel SSH si hace falta."""
    if not settings.ssh_tunnel or server.host not in _LOCAL_HOSTS:
        return
    if port_open(server.host, server.port):
        return
    with _LOCK:
        if port_open(server.host, server.port):
            return
        ssh_host, remote_host, remote_port = parse_tunnel(settings.ssh_tunnel)
        ssh = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
        cmd = [
            ssh, "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-L", f"{server.host}:{server.port}:{remote_host}:{remote_port}", ssh_host,
        ]
        logger.info(f"Abriendo túnel SSH: {' '.join(cmd[1:])}")
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=flags, close_fds=True)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if port_open(server.host, server.port, timeout=0.5):
                logger.info(f"Túnel SSH listo en {server.host}:{server.port} (pid {proc.pid})")
                return
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode(errors="replace").strip()
                raise ConnectionError(f"El túnel SSH a {ssh_host} terminó (exit {proc.returncode}): {err[:500]}")
            time.sleep(0.3)
        proc.kill()
        raise ConnectionError(f"El túnel SSH a {ssh_host} no abrió {server.host}:{server.port} en {wait_s:.0f} s")
