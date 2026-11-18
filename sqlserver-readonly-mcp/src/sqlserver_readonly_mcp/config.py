"""Configuración de sqlserver-readonly-mcp (SQL Server on-prem con autenticación SQL).

Variables (en ``sqlserver-readonly-mcp/.env`` o en el ``.env`` compartido de C:\\DVB\\mcp):

  MSSQL_USER / MSSQL_PASS         credenciales SQL
  MSSQL_SERVERS                  alias=host,puerto;alias2=host,puerto
  MSSQL_DEFAULT_CONNECTION       alias por defecto
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # C:\DVB\mcp\sqlserver-readonly-mcp

DEFAULT_SERVERS = "sql01=192.0.2.61,49152;sql05=192.0.2.65,49152"
DEFAULT_EXPORT_DIR = Path(r"C:\DVB\exports\mssql")


def load_env_files() -> list[Path]:
    """Carga .env propio, el compartido de C:\\DVB\\mcp y MSSQL_MCP_ENV_FILE (sin sobreescribir)."""
    candidates: list[Path] = []
    explicit = os.environ.get("MSSQL_MCP_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates += [PACKAGE_DIR / ".env", PACKAGE_DIR.parent / ".env"]
    loaded = []
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


def _nonempty(name: str) -> str:
    return os.environ.get(name, "").strip()


def pick_s3_credentials() -> tuple[str, str, str]:
    """Precedencia igual que s3-readonly-mcp: S3_* > access_key/secret_key > AWS_*."""
    if _nonempty("S3_ACCESS_KEY_ID") or _nonempty("S3_SECRET_ACCESS_KEY"):
        return _nonempty("S3_ACCESS_KEY_ID"), _nonempty("S3_SECRET_ACCESS_KEY"), _nonempty("S3_SESSION_TOKEN")
    if _nonempty("access_key") or _nonempty("secret_key"):
        return _nonempty("access_key"), _nonempty("secret_key"), ""
    return (
        _nonempty("AWS_ACCESS_KEY_ID"),
        _nonempty("AWS_SECRET_ACCESS_KEY"),
        _nonempty("AWS_SESSION_TOKEN"),
    )


@dataclass(frozen=True)
class Server:
    alias: str
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host},{self.port}"


def parse_servers(raw: str) -> dict[str, Server]:
    """``sql01=192.0.2.61,49152;sql05=host`` -> {alias: Server}. Puerto por defecto 1433."""
    servers: dict[str, Server] = {}
    for chunk in (raw or "").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Entrada de MSSQL_SERVERS inválida (falta alias=): {chunk!r}")
        alias, addr = (p.strip() for p in chunk.split("=", 1))
        host, _, port = addr.partition(",")
        if not alias or not host.strip():
            raise ValueError(f"Entrada de MSSQL_SERVERS inválida: {chunk!r}")
        try:
            port_num = int(port.strip()) if port.strip() else 1433
        except ValueError:
            raise ValueError(f"Puerto inválido en MSSQL_SERVERS: {chunk!r}") from None
        servers[alias] = Server(alias=alias, host=host.strip(), port=port_num)
    return servers


@dataclass(frozen=True)
class Settings:
    user: str
    password: str
    servers: dict[str, Server]
    default_connection: str
    driver: str
    encrypt: str
    trust_cert: str
    login_timeout: int
    query_timeout: int
    max_rows: int
    cursor_max_rows: int
    max_cell_chars: int
    max_response_chars: int
    export_dir: Path
    export_max_rows: int
    s3_region: str

    @classmethod
    def from_env(cls) -> "Settings":
        servers = parse_servers(os.environ.get("MSSQL_SERVERS", "").strip() or DEFAULT_SERVERS)
        default = os.environ.get("MSSQL_DEFAULT_CONNECTION", "").strip()
        if default not in servers:
            default = next(iter(servers), "")
        export_raw = os.environ.get("MSSQL_EXPORT_DIR", "").strip()
        export_dir = Path(export_raw) if export_raw else DEFAULT_EXPORT_DIR
        return cls(
            user=os.environ.get("MSSQL_USER", "").strip(),
            password=os.environ.get("MSSQL_PASS", ""),
            servers=servers,
            default_connection=default,
            driver=os.environ.get("MSSQL_DRIVER", "").strip() or "ODBC Driver 18 for SQL Server",
            encrypt=os.environ.get("MSSQL_ENCRYPT", "").strip() or "yes",
            trust_cert=os.environ.get("MSSQL_TRUST_CERT", "").strip() or "yes",
            login_timeout=_env_int("MSSQL_LOGIN_TIMEOUT", 10),
            query_timeout=_env_int("MSSQL_QUERY_TIMEOUT", 120),
            max_rows=_env_int("MSSQL_MAX_ROWS", 5000),
            cursor_max_rows=_env_int("MSSQL_CURSOR_MAX_ROWS", 1_000_000),
            max_cell_chars=_env_int("MSSQL_MAX_CELL_CHARS", 4000),
            max_response_chars=_env_int("MSSQL_MAX_RESPONSE_CHARS", 80000),
            export_dir=export_dir,
            export_max_rows=_env_int("MSSQL_EXPORT_MAX_ROWS", 50_000_000),
            s3_region=os.environ.get("S3_REGION", "").strip() or "us-east-1",
        )

    def problems(self) -> list[str]:
        out = []
        if not self.user:
            out.append("MSSQL_USER")
        if not self.password:
            out.append("MSSQL_PASS")
        if not self.servers:
            out.append("MSSQL_SERVERS")
        return out

    def masked_user(self) -> str:
        u = self.user
        return u if len(u) <= 3 else u[:2] + "*" * (len(u) - 3) + u[-1]
