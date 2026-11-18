"""Formato de valores Oracle: hora de red UTC-5 sin conversión; severidad."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any

from oracle_alarms_readonly_mcp.config import SEVERITY_LABELS

# Columnas típicas de severidad en ALARMS.ALARM_LOG / vistas OSS
_SEVERITY_COLUMNS = frozenset(
    {
        "PERCEIVED_SEVERITY",
        "SEVERITY",
        "ALARM_SEVERITY",
        "PERCEIVEDSEVERITY",
    }
)


def _max_cell_chars() -> int:
    try:
        return max(100, int(os.environ.get("ORACLE_FM_MAX_CELL_CHARS", "4000")))
    except ValueError:
        return 4000


def _cap_text(text: str) -> str:
    limit = _max_cell_chars()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[+{len(text) - limit} chars truncados]"


def format_network_datetime(value: datetime) -> str:
    """Formatea sin convertir zona: 'YYYY-MM-DD HH:MM:SS' (hora de red UTC-5)."""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def serialize_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return _cap_text(val)
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, Decimal):
        try:
            if val == val.to_integral_value():
                return int(val)
            return float(val)
        except Exception:
            return str(val)
    if isinstance(val, datetime):
        return format_network_datetime(val)
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, dtime):
        return val.strftime("%H:%M:%S")
    if isinstance(val, (bytes, bytearray, memoryview)):
        return _cap_text(bytes(val).hex())
    return _cap_text(str(val))


def severity_label(code: Any) -> str | None:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return None
    return SEVERITY_LABELS.get(n)


def enrich_row(columns: list[str], row_dict: dict[str, Any]) -> dict[str, Any]:
    """Añade etiqueta de severidad junto al código numérico cuando aplica."""
    for col in columns:
        if col.upper() in _SEVERITY_COLUMNS and col in row_dict:
            label = severity_label(row_dict[col])
            if label is not None:
                row_dict[f"{col}_LABEL"] = label
            break
    return row_dict


def rows_as_dicts(columns: list[str], raw_rows: list[tuple]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in raw_rows:
        d = {col: serialize_value(val) for col, val in zip(columns, raw)}
        enrich_row(columns, d)
        result.append(d)
    return result


def to_compact_json(payload: Any) -> str:
    """JSON sin espacios superfluos (menos tokens para el cliente LLM)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def fit_payload(payload: dict[str, Any], max_chars: int) -> str:
    """Serializa ``payload`` y, si excede ``max_chars``, recorta filas hasta que entre.

    Recorta ``rows`` del propio payload (consulta a un RC) o de ``results[*].rows``
    (consulta multi-RC), proporcionalmente, marcando ``truncated=true`` en cada
    resultado recortado y ``response_truncated=true`` en el payload.
    """
    text = to_compact_json(payload)
    if len(text) <= max_chars:
        return text

    if isinstance(payload.get("rows"), list):
        holders = [payload]
    else:
        holders = [
            r
            for r in payload.get("results") or []
            if isinstance(r, dict) and isinstance(r.get("rows"), list)
        ]
    payload["response_truncated"] = True
    payload["response_note"] = (
        f"Respuesta recortada a ~{max_chars} caracteres. Usá agregaciones "
        "(COUNT/GROUP BY), menos columnas, filtros WHERE más estrictos o max_rows menor."
    )
    for _ in range(30):
        text = to_compact_json(payload)
        if len(text) <= max_chars:
            return text
        candidates = [h for h in holders if h["rows"]]
        if not candidates:
            break
        ratio = max_chars / len(text)
        for h in candidates:
            rows = h["rows"]
            keep = min(len(rows) - 1, int(len(rows) * ratio * 0.9))
            del rows[max(0, keep):]
            h["truncated"] = True
            h["row_count"] = len(rows)

    text = to_compact_json(payload)
    if len(text) <= max_chars:
        return text
    # Último recurso (p.ej. celdas enormes): devolver un preview válido como JSON.
    return to_compact_json(
        {
            "ok": payload.get("ok", False),
            "response_truncated": True,
            "response_note": payload["response_note"],
            "preview": text[: max_chars - 400],
        }
    )
