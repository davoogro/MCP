"""Escritura en SQL Server: clasificación de riesgo, ejecución transaccional, dry-run y auditoría.

Reglas:
  * Todo lo que no sea un SELECT puro se considera escritura.
  * Lo destructivo o administrativo (DROP, TRUNCATE, DELETE/UPDATE sin WHERE, ALTER DATABASE,
    GRANT/REVOKE, KILL, SHUTDOWN, xp_*, sp_configure...) exige confirm=true.
  * Por defecto todo el script (todos los lotes GO) corre en UNA transacción: si algo falla se
    deshace todo. dry_run=true ejecuta y luego deshace, para ver filas afectadas y resultados.
  * Cada ejecución (también los rechazos y dry-runs) se anota en un JSONL de auditoría.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pyodbc

from opt_mssql_mcp import db
from opt_mssql_mcp.config import Server, Settings
from opt_mssql_mcp.safety import normalize, quote_ident, validate_readonly
from opt_mssql_mcp.tunnel import ensure_tunnel

logger = logging.getLogger("opt-mssql-mcp")

# ============================================================
# Lotes (GO)
# ============================================================

_GO_RE = re.compile(r"^[ \t]*GO(?:[ \t]+(\d+))?[ \t]*(?:--[^\n]*)?$", re.IGNORECASE | re.MULTILINE)
_MAX_GO_REPEAT = 1000


def split_batches(sql: str) -> list[str]:
    """Separa el script por líneas 'GO' (como sqlcmd/SSMS). 'GO n' repite el lote n veces."""
    batches: list[str] = []
    pos = 0
    for m in _GO_RE.finditer(sql):
        chunk = sql[pos:m.start()]
        repeat = int(m.group(1)) if m.group(1) else 1
        if repeat > _MAX_GO_REPEAT:
            raise ValueError(f"GO {repeat}: máximo {_MAX_GO_REPEAT} repeticiones")
        if chunk.strip():
            batches.extend([chunk.strip()] * repeat)
        pos = m.end()
    tail = sql[pos:]
    if tail.strip():
        batches.append(tail.strip())
    return batches


# ============================================================
# Clasificación
# ============================================================

_DANGEROUS = [
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW|PROC|PROCEDURE|FUNCTION|TRIGGER|INDEX|LOGIN|USER|ROLE|"
                r"SYNONYM|TYPE|SEQUENCE|ASSEMBLY|CERTIFICATE|ENDPOINT|PARTITION|STATISTICS|FULLTEXT|"
                r"EXTERNAL|CREDENTIAL|MASTER|SYMMETRIC|ASYMMETRIC|XML|SECURITY|APPLICATION|SERVER)\b"),
     "DROP de objeto"),
    (re.compile(r"\bTRUNCATE\s+TABLE\b"), "TRUNCATE TABLE"),
    (re.compile(r"\bALTER\s+TABLE\b[^;]*?\bDROP\b"), "ALTER TABLE ... DROP (columna/constraint)"),
    (re.compile(r"\bALTER\s+(DATABASE|SERVER|LOGIN|AUTHORIZATION|AVAILABILITY|RESOURCE|ENDPOINT)\b"),
     "ALTER de base/servidor/login"),
    (re.compile(r"\bCREATE\s+LOGIN\b"), "CREATE LOGIN"),
    (re.compile(r"\b(GRANT|REVOKE|DENY)\b"), "cambio de permisos (GRANT/REVOKE/DENY)"),
    (re.compile(r"\bSHUTDOWN\b"), "SHUTDOWN"),
    (re.compile(r"\bKILL\b"), "KILL de sesión"),
    (re.compile(r"\bRESTORE\b"), "RESTORE"),
    (re.compile(r"\bBACKUP\b"), "BACKUP"),
    (re.compile(r"\bRECONFIGURE\b|\bSP_CONFIGURE\b"), "cambio de configuración del servidor"),
    (re.compile(r"\bXP_\w+"), "procedimiento extendido xp_*"),
    (re.compile(r"\bDBCC\b"), "DBCC"),
    (re.compile(r"\bSP_(RENAME|DETACH_DB|ATTACH_DB|DROP\w*|ADDSRVROLEMEMBER|ADDROLEMEMBER|DROPROLEMEMBER|"
                r"ADDLOGIN|DEFAULTDB|PASSWORD|DELETE_\w+)\b"),
     "procedimiento de sistema administrativo/destructivo"),
    (re.compile(r"\bWHEN\s+NOT\s+MATCHED\s+BY\s+SOURCE\b[^;]*?\bDELETE\b"),
     "MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE"),
]

# Sentencias que SQL Server no permite dentro de una transacción de usuario.
_NO_TRANSACTION = [
    re.compile(r"\b(CREATE|ALTER|DROP)\s+DATABASE\b"),
    re.compile(r"\bBACKUP\b"),
    re.compile(r"\bRESTORE\b"),
    re.compile(r"\bRECONFIGURE\b"),
    re.compile(r"\b(CREATE|ALTER|DROP)\s+FULLTEXT\s+(CATALOG|INDEX)\b"),
    re.compile(r"\bKILL\b"),
    re.compile(r"\bALTER\s+SERVER\b"),
]

_DML_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE")
# El cuerpo de un módulo no se ejecuta al crearlo: no se evalúa como destructivo.
_MODULE_RE = re.compile(r"^\s*(CREATE\s+OR\s+ALTER|CREATE|ALTER)\s+(PROC|PROCEDURE|FUNCTION|TRIGGER|VIEW)\b")
_WORD_RE = re.compile(r"[A-Z_@#][A-Z0-9_@#$]*|[()]|;")


def _depth0_words(norm: str) -> list[str]:
    """Palabras a profundidad de paréntesis 0 (con ';' como separador)."""
    out, depth = [], 0
    for tok in _WORD_RE.findall(norm):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(tok)
    return out


def _statements(words: list[str]) -> list[list[str]]:
    """Agrupa palabras de nivel 0 en sentencias: ';' o el inicio de un verbo nuevo las separa."""
    starters = {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER", "EXEC",
                "EXECUTE", "DECLARE", "SET", "BEGIN", "COMMIT", "ROLLBACK", "IF", "WHILE", "PRINT",
                "WITH", "SELECT", "GRANT", "REVOKE", "DENY", "USE", "RETURN", "THROW"}
    stmts: list[list[str]] = []
    cur: list[str] = []
    prev = ""
    for w in words:
        if w == ";":
            if cur:
                stmts.append(cur)
            cur, prev = [], ""
            continue
        # SELECT tras UNION/INSERT/etc. no inicia sentencia; UPDATE dentro de "ON UPDATE"/"FOR UPDATE" tampoco.
        new = w in starters and cur and prev not in ("UNION", "ALL", "EXCEPT", "INTERSECT", "ON", "FOR",
                                                       "AFTER", "OF", "INSTEAD", "THEN", "AS", "INTO")
        if new and w == "SELECT" and cur and cur[0] in ("INSERT", "WITH", "DECLARE", "SET", "IF", "WHILE"):
            new = False
        if new and cur and cur[0] == "WITH" and w in _DML_VERBS:
            new = False
        # SET de UPDATE ... SET / MERGE ... UPDATE SET pertenece a la misma sentencia.
        if new and w == "SET" and cur and ("UPDATE" in cur or cur[0] == "MERGE"):
            new = False
        if new:
            stmts.append(cur)
            cur = []
        cur.append(w)
        prev = w
    if cur:
        stmts.append(cur)
    return stmts


def _unfiltered_dml(stmt: list[str]) -> Optional[str]:
    """'DELETE sin WHERE' / 'UPDATE sin WHERE' si la sentencia modifica toda la tabla."""
    verbs = [w for w in stmt if w in ("DELETE", "UPDATE")]
    if not verbs:
        return None
    head = stmt[0]
    if head == "WITH":
        verb = next((w for w in stmt if w in _DML_VERBS), None)
    else:
        verb = head
    if verb not in ("DELETE", "UPDATE"):
        return None
    if verb == "UPDATE" and "STATISTICS" in stmt[:3]:
        return None
    if "WHERE" in stmt:
        return None
    return f"{verb} sin WHERE (afecta todas las filas)"


@dataclass
class Classification:
    risk: str  # read | write | dangerous
    reasons: list[str] = field(default_factory=list)
    verbs: list[str] = field(default_factory=list)
    batches: int = 0
    requires_autocommit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"risk": self.risk, "reasons": self.reasons, "statement_verbs": self.verbs,
                "batches": self.batches, "requires_autocommit": self.requires_autocommit}


def classify(sql: str) -> Classification:
    if not sql or not sql.strip():
        raise ValueError("SQL vacío")
    batches = split_batches(sql)
    if not batches:
        raise ValueError("SQL vacío")
    reasons: list[str] = []
    verbs: list[str] = []
    autocommit = False
    all_read = True
    for b in batches:
        norm = normalize(b)
        if validate_readonly(b) is not None:
            all_read = False
        module = _MODULE_RE.match(norm)
        if module:
            verbs.append(f"{module.group(1).split()[0]} {module.group(2)}")
            continue
        for rx, label in _DANGEROUS:
            if rx.search(norm) and label not in reasons:
                reasons.append(label)
        if any(rx.search(norm) for rx in _NO_TRANSACTION):
            autocommit = True
        for stmt in _statements(_depth0_words(norm)):
            verbs.append(stmt[0])
            why = _unfiltered_dml(stmt)
            if why and why not in reasons:
                reasons.append(why)
    risk = "read" if all_read else ("dangerous" if reasons else "write")
    return Classification(risk=risk, reasons=reasons, verbs=verbs, batches=len(batches),
                          requires_autocommit=autocommit)


# ============================================================
# Auditoría
# ============================================================

_AUDIT_LOCK = threading.Lock()


def audit(settings: Settings, entry: dict[str, Any]) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    try:
        settings.audit_file.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK, open(settings.audit_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:  # pragma: no cover
        logger.warning(f"No se pudo escribir auditoría: {e}")


def read_audit(settings: Settings, limit: int = 50) -> list[dict[str, Any]]:
    if not settings.audit_file.is_file():
        return []
    with _AUDIT_LOCK, open(settings.audit_file, encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ============================================================
# Ejecución
# ============================================================


def _connect_rw(settings: Settings, server: Server, database: Optional[str], autocommit: bool) -> pyodbc.Connection:
    problems = settings.problems()
    if problems:
        raise RuntimeError(f"Configuración incompleta, faltan: {', '.join(problems)}")
    ensure_tunnel(settings, server)
    cnx = pyodbc.connect(db.build_connection_string(settings, server, database, readonly=False),
                         autocommit=autocommit, timeout=settings.login_timeout)
    cnx.timeout = settings.write_timeout
    cur = cnx.cursor()
    cur.execute(f"SET LOCK_TIMEOUT {int(settings.write_lock_timeout_ms)}; SET XACT_ABORT ON;")
    cur.close()
    return cnx


def _sql_error(e: pyodbc.Error) -> str:
    return str(e.args[1]) if len(e.args) > 1 else str(e)


def _messages(cur: pyodbc.Cursor) -> list[str]:
    out = []
    for m in getattr(cur, "messages", None) or []:
        text = m[1] if isinstance(m, tuple) and len(m) > 1 else str(m)
        text = re.sub(r"^\[[^\]]*\]\[[^\]]*\]\[[^\]]*\]\[SQL Server\]", "", str(text)).strip()
        if text:
            out.append(text)
    return out


def _run_batch(cur: pyodbc.Cursor, batch: str, max_rows: int, max_cell: int) -> dict[str, Any]:
    started = time.time()
    cur.execute(batch)
    counts: list[int] = []
    results: list[dict[str, Any]] = []
    messages: list[str] = []
    while True:
        messages += _messages(cur)
        if cur.description is not None:
            cols = [c[0] or f"col{i}" for i, c in enumerate(cur.description)]
            rows = cur.fetchmany(max_rows + 1)
            results.append({
                "columns": cols,
                "rows": [[db.serialize_value(v, max_cell) for v in r] for r in rows[:max_rows]],
                "row_count": min(len(rows), max_rows),
                "truncated": len(rows) > max_rows,
            })
        elif cur.rowcount is not None and cur.rowcount >= 0:
            counts.append(cur.rowcount)
        if not cur.nextset():
            break
    messages += _messages(cur)
    out: dict[str, Any] = {"rows_affected": sum(counts), "elapsed_ms": int((time.time() - started) * 1000)}
    if len(counts) > 1:
        out["rows_affected_per_statement"] = counts
    if results:
        out["result_sets"] = results
    if messages:
        out["messages"] = messages[:200]
    return out


def execute_script(settings: Settings, server: Server, database: Optional[str], sql: str, *,
                   dry_run: bool, use_transaction: bool, max_rows: int,
                   classification: Classification) -> dict[str, Any]:
    batches = split_batches(sql)
    notes: list[str] = []
    if classification.requires_autocommit:
        if dry_run:
            raise ValueError("dry_run no es posible: el script contiene sentencias que SQL Server no admite "
                             "dentro de una transacción (CREATE/ALTER/DROP DATABASE, BACKUP, RESTORE, KILL...)")
        if use_transaction:
            notes.append("Ejecutado en autocommit: contiene sentencias no permitidas dentro de una transacción.")
        use_transaction = False
    if dry_run:
        use_transaction = True
    cnx = _connect_rw(settings, server, database, autocommit=not use_transaction)
    started = time.time()
    done: list[dict[str, Any]] = []
    try:
        cur = cnx.cursor()
        for i, batch in enumerate(batches, 1):
            try:
                res = _run_batch(cur, batch, max_rows, settings.max_cell_chars)
            except pyodbc.Error as e:
                if use_transaction:
                    cnx.rollback()
                applied = "ninguno (se hizo rollback de todo)" if use_transaction else f"lotes 1..{i - 1} ya aplicados"
                raise RuntimeError(f"Error en el lote {i}/{len(batches)}: {_sql_error(e)} — cambios: {applied}") from e
            done.append({"batch": i, **res})
        trancount = None
        try:
            cur.execute("SELECT @@TRANCOUNT")
            trancount = cur.fetchone()[0]
        except pyodbc.Error:
            pass
        if dry_run:
            cnx.rollback()
            status = "dry_run_rolled_back"
            notes.append("dry_run: se ejecutó y se deshizo. Los valores IDENTITY/secuencias consumidos no se "
                         "recuperan y efectos externos de triggers no se revierten.")
        else:
            if use_transaction:
                cnx.commit()
            elif trancount:
                notes.append(f"Quedó una transacción abierta (@@TRANCOUNT={trancount}) que se deshace al cerrar: "
                             "agrega COMMIT al script.")
            status = "committed" if use_transaction else "autocommit"
    finally:
        try:
            cnx.close()  # cerrar sin commit deshace cualquier transacción pendiente
        except pyodbc.Error:  # pragma: no cover
            pass
    total = sum(d["rows_affected"] for d in done)
    out: dict[str, Any] = {
        "status": status,
        "rows_affected": total,
        "batches": done if len(done) > 1 else None,
        "elapsed_ms": int((time.time() - started) * 1000),
        "notes": notes or None,
    }
    if len(done) == 1:
        out.update({k: v for k, v in done[0].items() if k not in ("batch", "rows_affected", "elapsed_ms")})
    return {k: v for k, v in out.items() if v is not None}


def insert_rows(settings: Settings, server: Server, database: Optional[str], schema: str, table: str,
                rows: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    if not rows:
        raise ValueError("rows vacío")
    if len(rows) > settings.write_max_rows:
        raise ValueError(f"Máximo {settings.write_max_rows} filas por llamada (OPT_WRITE_MAX_ROWS)")
    if not all(isinstance(r, dict) and r for r in rows):
        raise ValueError("rows debe ser una lista de objetos {columna: valor}")
    columns: list[str] = []
    for r in rows:
        for k in r:
            if k not in columns:
                columns.append(k)
    target = f"{quote_ident(schema)}.{quote_ident(table)}"
    col_sql = ", ".join(quote_ident(c) for c in columns)
    sql = f"INSERT INTO {target} ({col_sql}) VALUES ({', '.join('?' for _ in columns)})"
    params = [tuple(r.get(c) for c in columns) for r in rows]
    cnx = _connect_rw(settings, server, database, autocommit=False)
    started = time.time()
    try:
        cur = cnx.cursor()
        cur.fast_executemany = True
        try:
            cur.executemany(sql, params)
        except pyodbc.Error as e:
            cnx.rollback()
            raise RuntimeError(f"Error insertando (rollback de todo): {_sql_error(e)}") from e
        if dry_run:
            cnx.rollback()
        else:
            cnx.commit()
    finally:
        cnx.close()
    return {"status": "dry_run_rolled_back" if dry_run else "committed", "table": f"{schema}.{table}",
            "columns": columns, "rows_inserted": len(params), "sql": sql,
            "elapsed_ms": int((time.time() - started) * 1000)}
