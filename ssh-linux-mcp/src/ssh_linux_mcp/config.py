"""Inventario de hosts (config/hosts.json) y credenciales desde .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

PKG_ROOT = Path(__file__).resolve().parents[2]
SHARED_ENV = Path(r"C:\DVB\mcp\.env")


def _load_env() -> None:
    # override=False: el primero que define una variable gana (local > compartido > SSHLX_ENV_FILE).
    local = PKG_ROOT / ".env"
    if local.is_file():
        load_dotenv(local, override=False)
    if SHARED_ENV.is_file():
        load_dotenv(SHARED_ENV, override=False)
    extra = os.environ.get("SSHLX_ENV_FILE")
    if extra and Path(extra).is_file():
        load_dotenv(extra, override=False)


_load_env()

CONFIG_PATH = Path(os.environ.get("SSHLX_CONFIG") or PKG_ROOT / "config" / "hosts.json")
STATE_DIR = Path(os.environ.get("SSHLX_STATE_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ssh-linux-mcp")
KNOWN_HOSTS = STATE_DIR / "known_hosts"
TRANSFER_DIR = Path(os.environ.get("SSHLX_TRANSFER_DIR") or r"C:\DVB\exports\ssh-linux")
MAX_CHARS = int(os.environ.get("SSHLX_MAX_CHARS", "60000"))


@dataclass
class Host:
    id: str
    hostname: str
    port: int = 22
    user_env: str = "LINUX_SSH_USER"
    password_env: str = "LINUX_SSH_PASSWORD"
    proxy: Optional[str] = None
    description: str = ""

    def proxy_addr(self) -> Optional[tuple[str, int]]:
        if not self.proxy:
            return None
        u = urlparse(self.proxy if "://" in self.proxy else f"socks5://{self.proxy}")
        if u.scheme not in ("socks5", "socks5h"):
            raise ValueError(f"Host '{self.id}': proxy '{self.proxy}' no soportado (solo socks5://host:puerto)")
        if not u.hostname or not u.port:
            raise ValueError(f"Host '{self.id}': proxy '{self.proxy}' inválido (esperado socks5://host:puerto)")
        return u.hostname, u.port

    @property
    def username(self) -> Optional[str]:
        return os.environ.get(self.user_env)

    def credentials(self) -> tuple[str, str]:
        user, password = os.environ.get(self.user_env), os.environ.get(self.password_env)
        missing = [name for name, val in ((self.user_env, user), (self.password_env, password)) if not val]
        if missing:
            raise ValueError(f"Faltan variables de entorno para '{self.id}': {', '.join(missing)} "
                             f"(defínelas en {SHARED_ENV} o en SSHLX_ENV_FILE)")
        return user, password  # type: ignore[return-value]

    def public(self) -> dict:
        return {"id": self.id, "hostname": self.hostname, "port": self.port, "user": self.username,
                "user_env": self.user_env, "proxy": self.proxy, "description": self.description}


@dataclass
class Settings:
    hosts: dict[str, Host]
    default: str

    @classmethod
    def load(cls) -> "Settings":
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        hosts = {
            hid: Host(hid, h["hostname"], int(h.get("port", 22)), h.get("user_env", "LINUX_SSH_USER"),
                      h.get("password_env", "LINUX_SSH_PASSWORD"), h.get("proxy") or None, h.get("description", ""))
            for hid, h in data["hosts"].items()
        }
        default = data.get("default") or next(iter(hosts))
        return cls(hosts, default)

    def get(self, host: str | None) -> Host:
        hid = host or self.default
        if hid not in self.hosts:
            raise ValueError(f"Host '{hid}' no está en {CONFIG_PATH}. Disponibles: {', '.join(self.hosts)}")
        return self.hosts[hid]
