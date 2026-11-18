#!/usr/bin/env python3
"""
Redshift MCP Server (solo lectura)
==================================
MCP server (stdio, SDK oficial `mcp` / FastMCP) que expone Amazon Redshift a
traves de la **Redshift Data API** con boto3:

    execute_statement -> describe_statement (polling) -> get_statement_result (paginado)

Es estrictamente de SOLO LECTURA: cualquier SQL que no empiece por SELECT o WITH
es rechazado antes de llegar a AWS, junto con multi-statement y palabras clave de
escritura (INSERT/UPDATE/DELETE/DROP/CREATE/COPY/UNLOAD/SELECT ... INTO, etc.).

Variables de entorno (se leen de D:\\Projects\\MCP\\.env):
  REDSHIFT_CLUSTER_ID        - Identificador del cluster (requerido)
  REDSHIFT_DATABASE          - Base de datos (requerido)
  REDSHIFT_DB_USER           - Usuario de base de datos (requerido)

  REDSHIFT_AWS_REGION        - Region AWS. Fallback: AWS_REGION1, AWS_REGION
  REDSHIFT_AWS_PROFILE       - Perfil de ~/.aws/credentials (o SSO) a usar

Credenciales: NUNCA hardcodeadas. Orden de resolucion:
  1. REDSHIFT_AWS_ACCESS_KEY_ID / REDSHIFT_AWS_SECRET_ACCESS_KEY (+ _SESSION_TOKEN)
  2. AWS_ACCESS_KEY_ID1 / AWS_SECRET_ACCESS_KEY1  (compatibilidad con .env compartido)
  3. REDSHIFT_AWS_PROFILE (perfil / SSO)
  4. Cadena por defecto de boto3 (env AWS_*, perfil, SSO, rol EC2/ECS)

Opcionales:
  REDSHIFT_MAX_ROWS          - Tope de filas devueltas por tool (default: 1000)
  REDSHIFT_QUERY_TIMEOUT     - Timeout en segundos por query (default: 300)
  REDSHIFT_POLL_INTERVAL     - Intervalo inicial de polling, s (default: 0.5)
  REDSHIFT_POLL_MAX_INTERVAL - Intervalo maximo de polling, s (default: 3.0)
  AWS_CA_BUNDLE              - CA del proxy corporativo (Zscaler); boto3 la usa
  AWS_DISABLE_SSL_VERIFY     - "1" desactiva verificacion TLS (ultimo recurso)
  LOG_LEVEL                  - DEBUG/INFO/WARNING/ERROR (default: INFO)

Todos los logs van a stderr (stdout esta reservado para el protocolo MCP).
"""

import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values, load_dotenv
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# -- Logging a stderr -------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - [redshift-readonly-mcp] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("redshift-readonly-mcp")

# -- Configuracion ----------------------------------------------------------
CLUSTER_ID = os.environ.get("REDSHIFT_CLUSTER_ID", "").strip()
DATABASE = os.environ.get("REDSHIFT_DATABASE", "").strip()
DB_USER = os.environ.get("REDSHIFT_DB_USER", "").strip()

AWS_REGION = (
    os.environ.get("REDSHIFT_AWS_REGION")
    or os.environ.get("AWS_REGION1")
    or os.environ.get("AWS_REGION")
    or "us-east-1"
).strip()
AWS_PROFILE = os.environ.get("REDSHIFT_AWS_PROFILE", "").strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("%s invalido, usando %s", name, default)
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("%s invalido, usando %s", name, default)
        return default


MAX_ROWS = max(1, _int_env("REDSHIFT_MAX_ROWS", 1000))
QUERY_TIMEOUT = max(1, _int_env("REDSHIFT_QUERY_TIMEOUT", 300))
POLL_INTERVAL = max(0.05, _float_env("REDSHIFT_POLL_INTERVAL", 0.5))
POLL_MAX_INTERVAL = max(POLL_INTERVAL, _float_env("REDSHIFT_POLL_MAX_INTERVAL", 3.0))
SKIP_SSL = os.environ.get("AWS_DISABLE_SSL_VERIFY", "").lower() in ("1", "true", "yes")
CA_BUNDLE = os.environ.get("AWS_CA_BUNDLE", "").strip()


def _verify_setting() -> Any:
    """verify= para boto3: CA corporativa (Zscaler) si existe, False si se desactiva TLS."""
    if SKIP_SSL:
        logger.warning("Verificacion TLS DESACTIVADA (AWS_DISABLE_SSL_VERIFY=1)")
        return False
    if CA_BUNDLE and Path(CA_BUNDLE).is_file():
        return CA_BUNDLE
    if CA_BUNDLE:
        logger.warning("AWS_CA_BUNDLE apunta a un archivo inexistente: %s", CA_BUNDLE)
    return True

