"""Configuración del servidor UNC FS MCP.

Carga ``windows-fileshare-mcp/.env``, después el ``.env`` compartido de ``C:\\DVB\\mcp``
(``override=False``) y, si existe, el archivo de ``UNC_MCP_ENV_FILE``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # C:\DVB\mcp\windows-fileshare-mcp


def load_env_files() -> list[Path]:
    """Carga .env propio y, como fallback, el .env compartido de C:\\DVB\\mcp.

    Nunca sobreescribe variables ya presentes en el entorno del proceso.
    ``UNC_MCP_ENV_FILE`` se carga primero si está definido.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("UNC_MCP_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates += [PACKAGE_DIR / ".env", PACKAGE_DIR.parent / ".env"]
    loaded: list[Path] = []
    for path in candidates:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            loaded.append(path)
    return loaded


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _nonempty(name: str) -> str:
    return os.environ.get(name, "").strip()


def _parse_roots(raw: str) -> dict[str, str]:
    """Parsea ``alias=\\\\server\\share;alias2=...``."""
    roots: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        alias, _, path = part.partition("=")
        alias = alias.strip()
        path = path.strip()
        if alias and path:
            roots[alias] = path
    return roots


_DEFAULT_ROOTS_RAW = r"share01=\\dbsrv02\OneDrive - Company"


_runtime_roots: dict[str, str] = {}


@dataclass(frozen=True, repr=False)
class Settings:
    roots: dict[str, str]
    default_root: str
    allowlist_only: bool
    allow_any_unc: bool
    allow_write: bool
    allow_local: bool
    max_read_bytes: int
    max_scan_entries: int
    max_write_bytes: int
    redact_secrets: bool
    op_timeout: float
    scan_timeout: float
    max_response_chars: int

    @classmethod
    def from_env(cls) -> "Settings":
        raw_roots = _nonempty("UNC_ROOTS") or _DEFAULT_ROOTS_RAW
        roots = _parse_roots(raw_roots)
        default_root = _nonempty("UNC_DEFAULT_ROOT") or (next(iter(roots)) if roots else "")
        if default_root and default_root not in roots and roots:
            default_root = next(iter(roots))
        return cls(
            roots=roots,
            default_root=default_root,
            allowlist_only=_env_bool("UNC_ALLOWLIST_ONLY", False),
            allow_any_unc=_env_bool("UNC_ALLOW_ANY_UNC", True),
            allow_write=_env_bool("UNC_ALLOW_WRITE", False),
            allow_local=_env_bool("UNC_ALLOW_LOCAL", False),
            max_read_bytes=max(1, _env_int("UNC_MAX_READ_BYTES", 10_485_760)),
            max_scan_entries=max(1, _env_int("UNC_MAX_SCAN_ENTRIES", 50_000)),
            max_write_bytes=max(1, _env_int("UNC_MAX_WRITE_BYTES", 10_485_760)),
            redact_secrets=_env_bool("UNC_REDACT_SECRETS", True),
            op_timeout=max(0.1, float(_env_int("UNC_OP_TIMEOUT", 30))),
            scan_timeout=max(0.1, float(_env_int("UNC_SCAN_TIMEOUT", 60))),
            max_response_chars=max(1, _env_int("UNC_MAX_RESPONSE_CHARS", 80_000)),
        )

    def public_dict(self) -> dict:
        return {
            "roots": {alias: path for alias, path in self.roots.items()},
            "default_root": self.default_root or None,
            "allowlist_only": self.allowlist_only,
            "allow_any_unc": self.allow_any_unc,
            "allow_write": self.allow_write,
            "allow_local": self.allow_local,
            "runtime_roots": dict(_runtime_roots),
            "max_read_bytes": self.max_read_bytes,
            "max_scan_entries": self.max_scan_entries,
            "max_write_bytes": self.max_write_bytes,
            "redact_secrets": self.redact_secrets,
            "op_timeout": self.op_timeout,
            "scan_timeout": self.scan_timeout,
            "max_response_chars": self.max_response_chars,
            "env_files": [str(path) for path in LOADED_ENV_FILES],
        }

    def __repr__(self) -> str:
        return (
            f"Settings(roots={list(self.roots.keys())!r}, default_root={self.default_root!r}, "
            f"allowlist_only={self.allowlist_only!r}, allow_write={self.allow_write!r})"
        )


_settings: Optional[Settings] = None
_generation = 0


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    global _settings, _generation
    _runtime_roots.clear()
    _settings = Settings.from_env()
    _generation += 1
    return _settings


def settings_generation() -> int:
    get_settings()
    return _generation


def merged_roots() -> dict[str, str]:
    """Raíces de entorno más alias añadidos en runtime (add_root)."""
    settings = get_settings()
    out = dict(settings.roots)
    out.update(_runtime_roots)
    return out


def add_runtime_root(alias: str, unc_path: str) -> dict[str, str]:
    """Registra un alias adicional en memoria (valida que la ruta exista)."""
    global _generation
    alias = alias.strip()
    path = os.path.normpath(unc_path.strip().replace("/", "\\"))
    if not alias:
        raise ValueError("alias vacío")
    if not path:
        raise ValueError("ruta vacía")
    if not os.path.isdir(path):
        raise ValueError(f"la ruta no existe o no es directorio: {path}")
    _runtime_roots[alias] = path
    _generation += 1
    return merged_roots()


LOADED_ENV_FILES: list[Path] = load_env_files()
