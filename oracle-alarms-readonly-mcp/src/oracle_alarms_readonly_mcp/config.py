"""Configuración de collectors OSS FM y variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

# Tope explícito del parámetro max_rows en tools (por encima del default de env).
PER_CALL_MAX_ROWS = 20_000
PER_CALL_MAX_TIMEOUT_S = 300

DEFAULT_EXPORT_DIR = Path(r"C:\DVB\exports\oss")

# Raíz del proyecto (…/oracle-alarms-readonly-mcp), resuelta desde este archivo y no desde el
# cwd: el .env se encuentra aunque el cliente MCP lance el proceso desde otro directorio.
# ORACLE_FM_ENV_FILE permite apuntar a otro .env. Las variables ya presentes en el
# entorno del proceso tienen prioridad (override=False).
BASE_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("ORACLE_FM_ENV_FILE") or (BASE_DIR / ".env")).expanduser()
load_dotenv(dotenv_path=ENV_FILE, override=False)

# Puertos versionados (no son secretos). Host/service/user/password vienen de env.
RC_PORTS: dict[str, int] = {
    "rc1": 8001,
    "rc2": 8002,
    "rc7": 8006,
    "rc8": 8008,
    "rc9": 8009,
    "rc10": 8004,
}

ALL_RCS: tuple[str, ...] = tuple(RC_PORTS.keys())

SEVERITY_LABELS: dict[int, str] = {
    1: "critical",
    2: "major",
    3: "minor",
    4: "warning",
}

# Hora local de red OSS: UTC-5 fijo, sin DST. No se convierte; solo se documenta.
NETWORK_TZ_OFFSET = "-05:00"
NETWORK_TZ_LABEL = "UTC-5 (fijo, sin horario de verano)"


@dataclass(frozen=True)
class Settings:
    user: str
    password: str
    host: str
    service: str
    max_rows: int  # techo duro de filas por RC
    query_timeout_s: int
    rc8_chunk_days: int
    default_rows: int = 500  # filas por RC cuando la llamada no indica max_rows
    max_response_chars: int = 80_000  # tope de tamaño del JSON devuelto al cliente MCP
    export_dir: Path = DEFAULT_EXPORT_DIR

    @classmethod
    def from_env(cls) -> "Settings":
        user = os.environ.get("ORACLE_FM_USER", "").strip()
        password = os.environ.get("ORACLE_FM_PASSWORD", "")
        host = os.environ.get("ORACLE_FM_HOST", "192.0.2.204").strip()
        service = os.environ.get("ORACLE_FM_SERVICE", "OSS").strip()
        max_rows = int(os.environ.get("ORACLE_FM_MAX_ROWS", "5000"))
        timeout_s = int(os.environ.get("ORACLE_FM_QUERY_TIMEOUT_S", "30"))
        chunk_days = int(os.environ.get("ORACLE_FM_RC8_CHUNK_DAYS", "3"))
        default_rows = int(os.environ.get("ORACLE_FM_DEFAULT_ROWS", "500"))
        max_chars = int(os.environ.get("ORACLE_FM_MAX_RESPONSE_CHARS", "80000"))
        export_dir = Path(
            os.environ.get("ORACLE_FM_EXPORT_DIR", str(DEFAULT_EXPORT_DIR))
        ).expanduser()
        max_rows = max(1, max_rows)
        return cls(
            user=user,
            password=password,
            host=host,
            service=service,
            max_rows=max_rows,
            query_timeout_s=max(1, timeout_s),
            rc8_chunk_days=max(0, chunk_days),
            default_rows=min(max(1, default_rows), PER_CALL_MAX_ROWS),
            max_response_chars=max(5_000, max_chars),
            export_dir=export_dir,
        )

    def effective_rows(self, requested: int | None) -> int:
        """Filas por RC: default 500; parámetro max_rows hasta PER_CALL_MAX_ROWS."""
        if requested is None:
            return self.default_rows
        return min(max(1, int(requested)), PER_CALL_MAX_ROWS)

    def effective_timeout(self, requested: int | None) -> int:
        """Timeout por round-trip (s); tope PER_CALL_MAX_TIMEOUT_S."""
        if requested is None:
            return min(self.query_timeout_s, PER_CALL_MAX_TIMEOUT_S)
        return min(max(1, int(requested)), PER_CALL_MAX_TIMEOUT_S)

    def validate_credentials(self) -> None:
        missing = []
        if not self.user:
            missing.append("ORACLE_FM_USER")
        if not self.password:
            missing.append("ORACLE_FM_PASSWORD")
        if not self.host:
            missing.append("ORACLE_FM_HOST")
        if not self.service:
            missing.append("ORACLE_FM_SERVICE")
        if missing:
            raise RuntimeError(
                "Variables de entorno faltantes: "
                + ", ".join(missing)
                + f". Copiá .env.example → {ENV_FILE} y completá las credenciales."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings desde env, cacheados por proceso (una sola lectura del .env)."""
    return Settings.from_env()


def normalize_rc_name(rc: str) -> str:
    name = rc.strip().lower()
    if name.startswith("rc") and name[2:].isdigit():
        return name
    raise ValueError(
        f"RC inválido: '{rc}'. Esperados: {', '.join(ALL_RCS)} o 'all'."
    )


def resolve_rcs(rc: str | Iterable[str] | None) -> list[str]:
    """Acepta un RC, lista, o 'all'. Devuelve lista ordenada de nombres canónicos."""
    if rc is None:
        raise ValueError("Parámetro 'rc' requerido")

    if isinstance(rc, str):
        raw = rc.strip()
        if not raw:
            raise ValueError("Parámetro 'rc' vacío")
        if raw.lower() == "all":
            return list(ALL_RCS)
        # Permitir "rc1,rc2" como atajo
        if "," in raw:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            return resolve_rcs(parts)
        name = normalize_rc_name(raw)
        if name not in RC_PORTS:
            raise ValueError(
                f"RC desconocido: '{name}'. Conocidos: {', '.join(ALL_RCS)}"
            )
        return [name]

    names: list[str] = []
    seen: set[str] = set()
    for item in rc:
        for name in resolve_rcs(item):
            if name not in seen:
                seen.add(name)
                names.append(name)
    if not names:
        raise ValueError("Lista de RCs vacía")
    return names


def resolve_rcs_optional(rc: str | Iterable[str] | None) -> list[str]:
    """None o 'all' → los 6 RCs; en otro caso igual que resolve_rcs."""
    if rc is None:
        return list(ALL_RCS)
    return resolve_rcs(rc)


def dsn_for(rc: str, settings: Settings) -> str:
    name = normalize_rc_name(rc)
    if name not in RC_PORTS:
        raise ValueError(f"RC desconocido: '{name}'")
    port = RC_PORTS[name]
    return f"{settings.host}:{port}/{settings.service}"
