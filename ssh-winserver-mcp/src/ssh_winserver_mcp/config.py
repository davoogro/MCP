"""Inventario de hosts (alias de ~/.ssh/config) en config/hosts.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("SSHWIN_CONFIG") or PKG_ROOT / "config" / "hosts.json")
STATE_DIR = Path(os.environ.get("SSHWIN_STATE_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ssh-winserver-mcp")
TRANSFER_DIR = Path(os.environ.get("SSHWIN_TRANSFER_DIR") or r"C:\DVB\exports\ssh-winserver")
MAX_CHARS = int(os.environ.get("SSHWIN_MAX_CHARS", "60000"))


@dataclass
class Host:
    id: str
    ssh_alias: str
    description: str = ""
    allow_write: bool = True


@dataclass
class Settings:
    hosts: dict[str, Host]
    default: str

    @classmethod
    def load(cls) -> "Settings":
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        hosts = {
            hid: Host(hid, h.get("ssh_alias", hid), h.get("description", ""), bool(h.get("allow_write", True)))
            for hid, h in data["hosts"].items()
        }
        default = data.get("default") or next(iter(hosts))
        return cls(hosts, default)

    def get(self, host: str | None) -> Host:
        hid = host or self.default
        if hid not in self.hosts:
            raise ValueError(f"Host '{hid}' no está en {CONFIG_PATH}. Disponibles: {', '.join(self.hosts)}")
        return self.hosts[hid]
