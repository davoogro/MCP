"""Conexión SSH persistente por host (paramiko), opcionalmente a través de un proxy SOCKS5.

Un `paramiko.SSHClient` por host protegido con un lock. Host keys TOFU en
%LOCALAPPDATA%\\ssh-linux-mcp\\known_hosts: la primera vez se aceptan y guardan; si cambian, se rechaza.
Si el transporte está caído o un comando falla por error de transporte, se reconecta una vez y se reintenta.
SFTP se usa solo para operaciones de lectura.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import paramiko
import socks

from ssh_linux_mcp.config import KNOWN_HOSTS, Host

logger = logging.getLogger("ssh-linux-mcp")

CONNECT_TIMEOUT = 20
KEEPALIVE_S = 15
SFTP_TIMEOUT = 120
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
_TRANSPORT_ERRORS = (paramiko.SSHException, EOFError, OSError)


class RemoteError(RuntimeError):
    pass


def fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        client.get_host_keys().add(hostname, key.get_name(), key)
        KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(KNOWN_HOSTS))
        logger.warning("Host key nueva para %s aceptada y guardada en %s: %s %s",
                       hostname, KNOWN_HOSTS, key.get_name(), fingerprint(key))


@dataclass
class Result:
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    output_capped: bool = False


class Connection:
    def __init__(self, host: Host):
        self.host = host
        self.client: Optional[paramiko.SSHClient] = None
        self.lock = threading.Lock()
        self.connected_at: Optional[float] = None
        self.connect_s: Optional[float] = None

    # ---------------------------------------------------------------- ciclo de vida
    def is_active(self) -> bool:
        transport = self.client.get_transport() if self.client else None
        return bool(transport and transport.is_active())

    def _open_socket(self) -> Optional[socks.socksocket]:
        proxy = self.host.proxy_addr()
        if not proxy:
            return None
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, proxy[0], proxy[1], rdns=True)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((self.host.hostname, self.host.port))
        except socks.ProxyConnectionError as exc:
            sock.close()
            raise RemoteError(f"No se pudo conectar al proxy SOCKS5 {proxy[0]}:{proxy[1]} ({exc}). "
                              "¿Está levantado el proxy/túnel SOCKS? Levántalo y reintenta.") from exc
        except (socks.ProxyError, OSError) as exc:
            sock.close()
            raise RemoteError(f"El proxy SOCKS5 {proxy[0]}:{proxy[1]} no pudo llegar a "
                              f"{self.host.hostname}:{self.host.port}: {exc}") from exc
        return sock

    def _connect(self) -> None:
        self._close()
        user, password = self.host.credentials()
        client = paramiko.SSHClient()
        if KNOWN_HOSTS.is_file():
            client.load_host_keys(str(KNOWN_HOSTS))
        client.set_missing_host_key_policy(_TrustOnFirstUse())
        target = f"{user}@{self.host.hostname}:{self.host.port}"
        logger.info("Conectando por SSH a %s%s", target, f" vía {self.host.proxy}" if self.host.proxy else "")
        t0 = time.monotonic()
        sock = self._open_socket()
        try:
            client.connect(self.host.hostname, port=self.host.port, username=user, password=password, sock=sock,
                           timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT,
                           allow_agent=False, look_for_keys=False)
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise RemoteError(f"La host key de {self.host.hostname} CAMBIÓ (esperada {fingerprint(exc.expected_key)}, "
                              f"recibida {fingerprint(exc.key)}): conexión rechazada. Si el cambio es legítimo, "
                              f"borra su línea en {KNOWN_HOSTS}.") from exc
        except paramiko.AuthenticationException as exc:
            client.close()
            raise RemoteError(f"Autenticación fallida para {target}: {exc}") from exc
        except _TRANSPORT_ERRORS as exc:
            client.close()
            if sock is not None:
                sock.close()
            raise RemoteError(f"No se pudo abrir SSH a {target}: {type(exc).__name__}: {exc}") from exc
        transport = client.get_transport()
        assert transport is not None
        transport.set_keepalive(KEEPALIVE_S)
        self.client = client
        self.connect_s = time.monotonic() - t0
        self.connected_at = time.time()
        logger.info("SSH a %s abierto en %.2fs (%s)", target, self.connect_s, transport.remote_version)

    def ensure(self) -> paramiko.SSHClient:
        with self.lock:
            if not self.is_active():
                self._connect()
            assert self.client is not None
            return self.client

    def _close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001 - cierre best effort
                pass
        self.client = None
        self.connected_at = None

    def close(self) -> None:
        with self.lock:
            self._close()

    # ---------------------------------------------------------------- ejecución
    def run(self, command: str, timeout_s: float = 60) -> Result:
        """Ejecuta `command` (ya validado) con LC_ALL=C. Mata el canal si excede timeout_s."""
        full = "export LC_ALL=C; " + command
        for attempt in (1, 2):
            client = self.ensure()
            try:
                stdin, stdout, stderr = client.exec_command(full, timeout=timeout_s)
            except _TRANSPORT_ERRORS as exc:
                if attempt == 2:
                    raise RemoteError(f"Error de transporte SSH al ejecutar: {type(exc).__name__}: {exc}") from exc
                logger.warning("Error de transporte (%s); reconectando y reintentando una vez", exc)
                self.close()
                continue
            res = self._collect(stdin, stdout, timeout_s)
            if res.exit_code == -1 and not res.timed_out and not self.is_active() and attempt == 1:
                logger.warning("La conexión se cayó durante el comando; reconectando y reintentando una vez")
                self.close()
                continue
            return res
        raise RemoteError("No se pudo ejecutar el comando tras reconectar")

    @staticmethod
    def _collect(stdin: paramiko.ChannelFile, stdout: paramiko.ChannelFile, timeout_s: float) -> Result:
        chan = stdout.channel
        t0 = time.monotonic()
        try:
            stdin.close()
            chan.shutdown_write()
        except _TRANSPORT_ERRORS:
            pass
        out, err = bytearray(), bytearray()
        deadline = t0 + timeout_s
        timed_out = capped = False
        while True:
            got = False
            while chan.recv_ready():
                out += chan.recv(65536)
                got = True
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
                got = True
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if len(out) + len(err) > MAX_OUTPUT_BYTES:
                capped = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            if not got:
                time.sleep(0.02)
        if timed_out or capped:
            chan.close()
            code: Optional[int] = None
        else:
            code = chan.recv_exit_status()
            chan.close()
        return Result(code, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace"),
                      time.monotonic() - t0, timed_out, capped)

    # ---------------------------------------------------------------- SFTP (solo lectura)
    @contextmanager
    def sftp(self) -> Iterator[paramiko.SFTPClient]:
        """Cliente SFTP efímero. Úsese solo con listdir_attr / stat / lstat / readlink / open('rb') / get."""
        sftp: Optional[paramiko.SFTPClient] = None
        for attempt in (1, 2):
            client = self.ensure()
            try:
                sftp = client.open_sftp()
                break
            except _TRANSPORT_ERRORS as exc:
                if attempt == 2:
                    raise RemoteError(f"No se pudo abrir SFTP: {type(exc).__name__}: {exc}") from exc
                logger.warning("Error abriendo SFTP (%s); reconectando y reintentando una vez", exc)
                self.close()
        assert sftp is not None
        sftp.get_channel().settimeout(SFTP_TIMEOUT)
        try:
            yield sftp
        finally:
            sftp.close()

    def info(self) -> dict:
        transport = self.client.get_transport() if self.client else None
        if not transport:
            return {}
        banner = transport.get_banner()
        if isinstance(banner, bytes):
            banner = banner.decode("utf-8", "replace")
        key = transport.get_remote_server_key()
        return {"server_version": transport.remote_version,
                "banner": banner.strip() if banner else None,
                "host_key": f"{key.get_name()} {fingerprint(key)}"}