# -- Guardia de solo lectura ------------------------------------------------
# Palabras que nunca pueden aparecer en un SQL de solo lectura. Se evaluan sobre
# el SQL con comentarios y literales/identificadores citados ya eliminados, con
# limite de palabra (\b), asi que columnas tipo `insert_ts` o `update_date` no
# disparan el filtro.
FORBIDDEN = (
    "insert", "update", "delete", "merge", "upsert",
    "create", "alter", "drop", "truncate", "rename",
    "grant", "revoke", "copy", "unload", "into",
    "vacuum", "analyze", "commit", "rollback", "begin", "start",
    "lock", "set", "reset", "prepare", "execute", "deallocate",
    "call", "refresh", "attach", "detach",
    "pg_terminate_backend", "pg_cancel_backend",
)
FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN) + r")\b", re.IGNORECASE)


class ReadOnlyViolation(ValueError):
    """El SQL solicitado no es una consulta de solo lectura."""


def _strip_noise(sql: str) -> str:
    """Elimina comentarios (-- y /* */), literales '...' e identificadores "..."."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        if ch == "'" or ch == '"':
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote and j + 1 < n and sql[j + 1] == quote:
                    j += 2
                elif sql[j] == quote:
                    j += 1
                    break
                else:
                    j += 1
            i = j
            out.append(" literal " if quote == "'" else " ident ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def ensure_read_only(sql: str) -> str:
    """Valida que `sql` sea una unica consulta SELECT/WITH. Devuelve el SQL limpio."""
    if not sql or not sql.strip():
        raise ReadOnlyViolation("SQL vacio.")

    cleaned = _strip_noise(sql).strip()
    # Un solo statement: se tolera un unico ';' final.
    body = cleaned.rstrip().rstrip(";")
    if ";" in body:
        raise ReadOnlyViolation(
            "Solo se permite un statement por llamada (se encontro ';' intermedio)."
        )
    if not body.strip():
        raise ReadOnlyViolation("El SQL solo contiene comentarios.")

    first = re.match(r"^\(*\s*([a-zA-Z_]+)", body.strip())
    keyword = (first.group(1) if first else "").upper()
    if keyword not in ("SELECT", "WITH"):
        raise ReadOnlyViolation(
            "Solo se permiten consultas que empiecen por SELECT o WITH "
            f"(recibido: {keyword or '?'})."
        )

    hit = FORBIDDEN_RE.search(body)
    if hit:
        raise ReadOnlyViolation(
            "Palabra clave de escritura no permitida en un server de solo lectura: "
            f"'{hit.group(1).upper()}'."
        )

    return sql.strip().rstrip(";").strip()


# -- Cliente boto3 ----------------------------------------------------------
_client = None


def get_client():
    """Crea (y cachea) el cliente redshift-data resolviendo credenciales sin hardcodear."""
    global _client
    if _client is not None:
        return _client

    session_kwargs: dict[str, Any] = {"region_name": AWS_REGION}
    credentials_file = os.environ.get("REDSHIFT_CREDENTIALS_FILE", "").strip()
    if credentials_file:
        path = Path(credentials_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        values = dotenv_values(path)
        access = values.get("aws_access_key_id") or values.get("AWS_ACCESS_KEY_ID")
        secret = values.get("aws_secret_access_key") or values.get("AWS_SECRET_ACCESS_KEY")
        token = values.get("aws_session_token") or values.get("AWS_SESSION_TOKEN")
        if not access or not secret:
            raise ValueError(f"Faltan credenciales en {path}")
    else:
        access = os.environ.get("REDSHIFT_AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID1")
        secret = os.environ.get("REDSHIFT_AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY1")
        token = os.environ.get("REDSHIFT_AWS_SESSION_TOKEN")

    if access and secret:
        session_kwargs["aws_access_key_id"] = access
        session_kwargs["aws_secret_access_key"] = secret
        if token:
            session_kwargs["aws_session_token"] = token
        origin = f"archivo {credentials_file}" if credentials_file else "credenciales del .env (REDSHIFT_* / AWS_*1)"
    elif AWS_PROFILE:
        session_kwargs["profile_name"] = AWS_PROFILE
        origin = f"perfil boto3 '{AWS_PROFILE}'"
    else:
        origin = "cadena por defecto de boto3 (perfil/SSO/rol)"

    session = boto3.Session(**session_kwargs)
    expected = os.environ.get("REDSHIFT_AWS_ACCOUNT_ID", "").strip()
    if expected:
        actual = session.client("sts", verify=_verify_setting()).get_caller_identity()["Account"]
        if actual != expected:
            raise ValueError(f"Cuenta incorrecta: {actual}; se requiere {expected}.")
    _client = session.client(
        "redshift-data",
        verify=_verify_setting(),
        config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}),
    )
    logger.info("Cliente redshift-data listo (region=%s, %s)", AWS_REGION, origin)
    return _client


def _target_kwargs() -> dict[str, str]:
    return {"ClusterIdentifier": CLUSTER_ID, "Database": DATABASE, "DbUser": DB_USER}


# -- Ejecucion (execute -> poll -> paginar) ---------------------------------
def _field_value(field: dict[str, Any]) -> Any:
    if not field or field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in field:
            return field[key]
    if "blobValue" in field:
        blob = field["blobValue"]
        return base64.b64encode(blob).decode("ascii") if isinstance(blob, bytes) else str(blob)
    if "arrayValue" in field:
        array = field["arrayValue"] or {}
        for values in array.values():
            return list(values)
        return []
    return next(iter(field.values()), None)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name in seen:
            seen[name] += 1
            out.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 1
            out.append(name)
    return out


def _wait(client, statement_id: str, timeout_s: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    interval = POLL_INTERVAL
    while True:
        desc = client.describe_statement(Id=statement_id)
        status = desc.get("Status")
        if status == "FINISHED":
            return desc
        if status in ("FAILED", "ABORTED"):
            raise RuntimeError(
                f"Query {status}: {desc.get('Error') or 'sin detalle'} (id={statement_id})"
            )
        if time.monotonic() >= deadline:
            try:
                client.cancel_statement(Id=statement_id)
                logger.warning("Timeout: statement %s cancelado", statement_id)
            except (ClientError, BotoCoreError) as exc:
                logger.warning("No se pudo cancelar %s: %s", statement_id, exc)
            raise TimeoutError(
                f"Timeout de {timeout_s}s esperando la query "
                f"(id={statement_id}, ultimo estado={status})."
            )
        time.sleep(min(interval, max(0.05, deadline - time.monotonic())))
        interval = min(interval * 1.5, POLL_MAX_INTERVAL)


def _fetch(client, statement_id: str, max_rows: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    columns: list[dict[str, str]] = []
    names: list[str] = []
    next_token = None
    truncated = False

    while True:
        kwargs: dict[str, Any] = {"Id": statement_id}
        if next_token:
            kwargs["NextToken"] = next_token
        page = client.get_statement_result(**kwargs)

        if not columns:
            meta = page.get("ColumnMetadata", [])
            names = _dedupe([col.get("name") or f"col_{i}" for i, col in enumerate(meta)])
            columns = [
                {"name": names[i], "type": col.get("typeName", "unknown")}
                for i, col in enumerate(meta)
            ]

        for record in page.get("Records", []):
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append({names[i]: _field_value(f) for i, f in enumerate(record)})

        next_token = page.get("NextToken")
        if truncated or not next_token:
            truncated = truncated or bool(next_token)
            break

    return {"columns": columns, "rows": rows, "truncated": truncated}


def _run(sql: str, timeout_s: int | None, max_rows: int | None,
         parameters: list[dict[str, str]] | None = None) -> dict[str, Any]:
    client = get_client()
    timeout = max(1, int(timeout_s)) if timeout_s else QUERY_TIMEOUT
    limit = min(max(1, int(max_rows)), MAX_ROWS) if max_rows else MAX_ROWS

    kwargs: dict[str, Any] = dict(_target_kwargs())
    kwargs["Sql"] = sql
    if parameters:
        kwargs["Parameters"] = parameters

    started = time.monotonic()
    statement_id = client.execute_statement(**kwargs)["Id"]
    logger.info("Statement %s enviado (timeout=%ss, max_rows=%s)", statement_id, timeout, limit)

    desc = _wait(client, statement_id, timeout)
    result: dict[str, Any] = {
        "statement_id": statement_id,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "redshift_duration_ms": round(desc.get("Duration", 0) / 1_000_000, 1),
        "total_rows": desc.get("ResultRows", -1),
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
    }

    if not desc.get("HasResultSet"):
        logger.info("Statement %s finalizo sin result set", statement_id)
        return result

    result.update(_fetch(client, statement_id, limit))
    result["row_count"] = len(result["rows"])
    logger.info(
        "Statement %s: %s filas devueltas (truncado=%s)",
        statement_id, result["row_count"], result["truncated"],
    )
    return result


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _error(exc: Exception, **extra: Any) -> str:
    logger.error("%s: %s", type(exc).__name__, exc)
    payload: dict[str, Any] = {"error": str(exc), "error_type": type(exc).__name__}
    payload.update(extra)
    return _json(payload)


# -- Tools MCP --------------------------------------------------------------
mcp = FastMCP("redshift-readonly")


@mcp.tool()
def run_query(sql: str, timeout_s: int | None = None, max_rows: int | None = None) -> str:
    """Ejecuta una consulta de SOLO LECTURA en Redshift via Data API y devuelve las filas en JSON.

    Solo se aceptan sentencias unicas que empiecen por SELECT o WITH; cualquier
    otra cosa (INSERT/UPDATE/DELETE/DDL/COPY/UNLOAD/SELECT ... INTO, multi-statement)
    se rechaza sin llegar a AWS.

    Args:
        sql: La consulta SELECT o WITH ... SELECT.
        timeout_s: Timeout en segundos para esta query (default: REDSHIFT_QUERY_TIMEOUT).
        max_rows: Tope de filas a devolver (se acota a REDSHIFT_MAX_ROWS).

    Returns:
        JSON con columns, rows, row_count, total_rows, truncated, statement_id y tiempos.
    """
    try:
        safe_sql = ensure_read_only(sql)
    except ReadOnlyViolation as exc:
        return _error(exc, hint="Este MCP server es de solo lectura (SELECT / WITH).")

    try:
        return _json(_run(safe_sql, timeout_s, max_rows))
    except (ClientError, BotoCoreError, RuntimeError, TimeoutError) as exc:
        return _error(exc)


@mcp.tool()
def describe_table(schema: str, table: str) -> str:
    """Describe las columnas de una tabla (svv_columns): nombre, tipo, nullable, default y posicion.

    Args:
        schema: Esquema de la tabla (ej: public).
        table: Nombre de la tabla o vista.
    """
    sql = """
        SELECT table_schema, table_name, ordinal_position, column_name,
               data_type, character_maximum_length, numeric_precision, numeric_scale,
               is_nullable, column_default, remarks
        FROM svv_columns
        WHERE table_schema = :schema AND table_name = :table
        ORDER BY ordinal_position
    """
    params = [{"name": "schema", "value": schema}, {"name": "table", "value": table}]
    try:
        result = _run(sql, None, MAX_ROWS, params)
        if not result["rows"]:
            result["warning"] = (
                f"No se encontraron columnas para {schema}.{table} (revisa nombre o permisos)."
            )
        return _json(result)
    except (ClientError, BotoCoreError, RuntimeError, TimeoutError) as exc:
        return _error(exc, schema=schema, table=table)


@mcp.tool()
def list_tables(schema: str, pattern: str | None = None) -> str:
    """Lista tablas y vistas de un esquema (svv_tables), opcionalmente filtradas por patron LIKE.

    Args:
        schema: Esquema a listar (ej: public).
        pattern: Patron SQL LIKE sobre el nombre, ej '%kpi%'. Si se omite, lista todo.
    """
    params = [{"name": "schema", "value": schema}]
    where = "table_schema = :schema"
    if pattern:
        where += " AND table_name LIKE :pattern"
        params.append({"name": "pattern", "value": pattern})

    sql = f"""
        SELECT table_schema, table_name, table_type, remarks
        FROM svv_tables
        WHERE {where}
        ORDER BY table_name
    """
    try:
        return _json(_run(sql, None, MAX_ROWS, params))
    except (ClientError, BotoCoreError, RuntimeError, TimeoutError) as exc:
        return _error(exc, schema=schema, pattern=pattern)


@mcp.tool()
def test_connection() -> str:
    """Verifica la conexion: cluster, base de datos, usuario efectivo y version de Redshift."""
    try:
        result = _run(
            "SELECT current_user AS db_user, current_database() AS database, version() AS version",
            30, 1,
        )
        result["config"] = {
            "cluster": CLUSTER_ID,
            "database": DATABASE,
            "db_user": DB_USER,
            "region": AWS_REGION,
            "max_rows": MAX_ROWS,
            "query_timeout_s": QUERY_TIMEOUT,
            "read_only": True,
        }
        return _json(result)
    except (ClientError, BotoCoreError, RuntimeError, TimeoutError) as exc:
        return _error(exc, cluster=CLUSTER_ID, database=DATABASE, region=AWS_REGION)


def main() -> None:
    missing = [
        name for name, value in (
            ("REDSHIFT_CLUSTER_ID", CLUSTER_ID),
            ("REDSHIFT_DATABASE", DATABASE),
            ("REDSHIFT_DB_USER", DB_USER),
        ) if not value
    ]
    if missing:
        logger.error("Faltan variables de entorno requeridas: %s", ", ".join(missing))
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("Redshift MCP Server (SOLO LECTURA)")
    logger.info("  Cluster:  %s", CLUSTER_ID)
    logger.info("  Database: %s", DATABASE)
    logger.info("  DB user:  %s", DB_USER)
    logger.info("  Region:   %s", AWS_REGION)
    logger.info("  Max rows: %s | Timeout: %ss | Poll: %.2f-%.2fs",
                MAX_ROWS, QUERY_TIMEOUT, POLL_INTERVAL, POLL_MAX_INTERVAL)
    logger.info("=" * 50)
    mcp.run()


if __name__ == "__main__":
    main()
