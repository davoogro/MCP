"""Utilidades puras: URIs, tamaños, fechas, binarios y recorte de respuestas."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\s*$")
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
    "pib": 1024**5,
}
_TEXT_BYTES = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}


def mask_secret_id(value: Optional[str]) -> Optional[str]:
    """Enmascara un access key: AKIA...XXXX. Nunca devuelve el valor completo."""
    if not value:
        return None
    if len(value) <= 8:
        return "..."
    return f"{value[:4]}...{value[-4:]}"


def human_size(num: int) -> str:
    """Tamaño legible en base 1024, por ejemplo ``12.4 MB``."""
    size = float(num)
    units = ("B", "KB", "MB", "GB", "TB", "PB", "EB")
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(num)} B"


def parse_size(value: Any) -> Optional[int]:
    """Acepta bytes (int/float) o textos ``10MB``, ``1.5 GB``. None si el valor está vacío."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"tamaño inválido: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("el tamaño no puede ser negativo")
        return int(value)
    match = _SIZE_RE.fullmatch(str(value))
    if not match:
        raise ValueError(f"tamaño inválido: {value!r}")
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"unidad de tamaño desconocida: {unit}")
    if number < 0:
        raise ValueError("el tamaño no puede ser negativo")
    return int(round(number * _SIZE_UNITS[unit]))


def parse_datetime(value: Any) -> datetime:
    """Fecha ISO-8601. Sin zona horaria se interpreta como UTC. Devuelve datetime con tz UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("fecha vacía")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = datetime.fromisoformat(text)
        else:
            if "T" not in text and " " in text:
                text = text.replace(" ", "T", 1)
            try:
                dt = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(f"fecha ISO inválida: {value}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(value: Optional[datetime]) -> Optional[str]:
    """Serializa un datetime a ISO-8601 UTC (``YYYY-MM-DDTHH:MM:SSZ``)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_s3_uri(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Si ``value`` es ``s3://bucket/clave`` devuelve ``(bucket, clave)``.

    Si no es una URI devuelve ``(None, None)``. La clave puede ser ``""``.
    """
    if value is None:
        return None, None
    text = str(value).strip()
    if not text.lower().startswith("s3://"):
        return None, None
    rest = text[5:]
    bucket, _sep, key = rest.partition("/")
    if not bucket or any(ch.isspace() for ch in bucket):
        return None, None
    return bucket, key


def looks_binary(data: bytes) -> bool:
    """True si hay bytes nulos o el fragmento no parece texto UTF-8 imprimible.

    El texto UTF-8 (acentos incluidos) no se marca como binario: primero se
    intenta decodificar y solo entonces se mira la proporción de controles.
    """
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:8192]
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        bad = sum(1 for byte in sample if byte not in _TEXT_BYTES)
        return (bad / len(sample)) > 0.30
    if not text:
        return False
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
    return (controls / len(text)) > 0.30


def extension_of(key: str) -> str:
    """Extensión final en minúsculas, sin punto. Vacío si no hay."""
    name = key.rstrip("/").rsplit("/", 1)[-1]
    if not name or (name.startswith(".") and name.count(".") == 1) or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def relative_name(key: str, prefix: str) -> str:
    if prefix and key.startswith(prefix):
        rel = key[len(prefix):]
    else:
        rel = key
    return rel[1:] if rel.startswith("/") else rel


def group_label(key: str, prefix: str, depth: int) -> str:
    """Subcarpeta relativa al prefix, hasta ``depth`` niveles. ``(raíz)`` si el objeto cuelga del prefix."""
    rel = relative_name(key, prefix)
    parts = [part for part in rel.split("/") if part]
    if len(parts) <= 1:
        return "(raíz)"
    return "/".join(parts[:-1][: max(1, depth)])


def clip_cell(value: Any, limit: int = 500) -> Any:
    """Valor JSON-serializable. Los textos largos se recortan."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value[:200]).hex()
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def clean_etag(etag: Optional[str]) -> Optional[str]:
    if etag is None:
        return None
    return etag.strip().strip('"')


def json_len(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def _collect_lists(obj: Any, found: list[list]) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_lists(value, found)
    elif isinstance(obj, list):
        found.append(obj)
        for value in obj:
            _collect_lists(value, found)


def _longest_string(obj: Any) -> Optional[tuple[Any, Any, str]]:
    best: Optional[tuple[Any, Any, str]] = None

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and (best is None or len(value) > len(best[2])):
                    best = (node, key, value)
                elif not isinstance(value, str):
                    walk(value)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                if isinstance(value, str) and (best is None or len(value) > len(best[2])):
                    best = (node, index, value)
                elif not isinstance(value, str):
                    walk(value)

    walk(obj)
    return best


def bound_response(payload: dict[str, Any], limit: Optional[int] = None) -> dict[str, Any]:
    """Recorta listas y textos si el JSON supera ``S3_MAX_RESPONSE_CHARS``."""
    if limit is None:
        from s3_readonly_mcp.config import get_settings

        limit = get_settings().max_response_chars
    if json_len(payload) <= limit:
        return payload
    data = copy.deepcopy(payload)
    data["response_truncated"] = True
    for _ in range(10000):
        if json_len(data) <= limit:
            return data
        lists: list[list] = []
        _collect_lists(data, lists)
        lists = [item for item in lists if item]
        if lists:
            target = max(lists, key=json_len)
            drop = max(1, len(target) // 2)
            del target[-drop:]
            continue
        found = _longest_string(data)
        if found is None:
            break
        parent, key, text = found
        if len(text) <= 20:
            break
        cut = len(text) // 2
        parent[key] = (text[:cut] + "…") if cut else ""
    return data
