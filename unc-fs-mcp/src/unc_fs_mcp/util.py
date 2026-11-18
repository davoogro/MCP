"""Utilidades: resolución de rutas UNC, tamaños, fechas, redacción y recorte de respuestas."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, NamedTuple, Optional

from unc_fs_mcp.config import get_settings, merged_roots

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
}
_TEXT_BYTES = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D}

_SECRET_KEY_RE = re.compile(
    r"(pass|pwd|secret|token|key|credential)",
    re.IGNORECASE,
)
_ENV_LINE_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*=\s*(.*)$")
_YAML_LINE_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_JSON_KV_RE = re.compile(r'^(\s*)"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$')

_REDACT_SUFFIXES = (".env", ".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml", ".json")


class ResolvedPath(NamedTuple):
    path: str
    display: Optional[str]
    root_alias: Optional[str]


class PathError(ValueError):
    """Error de resolución de ruta con código estándar."""

    def __init__(self, message: str, code: str = "InvalidPath") -> None:
        super().__init__(message)
        self.code = code


def human_size(num: int) -> str:
    """Tamaño legible en base 1024, por ejemplo ``12.4 MB``."""
    size = float(num)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(num)} B"


def parse_size(value: Any) -> Optional[int]:
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
    return int(round(number * _SIZE_UNITS[unit]))


def parse_datetime(value: Any) -> datetime:
    """Fecha ISO o ``YYYY-MM-DD``. Sin zona se interpreta como hora local."""
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
            dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.astimezone()


def to_iso_local(value: Optional[float | datetime]) -> Optional[str]:
    """Serializa timestamp o datetime a ISO-8601 con zona local."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromtimestamp(value)
    local = dt.astimezone()
    return local.isoformat(timespec="seconds")


def path_parts_lower(path: str) -> tuple[str, ...]:
    normalized = os.path.normpath(path.replace("/", "\\"))
    parts = Path(normalized).parts
    return tuple(p.lower() for p in parts)


def is_under_root(resolved: str, root: str) -> bool:
    """Comparación por componentes, sin distinguir mayúsculas."""
    child = path_parts_lower(resolved)
    parent = path_parts_lower(root)
    if len(child) < len(parent):
        return False
    return child[: len(parent)] == parent


def _reject_device_paths(text: str) -> None:
    upper = text.replace("/", "\\")
    if upper.startswith("\\\\?\\") or upper.startswith("\\\\.\\"):
        raise PathError("ruta de dispositivo no permitida", "InvalidPath")


def _is_unc(text: str) -> bool:
    t = text.replace("/", "\\")
    return t.startswith("\\\\") and not t.startswith("\\\\?\\") and not t.startswith("\\\\.\\")


def _is_local_drive(text: str) -> bool:
    t = text.replace("/", "\\")
    return len(t) >= 2 and t[1] == ":" and t[0].isalpha()


def _join_under_root(root: str, relative: str) -> str:
    root_norm = os.path.normpath(root.replace("/", "\\"))
    rel_norm = os.path.normpath(relative.replace("/", "\\")) if relative else ""
    if rel_norm in ("", "."):
        combined = root_norm
    elif os.path.isabs(rel_norm):
        raise PathError("la ruta relativa no puede ser absoluta", "InvalidPath")
    else:
        combined = os.path.normpath(os.path.join(root_norm, rel_norm))
    if not is_under_root(combined, root_norm):
        raise PathError("la ruta escapa de la raíz permitida", "InvalidPath")
    return combined


def _check_allowlist(resolved: str, settings) -> None:
    roots = merged_roots()
    if settings.allowlist_only:
        if not roots:
            raise PathError("allowlist activo pero no hay raíces configuradas", "NotAllowed")
        for root_path in roots.values():
            if is_under_root(resolved, root_path):
                return
        raise PathError("ruta fuera de las raíces permitidas", "NotAllowed")
    if _is_unc(resolved) and not settings.allow_any_unc:
        if not roots:
            raise PathError("UNC restringido pero no hay raíces configuradas", "NotAllowed")
        for root_path in roots.values():
            if is_under_root(resolved, root_path):
                return
        raise PathError("ruta UNC fuera de las raíces configuradas (UNC_ALLOW_ANY_UNC=false)", "NotAllowed")


def _check_local_allowed(resolved: str, settings) -> None:
    if _is_local_drive(resolved) and not settings.allow_local:
        raise PathError("rutas locales no permitidas (UNC_ALLOW_LOCAL=false)", "NotAllowed")


