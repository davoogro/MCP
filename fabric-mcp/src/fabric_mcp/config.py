"""Configuración multi-conexión para Microsoft Fabric SQL.

Las conexiones se descubren desde variables de entorno con prefijo ``FABRIC<n>_``:

  FABRIC_SERVER / FABRIC_USER / FABRIC_PASSWORD ...    -> conexión "default"
  FABRIC2_SERVER / FABRIC2_USER / FABRIC2_PASSWORD ... -> conexión "fabric2"
  FABRIC3_...                                          -> conexión "fabric3"

Así se reutiliza tal cual el ``.env`` que ya usaban fabric_mcp_server.py y
fabric_mcp_server2.py en Cursor. ``FABRIC<n>_NAME`` permite un alias amigable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # C:\DVB\mcp\fabric-mcp
DEFAULT_EXPORT_DIR = Path(r"C:\DVB\exports\fabric")

_FABRIC_SQL_SUFFIX = ".datawarehouse.fabric.microsoft.com"
_SERVER_VAR_RE = re.compile(r"^FABRIC(\d*)_SERVER$")

# Métodos de autenticación soportados por ODBC Driver 18 que tienen sentido aquí.
AUTH_METHODS = {
    "ActiveDirectoryPassword": ("user", "password"),
    "ActiveDirectoryServicePrincipal": ("user", "password"),  # user=client_id, password=secret
    "ActiveDirectoryInteractive": (),  # abre navegador; user opcional como login_hint
    "ActiveDirectoryIntegrated": (),
    "ActiveDirectoryMsi": (),
}


def load_env_files() -> list[Path]:
    """Carga .env propio y, como fallback, el .env compartido de C:\\DVB\\mcp.

    Nunca sobreescribe variables ya presentes en el entorno del proceso.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("FABRIC_MCP_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates += [PACKAGE_DIR / ".env", PACKAGE_DIR.parent / ".env"]
    loaded = []
    for path in candidates:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            loaded.append(path)
    return loaded


def normalize_fabric_server(server: str) -> str:
    """Normaliza hosts de Fabric pegados desde portales o connection strings."""
    value = (server or "").strip()
    if value.lower().startswith("tcp:"):
        value = value[4:]
    if "," in value:
        value = value.split(",", 1)[0].strip()
    return re.sub(rf"(?i)({re.escape(_FABRIC_SQL_SUFFIX)})+$", _FABRIC_SQL_SUFFIX, value)


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


def pick_s3_credentials() -> tuple[str, str, str]:
    """Precedencia: S3_* > access_key/secret_key > AWS_* (mismo .env compartido C:\\DVB\\mcp)."""
    if _nonempty("S3_ACCESS_KEY_ID") or _nonempty("S3_SECRET_ACCESS_KEY"):
        return _nonempty("S3_ACCESS_KEY_ID"), _nonempty("S3_SECRET_ACCESS_KEY"), _nonempty("S3_SESSION_TOKEN")
    if _nonempty("access_key") or _nonempty("secret_key"):
        return _nonempty("access_key"), _nonempty("secret_key"), ""
    return (
        _nonempty("AWS_ACCESS_KEY_ID"),
        _nonempty("AWS_SECRET_ACCESS_KEY"),
        _nonempty("AWS_SESSION_TOKEN"),
    )


@dataclass
class Connection:
    name: str
    prefix: str
    server: str
    database: str
    port: str
    username: str
    password: str
    auth: str
    odbc_driver: str
    readonly: bool
    query_timeout: int
    cache_ttl: int
    pooling: bool

    @classmethod
    def from_prefix(cls, prefix: str, default_name: str) -> "Connection":
        def get(key: str, default: str = "") -> str:
            return os.environ.get(f"{prefix}{key}", default).strip()

        return cls(
            name=get("NAME") or default_name,
            prefix=prefix,
            server=normalize_fabric_server(get("SERVER")),
            database=get("DATABASE") or "master",
            port=get("PORT") or "1433",
            username=get("USER"),
            password=os.environ.get(f"{prefix}PASSWORD", ""),
            auth=get("AUTH") or "ActiveDirectoryPassword",
            odbc_driver=get("DRIVER"),
            readonly=_env_bool(f"{prefix}READONLY", True),
            query_timeout=_env_int(f"{prefix}QUERY_TIMEOUT", 120),
            cache_ttl=_env_int(f"{prefix}CACHE_TTL", 0),
            pooling=_env_bool(f"{prefix}POOL", True),
        )

    def problems(self) -> list[str]:
        out = []
        if not self.server:
            out.append(f"{self.prefix}SERVER")
        if self.auth not in AUTH_METHODS:
            out.append(f"{self.prefix}AUTH inválido ({self.auth}); usa uno de {sorted(AUTH_METHODS)}")
            return out
        required = AUTH_METHODS[self.auth]
        if "user" in required and not self.username:
            out.append(f"{self.prefix}USER")
        if "password" in required and not self.password:
            out.append(f"{self.prefix}PASSWORD")
        return out

    def public_info(self) -> dict:
        """Descripción sin secretos."""
        return {
            "name": self.name,
            "env_prefix": self.prefix,
            "server": self.server,
            "default_database": self.database,
            "user": self.username,
            "auth": self.auth,
            "readonly": self.readonly,
            "query_timeout_s": self.query_timeout,
            "cache_ttl_s": self.cache_ttl,
            "configured": not self.problems(),
            "missing": self.problems(),
        }


def discover_connections() -> dict[str, Connection]:
    """Descubre todas las conexiones FABRIC<n>_SERVER definidas en el entorno."""
    found: list[tuple[int, Connection]] = []
    for var in os.environ:
        m = _SERVER_VAR_RE.match(var.upper())
        if not m or not os.environ[var].strip():
            continue
        idx = m.group(1)
        prefix = f"FABRIC{idx}_"
        default_name = "default" if idx in ("", "1") else f"fabric{idx}"
        found.append((int(idx or 1), Connection.from_prefix(prefix, default_name)))
    found.sort(key=lambda t: t[0])
    conns: dict[str, Connection] = {}
    for _, conn in found:
        conns[conn.name] = conn
    return conns


@dataclass(frozen=True)
class Settings:
    default_connection: str
    max_cell_chars: int
    max_rows: int
    max_response_chars: int
    cursor_max_rows: int
    export_dir: Path
    export_max_rows: int
    s3_region: str

    @classmethod
    def from_env(cls) -> "Settings":
        export_raw = os.environ.get("FABRIC_EXPORT_DIR", "").strip()
        export_dir = Path(export_raw) if export_raw else DEFAULT_EXPORT_DIR
        return cls(
            default_connection=os.environ.get("FABRIC_MCP_DEFAULT", "").strip(),
            max_cell_chars=_env_int("FABRIC_MCP_MAX_CELL_CHARS", 4000),
            max_rows=_env_int("FABRIC_MCP_MAX_ROWS", 5000),
            max_response_chars=_env_int("FABRIC_MCP_MAX_RESPONSE_CHARS", 80_000),
            cursor_max_rows=_env_int("FABRIC_MCP_CURSOR_MAX_ROWS", 1_000_000),
            export_dir=export_dir,
            export_max_rows=_env_int("FABRIC_EXPORT_MAX_ROWS", 50_000_000),
            s3_region=os.environ.get("S3_REGION", "").strip() or "us-east-1",
        )
