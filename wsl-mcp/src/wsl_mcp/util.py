"""Utilidades: rutas Linux, truncado y decodificación de salida WSL."""

from __future__ import annotations

import shlex
import sys
from typing import Any, Optional

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def err(msg: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, **extra}


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    if "ok" not in payload:
        payload = {"ok": True, **payload}
    return payload


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    head = limit * 2 // 5
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [truncado: {omitted} chars omitidos] ...\n{text[-tail:]}", True


def decode_wsl_list(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace")
    if b"\x00" in raw[:64]:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def decode_output(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:64]:
        return decode_wsl_list(raw).replace("\x00", "")
    return raw.decode("utf-8", errors="replace")


def linux_path(path: str) -> str:
    value = path.strip()
    if not value:
        return value
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha() and (
        len(value) == 2 or value[2] in ("/", "\\")
    ):
        drive = value[0].lower()
        rest = value[3:].replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return value.replace("\\", "/")


def shell_path(path: str) -> str:
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        rest = path[2:]
        return '"$HOME"/' + shlex.quote(rest) if rest else '"$HOME"/'
    return shlex.quote(path)


def clamp_timeout(timeout: Optional[int], default: int, maximum: int) -> int:
    if timeout is None or int(timeout) <= 0:
        return max(1, default)
    return max(1, min(int(timeout), maximum))
