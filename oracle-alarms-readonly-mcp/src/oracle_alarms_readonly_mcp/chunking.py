"""Troceo de ventanas ALARM_TIME para mitigar el timeout de proxy de rc8 (~11 s)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

# Literales Oracle frecuentes en consultas FM
_TO_DATE = re.compile(
    r"TO_DATE\s*\(\s*'([^']+)'\s*(?:,\s*'([^']*)')?\s*\)",
    re.IGNORECASE,
)
_TIMESTAMP_LIT = re.compile(
    r"TIMESTAMP\s+'([^']+)'",
    re.IGNORECASE,
)
_DATE_LIT = re.compile(
    r"DATE\s+'([^']+)'",
    re.IGNORECASE,
)
_BARE_STRING = re.compile(r"'([^']+)'")

_BETWEEN = re.compile(
    r"(?P<prefix>\bALARM_TIME\s+BETWEEN\s+)"
    r"(?P<lo>.+?)"
    r"(?P<mid>\s+AND\s+)"
    r"(?P<hi>.+?)"
    r"(?=(?:\s+(?:AND|OR|GROUP|ORDER|HAVING|FETCH|UNION|MINUS|INTERSECT)\b|\s*\)|\s*$))",
    re.IGNORECASE | re.DOTALL,
)

_GE = re.compile(
    r"\bALARM_TIME\s*(?P<op>>=|>)\s*(?P<expr>"
    r"TO_DATE\s*\([^)]+\)|TIMESTAMP\s+'[^']+'|DATE\s+'[^']+'|'[^']+'"
    r")",
    re.IGNORECASE,
)
_LE = re.compile(
    r"\bALARM_TIME\s*(?P<op><=|<)\s*(?P<expr>"
    r"TO_DATE\s*\([^)]+\)|TIMESTAMP\s+'[^']+'|DATE\s+'[^']+'|'[^']+'"
    r")",
    re.IGNORECASE,
)

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)


class ChunkingError(ValueError):
    """No se pudo trocear una ventana ALARM_TIME cuando era necesario."""


@dataclass(frozen=True)
class AlarmTimeRange:
    start: datetime
    end: datetime
    kind: str  # "between" | "inequalities"


@dataclass(frozen=True)
class ChunkRewrite:
    sql: str
    chunk_start: datetime
    chunk_end: datetime


def _parse_datetime(text: str) -> datetime | None:
    raw = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    # Truncar fracciones de segundo si vienen
    if "." in raw:
        base = raw.split(".", 1)[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(base, fmt)
            except ValueError:
                continue
    return None


def parse_oracle_datetime_expr(expr: str) -> datetime | None:
    """Extrae un datetime de un literal Oracle (TO_DATE / TIMESTAMP / DATE / string)."""
    s = expr.strip()
    m = _TO_DATE.search(s)
    if m:
        return _parse_datetime(m.group(1))
    m = _TIMESTAMP_LIT.search(s)
    if m:
        return _parse_datetime(m.group(1))
    m = _DATE_LIT.search(s)
    if m:
        return _parse_datetime(m.group(1))
    m = _BARE_STRING.search(s)
    if m:
        return _parse_datetime(m.group(1))
    return None


def detect_alarm_time_range(sql: str) -> AlarmTimeRange | None:
    """Detecta un rango sobre ALARM_TIME si está claramente acotado por dos extremos."""
    m = _BETWEEN.search(sql)
    if m:
        lo = parse_oracle_datetime_expr(m.group("lo"))
        hi = parse_oracle_datetime_expr(m.group("hi"))
        if lo is None or hi is None:
            return None
        if hi < lo:
            lo, hi = hi, lo
        return AlarmTimeRange(start=lo, end=hi, kind="between")

    ge = list(_GE.finditer(sql))
    le = list(_LE.finditer(sql))
    if not ge or not le:
        return None

    starts: list[tuple[datetime, str]] = []
    for m in ge:
        dt = parse_oracle_datetime_expr(m.group("expr"))
        if dt is not None:
            starts.append((dt, m.group("op")))
    ends: list[tuple[datetime, str]] = []
    for m in le:
        dt = parse_oracle_datetime_expr(m.group("expr"))
        if dt is not None:
            ends.append((dt, m.group("op")))
    if not starts or not ends:
        return None

    start_dt = min(s[0] for s in starts)
    end_dt = max(e[0] for e in ends)
    if end_dt < start_dt:
        return None
    return AlarmTimeRange(start=start_dt, end=end_dt, kind="inequalities")


def _to_date_literal(dt: datetime) -> str:
    return (
        "TO_DATE('"
        + dt.strftime("%Y-%m-%d %H:%M:%S")
        + "', 'YYYY-MM-DD HH24:MI:SS')"
    )


def iter_chunks(start: datetime, end: datetime, chunk_days: int) -> Iterator[tuple[datetime, datetime]]:
    """Genera tramos [lo, hi) de hasta chunk_days; el último puede ser más corto."""
    if chunk_days < 1:
        yield start, end
        return
    step = timedelta(days=chunk_days)
    cur = start
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


def _rewrite_between(sql: str, lo: datetime, hi: datetime) -> str:
    def repl(m: re.Match[str]) -> str:
        return (
            m.group("prefix")
            + _to_date_literal(lo)
            + m.group("mid")
            + _to_date_literal(hi)
        )

    new_sql, n = _BETWEEN.subn(repl, sql, count=1)
    if n != 1:
        raise ChunkingError(
            "Se detectó ALARM_TIME BETWEEN pero no se pudo reescribir el rango "
            "para trocearlo. Reformulá la consulta con BETWEEN TO_DATE(...)."
        )
    return new_sql


def _rewrite_inequalities(sql: str, lo: datetime, hi: datetime) -> str:
    """Reemplaza el primer >=/> y el primer <=/< sobre ALARM_TIME por el tramo dado."""
    ge = _GE.search(sql)
    le = _LE.search(sql)
    if not ge or not le:
        raise ChunkingError(
            "Se detectó rango ALARM_TIME con desigualdades pero no se pudo reescribir. "
            "Usá: ALARM_TIME >= TO_DATE(...) AND ALARM_TIME < TO_DATE(...)."
        )

    # Reemplazar de atrás hacia adelante para no desplazar índices
    spans = sorted(
        [
            (ge.start(), ge.end(), f"ALARM_TIME {ge.group('op')} {_to_date_literal(lo)}"),
            (le.start(), le.end(), f"ALARM_TIME {le.group('op')} {_to_date_literal(hi)}"),
        ],
        key=lambda t: t[0],
        reverse=True,
    )
    out = sql
    for start, end, repl in spans:
        out = out[:start] + repl + out[end:]
    return out


def rewrite_sql_for_chunk(sql: str, lo: datetime, hi: datetime, kind: str) -> str:
    if kind == "between":
        return _rewrite_between(sql, lo, hi)
    return _rewrite_inequalities(sql, lo, hi)


def should_chunk(rc: str, chunk_days: int) -> bool:
    return rc.strip().lower() == "rc8" and chunk_days > 0


def plan_rc8_chunks(sql: str, rc: str, chunk_days: int) -> list[ChunkRewrite] | None:
    """Si aplica troceo rc8, devuelve N rewrites; si no hace falta, None (correr SQL original).

    Raises:
        ChunkingError: rango detectado demasiado amplio pero no reescribible.
    """
    if not should_chunk(rc, chunk_days):
        return None

    rng = detect_alarm_time_range(sql)
    if rng is None:
        return None

    span = rng.end - rng.start
    if span <= timedelta(days=chunk_days):
        return None

    chunks: list[ChunkRewrite] = []
    for lo, hi in iter_chunks(rng.start, rng.end, chunk_days):
        rewritten = rewrite_sql_for_chunk(sql, lo, hi, rng.kind)
        chunks.append(ChunkRewrite(sql=rewritten, chunk_start=lo, chunk_end=hi))

    if len(chunks) < 2:
        return None
    return chunks
