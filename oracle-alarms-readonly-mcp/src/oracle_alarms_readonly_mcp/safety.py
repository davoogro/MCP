"""Capa de seguridad SQL: solo SELECT / WITH…SELECT, una sentencia, sin PL/SQL.

Defensa en profundidad (la última barrera es SET TRANSACTION READ ONLY + privilegios
del usuario Oracle):

1. Un lexer mínimo separa literales ('...', q'[...]', "IDENT") y comentarios, de modo
   que palabras clave dentro de literales/comentarios no producen falsos positivos y
   un ';' o '--' dentro de un literal no rompe la consulta.
2. Una sola sentencia; debe empezar por SELECT o WITH (se toleran paréntesis iniciales).
3. Lista negra de tokens DML/DDL/PL-SQL/TCL, incluido SELECT … FOR UPDATE y
   WITH FUNCTION/PROCEDURE (PL/SQL inline de 12c).
4. Paquetes con efectos laterales (DBMS_*, UTL_*, HTTPURITYPE…) salvo una allowlist
   de paquetes de solo lectura habituales (DBMS_LOB, DBMS_XPLAN, …).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlparse

# Rechazo explícito (defensa en profundidad además del whitelist de prefijos).
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UPSERT",
        "TRUNCATE",
        "DROP",
        "ALTER",
        "CREATE",
        "GRANT",
        "REVOKE",
        "COMMIT",
        "ROLLBACK",
        "EXECUTE",
        "EXEC",
        "CALL",
        "BEGIN",
        "DECLARE",
        "PRAGMA",
        "SAVEPOINT",
        "LOCK",
        "SET",  # evita SET ROLE / SET TRANSACTION desde el cliente
        "PURGE",
        "FLASHBACK",
        "ANALYZE",
        "ASSOCIATE",
        "DISASSOCIATE",
        "COMMENT",  # COMMENT ON …
        "RENAME",
        "AUDIT",
        "NOAUDIT",
        "SHUTDOWN",
        "STARTUP",
        "HTTPURITYPE",
        "HTP",
        "HTF",
    }
)

# Paquetes Oracle con prefijo de riesgo (red, ficheros, jobs, pipes, locks, SQL dinámico…)
_RISKY_PACKAGE_PREFIXES = ("DBMS_", "UTL_", "OWA_")
# Paquetes de solo lectura habituales permitidos en SELECT.
_ALLOWED_PACKAGES = frozenset(
    {
        "DBMS_LOB",
        "DBMS_RANDOM",
        "DBMS_XPLAN",
        "DBMS_METADATA",
        "DBMS_UTILITY",
        "UTL_RAW",
        "UTL_I18N",
        "UTL_ENCODE",
        "UTL_MATCH",
    }
)

_ALLOWED_PREFIXES = ("SELECT", "WITH")

_TOKEN = re.compile(r"(?<![A-Z0-9_$#])[A-Z_][A-Z0-9_$#]*")

# Indicios de bloque PL/SQL aunque empiece con WITH/SELECT anómalo
_PLSQL_MARKERS = (
    re.compile(r"\bWITH\s+(?:FUNCTION|PROCEDURE)\b", re.IGNORECASE),
    re.compile(r"\bEND\s*;", re.IGNORECASE),
    re.compile(r":="),  # asignación PL/SQL
)

_FOR_UPDATE = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)

# Límites de filas ya presentes en la consulta
_HAS_FETCH_FIRST = re.compile(
    r"\bFETCH\s+(?:FIRST|NEXT)\s+\d+\s+ROWS?\s+ONLY\b",
    re.IGNORECASE,
)
_HAS_ROWNUM = re.compile(r"\bROWNUM\b", re.IGNORECASE)

_Q_QUOTE_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$#"


class SqlSafetyError(ValueError):
    """La consulta fue rechazada por la política de solo lectura."""


@dataclass(frozen=True)
class ValidatedSql:
    original: str
    cleaned: str  # SQL ejecutable: sin comentarios (salvo hints /*+ */), literales intactos
    has_row_limit: bool
    masked: str = ""  # SQL para inspección: literales y comentarios neutralizados


def lex_sql(sql_text: str) -> tuple[str, str]:
    """Separa literales y comentarios de un SQL Oracle.

    Returns:
        (executable, masked)
        - executable: SQL sin comentarios (los hints ``/*+ … */`` se conservan),
          literales y comillas dobles intactos.
        - masked: igual pero con literales reemplazados por ``''``, identificadores
          entrecomillados por ``"_"`` y hints eliminados. Solo para inspección.

    Raises:
        SqlSafetyError: literal, identificador o comentario sin cerrar.
    """
    s = sql_text
    n = len(s)
    i = 0
    exe: list[str] = []
    msk: list[str] = []
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""

        # Comentario de línea
        if c == "-" and nxt == "-":
            j = s.find("\n", i)
            j = n if j == -1 else j
            exe.append(" ")
            msk.append(" ")
            i = j
            continue

        # Comentario de bloque / hint
        if c == "/" and nxt == "*":
            j = s.find("*/", i + 2)
            if j == -1:
                raise SqlSafetyError("Se rechazó la consulta: comentario /* sin cerrar.")
            body = s[i : j + 2]
            exe.append(body if body.startswith("/*+") else " ")
            msk.append(" ")
            i = j + 2
            continue

        # Literal q'X…X' (y nq'…', Nq'…')
        prev_ok = i == 0 or not _is_ident_char(s[i - 1])
        if prev_ok and (
            (c in "qQ" and nxt == "'") or (c in "nN" and nxt in "qQ" and s[i + 2 : i + 3] == "'")
        ):
            start = i
            qpos = i + 1 if c in "qQ" else i + 2
            if qpos + 1 >= n:
                raise SqlSafetyError("Se rechazó la consulta: literal q'…' sin cerrar.")
            delim = s[qpos + 1]
            closer = _Q_QUOTE_CLOSERS.get(delim, delim)
            end = s.find(closer + "'", qpos + 2)
            if end == -1:
                raise SqlSafetyError("Se rechazó la consulta: literal q'…' sin cerrar.")
            exe.append(s[start : end + 2])
            msk.append("''")
            i = end + 2
            continue

        # Literal '…' con escape ''
        if c == "'":
            j = i + 1
            while True:
                k = s.find("'", j)
                if k == -1:
                    raise SqlSafetyError("Se rechazó la consulta: literal '…' sin cerrar.")
                if k + 1 < n and s[k + 1] == "'":
                    j = k + 2
                    continue
                break
            exe.append(s[i : k + 1])
            msk.append("''")
            i = k + 1
            continue

        # Identificador entrecomillado
        if c == '"':
            k = s.find('"', i + 1)
            if k == -1:
                raise SqlSafetyError('Se rechazó la consulta: identificador "…" sin cerrar.')
            exe.append(s[i : k + 1])
            msk.append('"_"')
            i = k + 1
            continue

        exe.append(c)
        msk.append(c)
        i += 1
    return "".join(exe), "".join(msk)


def strip_sql_comments(sql_text: str) -> str:
    """Elimina comentarios respetando literales (compatibilidad)."""
    return lex_sql(sql_text)[0]


def _first_keyword(cleaned_upper: str) -> str:
    parts = cleaned_upper.lstrip("( \t\r\n").split()
    return parts[0] if parts else ""


def _check_tokens(masked_upper: str) -> None:
    tokens = _TOKEN.findall(masked_upper)
    prev = ""
    for tok in tokens:
        # LISTAGG(... ON OVERFLOW TRUNCATE ...) es sintaxis de lectura legítima
        if tok in _FORBIDDEN_KEYWORDS and not (tok == "TRUNCATE" and prev == "OVERFLOW"):
            raise SqlSafetyError(
                f"Se rechazó la consulta: palabra clave prohibida '{tok}'. "
                f"Este MCP es de solo lectura; no se ejecutan DML/DDL ni PL/SQL."
            )
        if tok.startswith(_RISKY_PACKAGE_PREFIXES) and tok not in _ALLOWED_PACKAGES:
            raise SqlSafetyError(
                f"Se rechazó la consulta: llamada a paquete '{tok}' no permitida "
                f"(posibles efectos laterales). Permitidos: {', '.join(sorted(_ALLOWED_PACKAGES))}."
            )
        prev = tok


def validate_readonly_sql(sql_text: str) -> ValidatedSql:
    """Valida que la SQL sea una única sentencia SELECT/WITH de solo lectura.

    Raises:
        SqlSafetyError: con mensaje claro de por qué se rechazó.
    """
    if sql_text is None or not str(sql_text).strip():
        raise SqlSafetyError("Consulta vacía: se esperaba un SELECT o WITH … SELECT.")

    original = str(sql_text)
    executable, masked = lex_sql(original)
    executable = executable.strip()
    if not masked.strip():
        raise SqlSafetyError(
            "Consulta vacía tras eliminar comentarios: se esperaba SELECT o WITH … SELECT."
        )

    upper = masked.strip().upper()

    # Una sola sentencia: ';' solo como terminador final opcional
    body = upper.rstrip().rstrip(";").rstrip()
    if ";" in body:
        raise SqlSafetyError(
            "Se rechazó la consulta: no se permiten múltiples sentencias "
            "(hay ';' separando statements). Enviá una sola sentencia SELECT/WITH."
        )
    first = _first_keyword(body)
    if first not in _ALLOWED_PREFIXES:
        raise SqlSafetyError(
            f"Se rechazó la consulta: solo se permiten sentencias que empiecen "
            f"con SELECT o WITH … SELECT. Recibido: '{first}'."
        )

    for marker in _PLSQL_MARKERS:
        if marker.search(body):
            raise SqlSafetyError(
                "Se rechazó la consulta: parece un bloque PL/SQL "
                f"(patrón '{marker.pattern}'). Solo se aceptan SELECT/WITH."
            )

    if _FOR_UPDATE.search(body):
        raise SqlSafetyError(
            "Se rechazó la consulta: SELECT … FOR UPDATE bloquea filas y no "
            "está permitido en este MCP de solo lectura."
        )

    _check_tokens(body)

    # sqlparse: múltiples statements o tipo no-DML. Se le pasa el SQL enmascarado porque
    # sqlparse no entiende literales q'[…]' de Oracle.
    parsed = sqlparse.parse(masked)
    non_empty = [s for s in parsed if s.tokens and str(s).strip().strip(";").strip()]
    if len(non_empty) > 1:
        raise SqlSafetyError(
            "Se rechazó la consulta: sqlparse detectó múltiples statements. "
            "Solo se permite uno por llamada."
        )
    if non_empty:
        stmt_type = non_empty[0].get_type()
        # sqlparse marca SELECT/WITH como 'SELECT' o a veces 'UNKNOWN'
        if stmt_type not in ("SELECT", "UNKNOWN"):
            raise SqlSafetyError(
                f"Se rechazó la consulta: tipo de sentencia detectado '{stmt_type}'. "
                f"Solo SELECT/WITH están permitidos."
            )

    has_limit = bool(_HAS_FETCH_FIRST.search(body) or _HAS_ROWNUM.search(body))
    return ValidatedSql(original=original, cleaned=executable, has_row_limit=has_limit, masked=body)


def ensure_row_limit(sql_text: str, max_rows: int, has_row_limit: bool) -> tuple[str, bool]:
    """Si la consulta no trae FETCH FIRST ni ROWNUM, la envuelve con tope.

    El tope es ``max_rows + 1`` para que el llamador pueda detectar truncado
    (si llegan más de ``max_rows`` filas, hubo más resultados en origen).

    Returns:
        (sql_ejecutable, wrapped) — wrapped=True si se aplicó el envoltorio.
    """
    max_rows = max(1, int(max_rows))
    stripped = sql_text.strip().rstrip(";").strip()
    if has_row_limit:
        return stripped, False
    wrapped = f"SELECT * FROM (\n{stripped}\n) q_limited WHERE ROWNUM <= {max_rows + 1}"
    return wrapped, True
