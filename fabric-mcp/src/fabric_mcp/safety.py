"""Validación de SQL en modo solo lectura (tokenizador + posición de sentencia)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

try:
    import sqlparse

    HAS_SQLPARSE = True
except ImportError:  # pragma: no cover
    HAS_SQLPARSE = False

ALLOWED_EXEC_SPS = {"SP_HELP", "SP_COLUMNS", "SP_TABLES", "SP_HELPTEXT"}

_STATEMENT_START_FORBIDDEN = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER",
    "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE",
    "DBCC", "KILL", "SHUTDOWN", "RECONFIGURE", "USE", "SET", "DECLARE", "BEGIN",
    "GO", "CHECKPOINT", "READTEXT", "WRITETEXT", "UPDATETEXT", "SAVE",
})

_ANYWHERE_FORBIDDEN = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER",
    "WAITFOR", "COMMIT", "ROLLBACK", "TRANSACTION", "BULK", "COPY",
})

_FORBIDDEN_PATTERNS = (
    (re.compile(r"\bXP_\w+"), "procedimientos extendidos xp_*"),
    (re.compile(r"\bNEXT\s+VALUE\s+FOR\b"), "NEXT VALUE FOR (consume secuencias)"),
    (re.compile(r"\bFOR\s+UPDATE\b"), "FOR UPDATE"),
    (re.compile(r"\b(UPDLOCK|XLOCK|HOLDLOCK|TABLOCKX|PAGLOCK|TABLOCK|SERIALIZABLE)\b"), "hints de bloqueo"),
    (re.compile(r"\bSP_EXECUTESQL\b"), "SP_EXECUTESQL"),
    (re.compile(r"\bOPENROWSET\b"), "OPENROWSET"),
    (re.compile(r"\bOPENQUERY\b"), "OPENQUERY"),
    (re.compile(r"\bOPENDATASOURCE\b"), "OPENDATASOURCE"),
    (re.compile(r"\bOPENXML\b"), "OPENXML"),
)

_NORM_RE = re.compile(
    r"(?P<str>N?'(?:[^']|'')*')"
    r"|(?P<brk>\[(?:[^\]]|\]\])*\])"
    r"|(?P<dq>\"(?:[^\"]|\"\")*\")"
    r"|(?P<lc>--[^\n]*)"
    r"|(?P<bc>/\*.*?\*/)",
    re.DOTALL,
)

_TOKEN_RE = re.compile(
    r"(?P<str>N?'(?:[^']|'')*')"
    r"|(?P<brk>\[(?:[^\]]|\]\])*\])"
    r"|(?P<dq>\"(?:[^\"]|\"\")*\")"
    r"|(?P<lc>--[^\n]*)"
    r"|(?P<bc>/\*.*?\*/)"
    r"|(?P<word>[A-Za-z_@#][\w@#$]*)"
    r"|(?P<punct>[;,()=])"
    r"|(?P<other>.)",
    re.DOTALL,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str

    @property
    def upper(self) -> str:
        return self.value.upper() if self.kind == "word" else ""


def _tokenize(sql: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    for m in _TOKEN_RE.finditer(sql):
        kind = m.lastgroup or "other"
        val = m.group(0)
        if kind in ("lc", "bc"):
            tokens.append(_Tok("space", " "))
        elif kind == "word":
            tokens.append(_Tok("word", val))
        elif kind in ("str", "brk", "dq"):
            tokens.append(_Tok(kind, val))
        elif kind == "punct":
            tokens.append(_Tok("punct", val))
        else:
            tokens.append(_Tok("other", val))
    return tokens


def normalize(sql: str) -> str:
    def repl(m: re.Match) -> str:
        kind = m.lastgroup
        if kind == "str":
            return "''"
        if kind in ("brk", "dq"):
            return "[X]"
        return " "

    out = _NORM_RE.sub(repl, sql)
    if "/*" in out or "'" in out.replace("''", ""):
        raise ValueError("Comentario /* o literal ' sin cerrar")
    return out.upper()


def strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def strip_literals(sql: str) -> str:
    sql = re.sub(r"N?'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r"\[(?:[^\]]|\]\])*\]", "[x]", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '"x"', sql)
    return sql


def _meaningful_tokens(tokens: list[_Tok]) -> list[_Tok]:
    return [t for t in tokens if t.kind != "space"]


def _skip_leading_parens(tokens: list[_Tok], i: int) -> int:
    while i < len(tokens) and tokens[i].kind == "punct" and tokens[i].value == "(":
        i += 1
    return i


def _first_word_index(tokens: list[_Tok]) -> int:
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "punct" and t.value == "(":
            i += 1
            continue
        if t.kind == "word":
            return i
        i += 1
    return len(tokens)


def _scan_blocked(tokens: list[_Tok]) -> Optional[str]:
    depth = 0
    at_statement_start = True
    prev_word: Optional[_Tok] = None
    for t in tokens:
        if t.kind == "punct":
            if t.value == "(":
                depth += 1
            elif t.value == ")":
                depth = max(0, depth - 1)
            elif t.value == ";" and depth == 0:
                at_statement_start = True
                prev_word = None
            continue
        if t.kind != "word" or depth > 0:
            if t.kind == "word":
                prev_word = t
            continue

        w = t.upper
        prev_upper = prev_word.upper if prev_word else ""

        if at_statement_start:
            if w in _STATEMENT_START_FORBIDDEN:
                return f"Palabra clave '{w}' no permitida en modo readonly"
            at_statement_start = False
        elif w in _ANYWHERE_FORBIDDEN:
            return f"Palabra clave '{w}' no permitida en modo readonly"
        elif w == "INTO" and prev_upper == "SELECT":
            return "SELECT ... INTO no permitido en modo readonly"
        elif w == "SAVE" and prev_upper == "TRANSACTION":
            return "Palabra clave 'SAVE TRANSACTION' no permitida en modo readonly"

        prev_word = t
    return None


def _validate_allowed_exec(sql: str) -> Optional[str]:
    no_comments = strip_sql_comments(sql).strip()
    upper = strip_literals(no_comments).strip().upper()
    first = upper.split()[0] if upper.split() else ""
    if first not in ("EXEC", "EXECUTE"):
        return None
    match = re.match(r"^(?:EXEC|EXECUTE)\s+((?:\[?\w+\]?\.)*)\[?(\w+)\]?", no_comments, re.IGNORECASE)
    if not match:
        return "Formato EXEC no reconocido"
    proc_name = match.group(2).upper()
    if proc_name not in ALLOWED_EXEC_SPS:
        return (
            f"EXEC {proc_name} no está permitido en modo readonly. "
            f"Permitidos: {sorted(ALLOWED_EXEC_SPS)}"
        )
    return None


def validate_readonly(sql: str) -> Optional[str]:
    """Devuelve un mensaje de error si el SQL no es de solo lectura, o None si es válido."""
    if not sql or not sql.strip():
        return "Query vacío"

    exec_err = _validate_allowed_exec(sql)
    if exec_err:
        return exec_err

    no_comments = strip_sql_comments(sql).strip()
    upper_raw = strip_literals(no_comments).strip().upper()
    if upper_raw.split()[0] in ("EXEC", "EXECUTE"):
        if HAS_SQLPARSE:
            parsed = sqlparse.parse(sql)
            non_empty = [s for s in parsed if s.tokens and str(s).strip().strip(";").strip()]
            if len(non_empty) > 1:
                return "sqlparse detectó múltiples statements; en readonly solo se permite uno"
        return None

    try:
        upper = normalize(sql).strip()
    except ValueError as e:
        return str(e)

    body = upper.rstrip().rstrip(";").rstrip()
    if ";" in body:
        return "No se permiten múltiples statements separados por ';' en modo readonly"
    if not body:
        return "Query vacío"

    try:
        norm_body = normalize(sql).strip().rstrip(";").strip()
    except ValueError as e:
        return str(e)
    for pattern, label in _FORBIDDEN_PATTERNS:
        if pattern.search(norm_body):
            return f"No permitido en solo lectura: {label}"

    tokens = _meaningful_tokens(_tokenize(sql))
    while tokens and tokens[-1].kind == "punct" and tokens[-1].value == ";":
        tokens.pop()
    if any(t.kind == "punct" and t.value == ";" for t in tokens):
        return "No se permiten múltiples statements separados por ';' en modo readonly"

    if not tokens:
        return "Query vacío"
    start = _first_word_index(tokens)
    start = _skip_leading_parens(tokens, start)
    if start >= len(tokens) or tokens[start].kind != "word" or tokens[start].upper not in ("SELECT", "WITH"):
        got = tokens[start].value if start < len(tokens) else "?"
        return (
            f"Solo se permiten queries que empiecen con SELECT/WITH o EXEC <sp_help*>. "
            f"Recibido: '{got}'"
        )

    blocked = _scan_blocked(tokens)
    if blocked:
        return blocked

    if HAS_SQLPARSE:
        parsed = sqlparse.parse(sql)
        non_empty = [s for s in parsed if s.tokens and str(s).strip().strip(";").strip()]
        if len(non_empty) > 1:
            return "sqlparse detectó múltiples statements; en readonly solo se permite uno"

    return None


def quote_ident(name: str) -> str:
    """Delimita un identificador T-SQL de forma segura: foo]bar -> [foo]]bar]."""
    if not name or len(name) > 128 or "\x00" in name:
        raise ValueError(f"Identificador inválido: {name!r}")
    return "[" + name.replace("]", "]]") + "]"
