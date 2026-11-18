"""Validación de SQL de solo lectura (capa 1 de 3; ver db.py para rollback y ApplicationIntent)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import sqlparse

# Verbos de sentencia: prohibidos en cualquier posición de nivel 0 (T-SQL no exige ';').
_STATEMENT_START_FORBIDDEN = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER",
    "EXEC", "EXECUTE", "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE",
    "DBCC", "KILL", "SHUTDOWN", "RECONFIGURE", "USE", "SET", "DECLARE", "BEGIN",
    "GO", "CHECKPOINT", "READTEXT", "WRITETEXT", "UPDATETEXT", "SAVE",
})

# Prohibidos dentro de paréntesis (subconsultas): verbos que modifican datos u objetos.
_ANY_DEPTH_FORBIDDEN = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER",
    "EXEC", "EXECUTE", "GRANT", "REVOKE", "DENY", "BACKUP", "RESTORE", "DBCC", "KILL",
    "SHUTDOWN", "RECONFIGURE",
})

# Prohibidos en cualquier punto de la sentencia (profundidad de paréntesis 0).
_ANYWHERE_FORBIDDEN = frozenset({
    "WAITFOR", "COMMIT", "ROLLBACK", "TRANSACTION", "BULK",
})

_FORBIDDEN_PATTERNS = (
    (re.compile(r"\bXP_\w+"), "procedimientos extendidos xp_*"),
    (re.compile(r"\bNEXT\s+VALUE\s+FOR\b"), "NEXT VALUE FOR (consume secuencias)"),
    (re.compile(r"\bFOR\s+UPDATE\b"), "FOR UPDATE"),
    (re.compile(r"\b(UPDLOCK|XLOCK|HOLDLOCK|TABLOCKX|PAGLOCK|TABLOCK|SERIALIZABLE)\b"),
     "hints de bloqueo"),
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
    """Devuelve el SQL en mayúsculas sin comentarios y con literales/identificadores neutralizados."""
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


def _meaningful_tokens(tokens: list[_Tok]) -> list[_Tok]:
    return [t for t in tokens if t.kind != "space"]


def _skip_leading_parens(tokens: list[_Tok], i: int) -> int:
    while i < len(tokens) and tokens[i].kind == "punct" and tokens[i].value == "(":
        i += 1
    return i


# Tras estos tokens SQL espera una expresión/identificador: una palabra clave ahí es un nombre de
# columna (p. ej. "SELECT Set, Begin" o "alias = Update"), no el inicio de otra sentencia.
_EXPR_CONTEXT_WORDS = frozenset({
    "SELECT", "DISTINCT", "AS", "WHERE", "AND", "OR", "NOT", "BY", "ON", "WHEN", "THEN", "ELSE",
    "CASE", "HAVING", "IS", "LIKE", "BETWEEN", "IN", "TOP", "ALL", "ANY", "SOME", "EXISTS",
})
_EXPR_CONTEXT_PUNCT = frozenset({",", "=", "(", ".", "<", ">", "+", "-", "*", "/", "%", "!", "~", "&", "|", "^"})


def _is_identifier_position(prev: Optional[_Tok]) -> bool:
    if prev is None:
        return False
    if prev.kind == "word":
        return prev.upper in _EXPR_CONTEXT_WORDS
    return prev.value in _EXPR_CONTEXT_PUNCT


def _scan_blocked(tokens: list[_Tok]) -> Optional[str]:
    """Detecta construcciones prohibidas por posición de token.

    T-SQL no exige ';' entre sentencias ("SELECT 1 DELETE FROM t", "WITH c AS (...) DELETE FROM t"),
    así que un verbo de sentencia se rechaza en cualquier posición salvo donde SQL espera una
    expresión (ahí solo puede ser un nombre de columna).
    """
    depth = 0
    prev_tok: Optional[_Tok] = None
    prev_word: Optional[_Tok] = None
    for t in tokens:
        if t.kind == "punct":
            if t.value == "(":
                depth += 1
            elif t.value == ")":
                depth = max(0, depth - 1)
            elif t.value == ";" and depth == 0:
                prev_word = None
            prev_tok = t
            continue
        if t.kind != "word":
            if t.value.strip():
                prev_tok = t
            continue

        w = t.upper
        prev_upper = prev_word.upper if prev_word else ""
        as_identifier = _is_identifier_position(prev_tok)

        if depth > 0:
            if w in _ANY_DEPTH_FORBIDDEN and not as_identifier:
                return f"Palabra clave '{w}' no permitida: este MCP es de solo lectura"
        else:
            if w in _STATEMENT_START_FORBIDDEN and not as_identifier:
                return f"Palabra clave '{w}' no permitida: este MCP es de solo lectura"
            if w in _ANYWHERE_FORBIDDEN:
                return f"Palabra clave '{w}' no permitida: este MCP es de solo lectura"
            if w == "INTO" and prev_upper == "SELECT":
                return "SELECT ... INTO no permitido: este MCP es de solo lectura"
            if w == "INTO":
                return "INTO no permitido: este MCP es de solo lectura"
        prev_word = t
        prev_tok = t
    return None


def validate_readonly(sql: str) -> Optional[str]:
    """Mensaje de error si el SQL no es un único SELECT/WITH de solo lectura; None si es válido."""
    if not sql or not sql.strip():
        return "Query vacío"
    try:
        upper = normalize(sql).strip()
    except ValueError as e:
        return str(e)

    body = upper.rstrip().rstrip(";").rstrip()
    if ";" in body:
        return "Solo se permite UNA sentencia (se encontró ';' intermedio)"
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
        return "Solo se permite UNA sentencia (se encontró ';' intermedio)"

    if not tokens:
        return "Query vacío"
    start = _skip_leading_parens(tokens, 0)
    if start >= len(tokens) or tokens[start].kind != "word" or tokens[start].upper not in ("SELECT", "WITH"):
        got = tokens[start].value if start < len(tokens) else "?"
        return f"Solo se permiten consultas SELECT o WITH (CTE). Recibido: '{got}'"

    blocked = _scan_blocked(tokens)
    if blocked:
        return blocked

    statements = [s for s in sqlparse.parse(sql) if str(s).strip().strip(";").strip()]
    if len(statements) > 1:
        return "sqlparse detectó múltiples sentencias; solo se permite una"
    return None


def validate_fragment(fragment: str, what: str) -> Optional[str]:
    """Valida un fragmento (WHERE / ORDER BY) insertándolo en un SELECT de prueba."""
    if fragment is None or not fragment.strip():
        return None
    try:
        norm = normalize(fragment)
    except ValueError as e:
        return f"{what}: {e}"
    if ";" in norm:
        return f"{what}: no se permite ';'"
    depth = 0
    for ch in norm:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            break
    if depth != 0:
        return f"{what}: paréntesis desbalanceados"
    if re.search(r"\b(SELECT|UNION|EXCEPT|INTERSECT|FROM)\b", norm) and what == "order_by":
        return f"{what}: no se permiten subconsultas"
    err = validate_readonly(f"SELECT 1 AS x FROM [t] WHERE {fragment}" if what == "where"
                            else f"SELECT 1 AS x FROM [t] ORDER BY {fragment}")
    return f"{what}: {err}" if err else None


_IDENT_RE = re.compile(r"^[\w@#$ .\-]{1,128}$", re.UNICODE)


def quote_ident(name: str) -> str:
    """[foo]]bar] a partir de foo]bar. Acepta 'x' o '[x]' y rechaza caracteres raros."""
    if name is None:
        raise ValueError("Identificador vacío")
    name = name.strip()
    if name.startswith("[") and name.endswith("]") and len(name) > 2:
        name = name[1:-1].replace("]]", "]")
    if not _IDENT_RE.match(name):
        raise ValueError(f"Identificador inválido: {name!r}")
    return "[" + name.replace("]", "]]") + "]"


def like_pattern(pattern: str) -> str:
    """'cell' -> '%cell%'; 'cell*' -> 'cell%'; respeta % explícitos."""
    p = (pattern or "").strip().replace("*", "%").replace("?", "_")
    if not p:
        raise ValueError("pattern vacío")
    return p if "%" in p else f"%{p}%"
