"""Utilidades de respuesta, límites y manejo seguro de errores."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp.types import CallToolResult, TextContent

from nifi_readonly_mcp.exceptions import ConfigError, DebugEndpointDenied, NiFiClientError, NiFiMcpError

logger = logging.getLogger(__name__)

EXPECTED_ERRORS = (NiFiMcpError, ConfigError, DebugEndpointDenied, NiFiClientError, KeyError, ValueError)

# Claude Code avisa a partir de ~10k tokens y corta en ~25k por defecto: ~80k caracteres
# de JSON compacto es un tope prudente. Configurable con NIFI_MCP_MAX_RESPONSE_CHARS.
DEFAULT_MAX_RESPONSE_CHARS = 80_000


def max_response_chars() -> int:
    try:
        return max(5_000, int(os.environ.get("NIFI_MCP_MAX_RESPONSE_CHARS", DEFAULT_MAX_RESPONSE_CHARS)))
    except ValueError:
        return DEFAULT_MAX_RESPONSE_CHARS


def validate_limit(limit: int | None, *, default: int, max_limit: int = 10_000) -> int:
    if limit is None:
        return default
    if limit < 1:
        raise ConfigError(f"limit debe ser >= 1, recibido: {limit}")
    if limit > max_limit:
        raise ConfigError(f"limit excede máximo ({max_limit}): {limit}")
    return limit


def safe_error_message(exc: Exception) -> str:
    if isinstance(exc, NiFiMcpError):
        return str(exc)
    if isinstance(exc, KeyError):
        return f"Recurso no encontrado: {exc}"
    if isinstance(exc, ValueError):
        return str(exc)
    return "Operación fallida; revise logs del servidor MCP"


def tool_error(msg: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": msg}
    payload.update(extra)
    return payload


def to_compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


_SHORT_LIST = 10


def _largest_list(node: Any) -> tuple[list[Any] | None, str, int]:
    """Elige la lista a recortar: (lista, ruta, tamaño serializado).

    Se prefiere la lista más pesada con más de ``_SHORT_LIST`` elementos, para no
    descartar servidores enteros de ``results[]`` cuando lo que pesa son sus
    ``findings``/``items`` anidados. Si no hay ninguna larga, la más pesada.
    """
    found: list[tuple[int, str, list[Any]]] = []

    def walk(n: Any, path: str) -> None:
        if isinstance(n, dict):
            for k, v in n.items():
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}.{k}")
        elif isinstance(n, list):
            if len(n) > 1:
                found.append((len(to_compact_json(n)), path, n))
            for i, v in enumerate(n):
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}[{i}]")

    walk(node, "$")
    if not found:
        return None, "", 0
    long_lists = [f for f in found if len(f[2]) > _SHORT_LIST]
    size, path, lst = max(long_lists or found, key=lambda f: f[0])
    return lst, path, size


def fit_payload(payload: dict[str, Any], max_chars: int | None = None) -> str:
    """JSON compacto; si excede ``max_chars`` recorta las listas más pesadas.

    Conserva los primeros elementos de cada lista recortada y añade
    ``response_truncated=true`` + ``response_trimmed`` (ruta: antes→después).
    """
    limit = max_chars or max_response_chars()
    text = to_compact_json(payload)
    if len(text) <= limit:
        return text

    data = json.loads(text)
    trimmed: dict[str, str] = {}
    data["response_truncated"] = True
    data["response_note"] = (
        f"Respuesta recortada a ~{limit} caracteres. Usa limit menor, filtros "
        "(server_id, process_group_id, name/state) o herramientas de resumen."
    )
    for _ in range(40):
        text = to_compact_json(data)
        if len(text) <= limit:
            break
        lst, lpath, lsize = _largest_list(data)
        if lst is None:
            break
        # Conservar el prefijo más largo que quepa en el presupuesto restante.
        budget = lsize - (len(text) - limit)
        keep, used = 0, 2
        for item in lst:
            used += len(to_compact_json(item)) + 1
            if used > budget:
                break
            keep += 1
        keep = min(len(lst) - 1, max(1, keep))
        before = trimmed.get(lpath, "").split("→")[0] or str(len(lst))
        del lst[keep:]
        trimmed[lpath] = f"{before}→{len(lst)}"
        data["response_trimmed"] = trimmed
        if lpath == "$.items":
            data["returned"] = len(lst)
            data["truncated"] = True
    text = to_compact_json(data)
    if len(text) <= limit:
        return text
    return to_compact_json(
        {
            "ok": data.get("ok", False),
            "response_truncated": True,
            "response_note": data["response_note"],
            "preview": text[: limit - 400],
        }
    )


def as_tool_result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    """Texto JSON compacto (para el LLM) + structuredContent con el payload completo."""
    text = fit_payload(payload)
    # structuredContent = lo mismo que el texto (ya acotado), nunca el payload sin recortar.
    structured = json.loads(text)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
        is_error=is_error,
    )


async def run_tool(coro_factory, *, context: str, **context_fields: Any) -> CallToolResult:
    try:
        result = await coro_factory()
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        return as_tool_result(result)
    except EXPECTED_ERRORS as exc:
        logger.warning("%s: %s", context, safe_error_message(exc))
        return as_tool_result(tool_error(safe_error_message(exc), **context_fields), is_error=True)
    except Exception:
        logger.exception("%s: error inesperado", context)
        return as_tool_result(
            tool_error("Operación fallida; revise logs del servidor MCP", **context_fields),
            is_error=True,
        )