def resolve_path(path: Optional[str] = None, root: Optional[str] = None) -> ResolvedPath:
    """Resuelve alias, UNC, relativa o local según configuración."""
    settings = get_settings()
    root_alias: Optional[str] = None
    base_root: Optional[str] = None

    roots = merged_roots()

    if root:
        root = root.strip()
        if root in roots:
            root_alias = root
            base_root = roots[root]
        else:
            base_root = root

    if path is None or str(path).strip() == "":
        alias = root_alias or settings.default_root
        if alias and alias in roots:
            resolved = os.path.normpath(roots[alias].replace("/", "\\"))
            return ResolvedPath(path=resolved, display=f"{alias}:", root_alias=alias)
        if base_root:
            resolved = os.path.normpath(base_root.replace("/", "\\"))
            return ResolvedPath(path=resolved, display=None, root_alias=root_alias)
        raise PathError("no hay ruta ni raíz por defecto", "InvalidPath")

    text = str(path).strip().replace("/", "\\")
    _reject_device_paths(text)

    if ":" in text and not text.startswith("\\\\"):
        alias_part, _, rest = text.partition(":")
        if alias_part in roots and (len(text) > len(alias_part) + 1 or text.endswith(":")):
            root_alias = alias_part
            base_root = roots[alias_part]
            rel = rest.lstrip("\\/")
            resolved = _join_under_root(base_root, rel)
            rel_disp = rel.replace("\\", "/") if rel else ""
            display = f"{alias_part}:{rel_disp}" if rel else f"{alias_part}:"
            _check_local_allowed(resolved, settings)
            _check_allowlist(resolved, settings)
            return ResolvedPath(path=resolved, display=display, root_alias=alias_part)

    if _is_unc(text):
        resolved = os.path.normpath(text)
        _check_local_allowed(resolved, settings)
        _check_allowlist(resolved, settings)
        return ResolvedPath(path=resolved, display=None, root_alias=None)

    if _is_local_drive(text):
        resolved = os.path.normpath(text)
        _check_local_allowed(resolved, settings)
        _check_allowlist(resolved, settings)
        return ResolvedPath(path=resolved, display=None, root_alias=None)

    alias = root_alias or settings.default_root
    if alias and alias in roots:
        base = roots[alias]
        resolved = _join_under_root(base, text)
        display = f"{alias}:{text.replace(chr(92), '/')}"
        _check_local_allowed(resolved, settings)
        _check_allowlist(resolved, settings)
        return ResolvedPath(path=resolved, display=display, root_alias=alias)

    if base_root:
        resolved = _join_under_root(base_root, text)
        _check_local_allowed(resolved, settings)
        _check_allowlist(resolved, settings)
        return ResolvedPath(path=resolved, display=text, root_alias=root_alias)

    raise PathError("no se pudo resolver la ruta", "InvalidPath")


def looks_binary(data: bytes) -> bool:
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
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
    return (controls / len(text)) > 0.30 if text else False


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def decode_bytes(raw: bytes, encoding: Optional[str] = None) -> tuple[str, str]:
    if encoding:
        return raw.decode(encoding), encoding
    enc = detect_encoding(raw)
    if enc == "utf-8-sig":
        return raw.decode("utf-8-sig"), enc
    return raw.decode(enc), enc


def should_redact_filename(name: str) -> bool:
    lower = name.lower()
    if lower.startswith(".env"):
        return True
    return any(lower.endswith(suffix) for suffix in _REDACT_SUFFIXES)


def redact_secrets_text(text: str) -> tuple[str, bool]:
    """Redacta valores sensibles en líneas tipo env/yaml/json."""
    changed = False
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        new_line = line
        line_redacted = False
        for regex in (_ENV_LINE_RE, _YAML_LINE_RE):
            m = regex.match(line.rstrip("\r\n"))
            if m and _SECRET_KEY_RE.search(m.group(2)):
                prefix, key, _val = m.group(1), m.group(2), m.group(3)
                sep = "=" if regex is _ENV_LINE_RE else ":"
                eol = line[len(line.rstrip("\r\n")) :]
                new_line = f"{prefix}{key}{sep} ***{eol}"
                line_redacted = True
                break
        if not line_redacted:
            m = _JSON_KV_RE.match(line.rstrip("\r\n"))
            if m and _SECRET_KEY_RE.search(m.group(2)):
                eol = line[len(line.rstrip("\r\n")) :]
                new_line = f'{m.group(1)}"{m.group(2)}": "***"{eol}'
                line_redacted = True
        if line_redacted:
            changed = True
        out_lines.append(new_line)
    return "".join(out_lines), changed


def glob_match(name: str, pattern: str) -> bool:
    return fnmatch(name.lower(), pattern.lower())


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
    if limit is None:
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


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    if domain and user:
        return f"{domain}\\{user}"
    return user or domain or "unknown"


def extension_of(name: str) -> str:
    if not name or (name.startswith(".") and name.count(".") == 1) or "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def clip_cell(value: Any, limit: int = 500) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return to_iso_local(value)
    if isinstance(value, date):
        return value.isoformat()
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text
