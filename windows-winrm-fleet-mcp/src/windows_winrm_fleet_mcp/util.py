"""Utilidades: tamaños, fechas, validación y recorte de respuestas."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime
from fnmatch import fnmatch
from typing import Any, Optional

from windows_winrm_fleet_mcp.config import get_settings

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)
_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)
_IPV6_RE = re.compile(r"^[\da-fA-F:]+$")

PROTECTED_PROCESS_NAMES = frozenset(
    {"csrss", "lsass", "wininit", "winlogon", "services", "smss", "System"}
)


def human_size(num: int) -> str:
    size = float(num)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(num)} B"


def add_human_sizes(obj: Any, suffix: str = "_human") -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = add_human_sizes(v, suffix)
            if k.endswith("_bytes") or k == "bytes" or k == "size_bytes":
                if isinstance(v, int):
                    out[k.replace("_bytes", suffix) if k.endswith("_bytes") else f"{k}{suffix}"] = (
                        human_size(v)
                    )
        return out
    if isinstance(obj, list):
        return [add_human_sizes(x, suffix) for x in obj]
    return obj


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


def path_matches_denylist(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("/", "\\")
    for pat in patterns:
        if fnmatch(normalized.lower(), pat.lower()):
            return True
    return False


def validate_network_target(target: str) -> str:
    t = target.strip()
    if not t:
        raise ValueError("target vacío")
    if _IPV4_RE.match(t) or (_IPV6_RE.match(t) and ":" in t):
        return t
    if _HOSTNAME_RE.match(t.rstrip(".")):
        return t.rstrip(".")
    raise ValueError(f"target no es hostname/IP válido: {target!r}")


def is_protected_pid(pid: int) -> bool:
    return pid <= 4


def is_protected_process(name: str) -> bool:
    base = name.split("\\")[-1].split(".")[0]
    return base.lower() in {n.lower() for n in PROTECTED_PROCESS_NAMES}


def to_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone().isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return value
