"""Configuración del servidor WSL MCP.

Carga ``wsl-mcp/.env`` y, sin pisar variables ya definidas, ``C:\\DVB\\mcp\\.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parents[2]

MAX_COMMAND_TIMEOUT = 3600


def load_env_files() -> list[Path]:
    candidates = [PACKAGE_DIR / ".env", PACKAGE_DIR.parent / ".env"]
    loaded: list[Path] = []
    for path in candidates:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            loaded.append(path)
    return loaded


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True, repr=False)
class Settings:
    distro: str
    user: str
    default_cwd: str
    command_timeout: int
    max_output: int
    shell: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            distro=os.environ.get("WSL_DISTRO", "mynoble24.04").strip() or "mynoble24.04",
            user=os.environ.get("WSL_USER", "").strip(),
            default_cwd=os.environ.get("WSL_DEFAULT_CWD", "").strip(),
            command_timeout=max(1, _env_int("WSL_COMMAND_TIMEOUT", 120)),
            max_output=max(1, _env_int("WSL_MAX_OUTPUT", 30000)),
            shell=os.environ.get("WSL_SHELL", "/bin/bash").strip() or "/bin/bash",
        )

    def resolve_distro(self, distro: Optional[str]) -> str:
        value = (distro or "").strip()
        return value or self.distro

    def public_dict(self) -> dict:
        return {
            "distro": self.distro,
            "user": self.user or None,
            "default_cwd": self.default_cwd or None,
            "command_timeout": self.command_timeout,
            "max_output": self.max_output,
            "shell": self.shell,
            "max_command_timeout": MAX_COMMAND_TIMEOUT,
            "env_files": [str(p) for p in LOADED_ENV_FILES],
        }


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


LOADED_ENV_FILES: list[Path] = load_env_files()
