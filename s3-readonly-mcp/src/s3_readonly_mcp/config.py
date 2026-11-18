"""Configuración del servidor S3 MCP.

Carga ``s3-readonly-mcp/.env``, después el ``.env`` compartido de ``C:\\DVB\\mcp``
(``override=False``) y, si existe, el archivo de ``S3_MCP_ENV_FILE``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from s3_readonly_mcp.util import mask_secret_id

PACKAGE_DIR = Path(__file__).resolve().parents[2]  # C:\DVB\mcp\s3-readonly-mcp


def load_env_files() -> list[Path]:
    """Carga .env propio y, como fallback, el .env compartido de C:\\DVB\\mcp.

    Nunca sobreescribe variables ya presentes en el entorno del proceso.
    ``S3_MCP_ENV_FILE`` se carga primero si está definido.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("S3_MCP_ENV_FILE", "").strip()
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


def _pick_credentials() -> tuple[str, str, str]:
    """Precedencia: S3_* > access_key/secret_key > AWS_*.

    El session token de AWS solo se usa con el par AWS_*, para no mezclar
    credenciales distintas. ``S3_SESSION_TOKEN`` acompaña al par S3_*.
    """
    if _nonempty("S3_ACCESS_KEY_ID") or _nonempty("S3_SECRET_ACCESS_KEY"):
        return _nonempty("S3_ACCESS_KEY_ID"), _nonempty("S3_SECRET_ACCESS_KEY"), _nonempty("S3_SESSION_TOKEN")
    if _nonempty("access_key") or _nonempty("secret_key"):
        return _nonempty("access_key"), _nonempty("secret_key"), ""
    return (
        _nonempty("AWS_ACCESS_KEY_ID"),
        _nonempty("AWS_SECRET_ACCESS_KEY"),
        _nonempty("AWS_SESSION_TOKEN"),
    )


def _parse_buckets(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, repr=False)
class Settings:
    region: str
    endpoint_url: Optional[str]
    buckets: tuple[str, ...]
    allowlist_only: bool
    default_bucket: str
    max_scan_objects: int
    max_read_bytes: int
    max_parquet_read_bytes: int
    max_count_bytes: int
    max_query_bytes: int
    max_download_bytes: int
    max_query_rows: int
    max_response_chars: int
    access_key_id: str
    secret_access_key: str
    session_token: str

    @classmethod
    def from_env(cls) -> "Settings":
        access, secret, token = _pick_credentials()
        buckets = _parse_buckets(os.environ.get("S3_BUCKETS", ""))
        default_bucket = _nonempty("S3_DEFAULT_BUCKET") or (buckets[0] if buckets else "")
        endpoint = _nonempty("S3_ENDPOINT_URL") or None
        return cls(
            region=_nonempty("S3_REGION") or "us-east-1",
            endpoint_url=endpoint,
            buckets=buckets,
            allowlist_only=_env_bool("S3_BUCKET_ALLOWLIST_ONLY", False),
            default_bucket=default_bucket,
            max_scan_objects=max(1, _env_int("S3_MAX_SCAN_OBJECTS", 200_000)),
            max_read_bytes=max(1, _env_int("S3_MAX_READ_BYTES", 20 * 1024 * 1024)),
            max_parquet_read_bytes=max(1, _env_int("S3_MAX_PARQUET_READ_BYTES", 256 * 1024 * 1024)),
            max_count_bytes=max(1, _env_int("S3_MAX_COUNT_BYTES", 2 * 1024 * 1024 * 1024)),
            max_query_bytes=max(1, _env_int("S3_QUERY_MAX_BYTES", 2 * 1024 * 1024 * 1024)),
            max_download_bytes=max(1, _env_int("S3_MAX_DOWNLOAD_BYTES", 5 * 1024 * 1024 * 1024)),
            max_query_rows=max(1, min(_env_int("S3_MAX_QUERY_ROWS", 5000), 5000)),
            max_response_chars=max(1, _env_int("S3_MAX_RESPONSE_CHARS", 80_000)),
            access_key_id=access,
            secret_access_key=secret,
            session_token=token,
        )

    @property
    def credentials_configured(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)

    def masked_access_key(self) -> Optional[str]:
        return mask_secret_id(self.access_key_id)

    def public_dict(self) -> dict:
        """Configuración visible, sin secretos ni access key completo."""
        return {
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "buckets": list(self.buckets),
            "default_bucket": self.default_bucket or None,
            "allowlist_only": self.allowlist_only,
            "max_scan_objects": self.max_scan_objects,
            "max_read_bytes": self.max_read_bytes,
            "max_parquet_read_bytes": self.max_parquet_read_bytes,
            "max_count_bytes": self.max_count_bytes,
            "max_query_bytes": self.max_query_bytes,
            "max_download_bytes": self.max_download_bytes,
            "max_query_rows": self.max_query_rows,
            "max_response_chars": self.max_response_chars,
            "access_key_masked": self.masked_access_key(),
            "credentials_configured": self.credentials_configured,
            "has_session_token": bool(self.session_token),
            "env_files": [str(path) for path in LOADED_ENV_FILES],
        }

    def __repr__(self) -> str:
        return (
            f"Settings(region={self.region!r}, endpoint_url={self.endpoint_url!r}, "
            f"buckets={list(self.buckets)!r}, default_bucket={self.default_bucket!r}, "
            f"allowlist_only={self.allowlist_only!r}, access_key={self.masked_access_key()!r})"
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
    _settings = Settings.from_env()
    _generation += 1
    return _settings


def settings_generation() -> int:
    get_settings()
    return _generation


LOADED_ENV_FILES: list[Path] = load_env_files()
