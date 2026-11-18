"""Structured AWS data engineering tools registered on the existing Athena MCP.

All mutating tools default to a plan (dry_run=True). This module never logs
credentials, request bodies, SQL text, or AWS exception messages.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


_CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"mode": "standard", "max_attempts": 3})
_MAX_TEXT = 1_000_000
_MAX_ARTIFACT = 100 * 1024 * 1024
_ARTIFACT_ROOT = Path(__file__).resolve().parent / "aws_artifacts"
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")


def _json(value: Any) -> str:
    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {key: ("[REDACTED]" if re.search(
                r"password|secret|access[_-]?key|session[_-]?token|authorization|credential",
                str(key), re.IGNORECASE) else scrub(item)) for key, item in obj.items()}
        if isinstance(obj, list):
            return [scrub(item) for item in obj]
        return obj

    def default(obj: Any) -> str:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(type(obj).__name__)
    return json.dumps(scrub(value), ensure_ascii=False, default=default)


def _safe_error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "AWSClientError")
        return {"error": str(code), "operation": exc.operation_name}
    if isinstance(exc, BotoCoreError):
        return {"error": type(exc).__name__}
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return {"error": type(exc).__name__, "message": str(exc)[:240]}
    return {"error": type(exc).__name__}


def _call(fn: Callable[[], Any]) -> str:
    try:
        return _json(fn())
    except Exception as exc:
        return _json(_safe_error(exc))


def _name(value: str, label: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"{label} debe contener solo letras, números, _, . o - (máximo 255).")
    return value


def _artifact_path(relative_path: str) -> Path:
    if not relative_path or relative_path in (".", ".."):
        raise ValueError("artifact_path debe nombrar un archivo bajo aws_artifacts.")
    root = _ARTIFACT_ROOT.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("artifact_path debe permanecer bajo aws_artifacts.")
    return path


def _client(service: str, *, credential_values: Mapping[str, str] | None = None) -> Any:
    env = credential_values if credential_values is not None else os.environ
    kwargs: dict[str, Any] = {"region_name": env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
                                       or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")}
    access = env.get("AWS_ACCESS_KEY_ID")
    secret = env.get("AWS_SECRET_ACCESS_KEY")
    if access and secret:
        kwargs.update(aws_access_key_id=access, aws_secret_access_key=secret)
        token = env.get("AWS_SESSION_TOKEN")
        if token:
            kwargs["aws_session_token"] = token
    session = boto3.Session(**kwargs)
    bundle = os.environ.get("AWS_CA_BUNDLE", "").strip().strip('"')
    verify: str | bool = bundle if bundle and os.path.isfile(bundle) else not (
        os.environ.get("AWS_DISABLE_SSL_VERIFY", "").lower() in ("1", "true", "yes")
    )
    return session.client(service, config=_CONFIG, verify=verify)


def _pages(client: Any, operation: str, result_key: str, *, limit: int = 100, **kwargs: Any) -> list[Any]:
    limit = min(max(1, limit), 500)
    result: list[Any] = []
    paginator = client.get_paginator(operation)
    for page in paginator.paginate(**kwargs):
        result.extend(page.get(result_key, []))
        if len(result) >= limit:
            break
    return result[:limit]


def _plan(service: str, operation: str, parameters: dict[str, Any], dry_run: bool) -> dict[str, Any] | None:
    if dry_run:
        return {"status": "planned", "service": service, "operation": operation, "parameters": parameters,
                "next_step": "Revisar el plan y repetir con dry_run=false para aplicar."}
    return None


def register_tools(mcp: Any) -> None:
    """Attach new tools without replacing the original Athena tool names."""

    @mcp.tool()
    def aws_identity() -> str:
        """Devuelve cuenta, ARN y región mediante STS GetCallerIdentity."""
        def run() -> dict[str, Any]:
            result = _client("sts").get_caller_identity()
            return {"account": result["Account"], "arn": result["Arn"],
                    "region": os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")}
        return _call(run)

    @mcp.tool()
    def aws_capabilities() -> str:
        """Sondea permisos de lectura con operaciones List/Get seguras; escritura queda sin verificar."""
        probes = {
            "STS": ("sts", "get_caller_identity", {}),
            "Athena": ("athena", "list_work_groups", {"MaxResults": 1}),
            "S3": ("s3", "list_buckets", {}),
            "Glue Catalog": ("glue", "get_databases", {"MaxResults": 1}),
            "Glue Jobs": ("glue", "get_jobs", {"MaxResults": 1}),
            "Glue Crawlers": ("glue", "get_crawlers", {"MaxResults": 1}),
            "Lambda": ("lambda", "list_functions", {"MaxItems": 1}),
            "EventBridge": ("events", "list_rules", {"Limit": 1}),
            "Step Functions": ("stepfunctions", "list_state_machines", {"maxResults": 1}),
            "CloudWatch Logs": ("logs", "describe_log_groups", {"limit": 1}),
        }
        result: dict[str, Any] = {}
        for label, (service, operation, params) in probes.items():
            try:
                getattr(_client(service), operation)(**params)
                result[label] = {"read_probe": "available", "operation": operation}
            except Exception as exc:
                result[label] = {"read_probe": "unavailable", "operation": operation, **_safe_error(exc)}
        return _json({"services": result, "write_create_modify_execute": "not_tested",
                      "note": "Un List permitido no demuestra permisos sobre todos los recursos."})

    @mcp.tool()
    def athena_list_workgroups(limit: int = 100) -> str:
        """Lista workgroups Athena con paginación acotada."""
        def run() -> Any:
            client = _client("athena")
            cap = min(max(limit, 1), 500)
            groups: list[Any] = []
            token = ""
            while len(groups) < cap:
                params: dict[str, Any] = {"MaxResults": min(50, cap - len(groups))}
                if token:
                    params["NextToken"] = token
                result = client.list_work_groups(**params)
                groups.extend(result.get("WorkGroups", []))
                token = result.get("NextToken", "")
                if not token:
                    break
            return groups[:cap]
        return _call(run)

    @mcp.tool()
    def athena_start_query(sql: str, database: str = "", dry_run: bool = True,
                           allow_destructive: bool = False) -> str:
        """Planifica o inicia SQL Athena. DROP/DELETE y reemplazos requieren allow_destructive."""
        def run() -> Any:
            from athena_mcp_server import _check_sql_intent, ATHENA_DATABASE, ATHENA_OUTPUT_S3, ATHENA_WORKGROUP
            _check_sql_intent(sql, allow_destructive)
            params: dict[str, Any] = {"workgroup": ATHENA_WORKGROUP, "database": database or ATHENA_DATABASE,
                                       "sql": sql}
            plan = _plan("Athena", "StartQueryExecution", params, dry_run)
            if plan:
                return plan
            request: dict[str, Any] = {"QueryString": sql, "WorkGroup": ATHENA_WORKGROUP}
            if database or ATHENA_DATABASE:
                request["QueryExecutionContext"] = {"Database": database or ATHENA_DATABASE}
            if ATHENA_OUTPUT_S3:
                request["ResultConfiguration"] = {"OutputLocation": ATHENA_OUTPUT_S3}
            response = _client("athena").start_query_execution(**request)
            return {"query_execution_id": response["QueryExecutionId"]}
        return _call(run)

    @mcp.tool()
    def athena_wait_query(query_execution_id: str, timeout_seconds: int = 120) -> str:
        """Espera una query existente y devuelve estado, razón y estadísticas resumidas."""
        def run() -> Any:
            client = _client("athena")
            deadline = time.monotonic() + min(max(timeout_seconds, 1), 600)
            while True:
                query = client.get_query_execution(QueryExecutionId=query_execution_id)["QueryExecution"]
                status = query["Status"]
                state = status["State"]
                if state in ("SUCCEEDED", "FAILED", "CANCELLED") or time.monotonic() >= deadline:
                    return {"query_execution_id": query_execution_id, "state": state,
                            "reason": status.get("StateChangeReason", "")[:500],
                            "statistics": query.get("Statistics", {})}
                time.sleep(1)
        return _call(run)

    @mcp.tool()
    def athena_get_results(query_execution_id: str, max_rows: int = 100, next_token: str = "") -> str:
        """Obtiene una página de resultados Athena sin reejecutar SQL."""
        def run() -> Any:
            params: dict[str, Any] = {"QueryExecutionId": query_execution_id,
                                      "MaxResults": min(max(max_rows, 1), 1000)}
            if next_token:
                params["NextToken"] = next_token
            resp = _client("athena").get_query_results(**params)
            data = resp["ResultSet"]
            columns = [c["Name"] for c in data["ResultSetMetadata"]["ColumnInfo"]]
            rows = [[v.get("VarCharValue") for v in row["Data"]] for row in data["Rows"]]
            if not next_token and rows and rows[0] == columns:
                rows = rows[1:]
            return {"columns": columns, "rows": rows,
                    "next_token": resp.get("NextToken")}
        return _call(run)

    @mcp.tool()
    def s3_list_buckets() -> str:
        """Lista buckets visibles para la identidad actual."""
        return _call(lambda: [b["Name"] for b in _client("s3").list_buckets().get("Buckets", [])])

    @mcp.tool()
    def s3_list_objects(bucket: str, prefix: str = "", limit: int = 100) -> str:
        """Lista objetos bajo un prefix, incluidos tamaño y fecha, con límite."""
        def run() -> Any:
            objects = _pages(_client("s3"), "list_objects_v2", "Contents", limit=limit,
                             Bucket=_name(bucket, "bucket"), Prefix=prefix)
            return [{"key": obj["Key"], "size": obj["Size"], "last_modified": obj["LastModified"]}
                    for obj in objects]
        return _call(run)

    @mcp.tool()
    def s3_head_object(bucket: str, key: str) -> str:
        """Comprueba existencia y metadata de un objeto S3."""
        def run() -> Any:
            obj = _client("s3").head_object(Bucket=_name(bucket, "bucket"), Key=key)
            return {"exists": True, "size": obj["ContentLength"], "content_type": obj.get("ContentType"),
                    "last_modified": obj.get("LastModified"), "etag": obj.get("ETag")}
        return _call(run)

    @mcp.tool()
    def s3_read_text(bucket: str, key: str, max_bytes: int = 100_000) -> str:
        """Lee UTF-8 (SQL, CSV, JSON, config) hasta un límite; no descarga binarios."""
        def run() -> Any:
            cap = min(max(max_bytes, 1), _MAX_TEXT)
            client = _client("s3")
            name = _name(bucket, "bucket")
            head = client.head_object(Bucket=name, Key=key)
            if head["ContentLength"] == 0:
                return {"text": "", "truncated": False, "content_type": head.get("ContentType")}
            obj = client.get_object(Bucket=name, Key=key, Range=f"bytes=0-{cap}")
            try:
                raw = obj["Body"].read(cap + 1)
            finally:
                obj["Body"].close()
            return {"text": raw[:cap].decode("utf-8"), "truncated": head["ContentLength"] > cap,
                    "content_type": obj.get("ContentType")}
        return _call(run)

    @mcp.tool()
    def s3_write_text(bucket: str, key: str, text: str, content_type: str = "text/plain; charset=utf-8",
                      dry_run: bool = True, overwrite: bool = False) -> str:
        """Planifica o escribe texto UTF-8 en S3; reemplazo requiere overwrite=true."""
        def run() -> Any:
            payload = text.encode("utf-8")
            if len(payload) > _MAX_TEXT:
                raise ValueError("El contenido supera 1 MB.")
            name = _name(bucket, "bucket")
            plan = _plan("S3", "PutObject", {"bucket": name, "key": key, "bytes": len(payload),
                                               "content_type": content_type, "overwrite": overwrite}, dry_run)
            if plan:
                return plan
            client = _client("s3")
            if not overwrite:
                try:
                    client.head_object(Bucket=name, Key=key)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                        raise
                else:
                    raise ValueError("El objeto ya existe; usa overwrite=true con intención explícita.")
            request = {"Bucket": name, "Key": key, "Body": payload, "ContentType": content_type}
            if not overwrite:
                request["IfNoneMatch"] = "*"
            response = client.put_object(**request)
            return {"status": "written", "etag": response.get("ETag"), "bytes": len(payload)}
        return _call(run)

    @mcp.tool()
    def s3_upload_artifact(bucket: str, key: str, artifact_path: str,
                           content_type: str = "application/octet-stream", dry_run: bool = True,
                           overwrite: bool = False) -> str:
        """Sube hasta 100 MB desde aws_artifacts/; plan previo y reemplazo explícito."""
        def run() -> Any:
            name = _name(bucket, "bucket")
            path = _artifact_path(artifact_path)
            if not path.is_file():
                raise ValueError("El archivo no existe bajo aws_artifacts.")
            size = path.stat().st_size
            if size > _MAX_ARTIFACT:
                raise ValueError("El artefacto supera 100 MB.")
            plan = _plan("S3", "PutObject", {"bucket": name, "key": key,
                                               "artifact_path": str(path.relative_to(_ARTIFACT_ROOT.resolve())),
                                               "bytes": size, "overwrite": overwrite}, dry_run)
            if plan:
                return plan
            request: dict[str, Any] = {"Bucket": name, "Key": key, "ContentType": content_type,
                                       "ContentLength": size}
            if not overwrite:
                request["IfNoneMatch"] = "*"
            with path.open("rb") as handle:
                request["Body"] = handle
                response = _client("s3").put_object(**request)
            return {"status": "uploaded", "bytes": size, "etag": response.get("ETag")}
        return _call(run)

    @mcp.tool()
    def s3_download_artifact(bucket: str, key: str, artifact_path: str,
                             dry_run: bool = True, overwrite: bool = False) -> str:
        """Descarga hasta 100 MB a aws_artifacts/; plan previo y reemplazo explícito."""
        def run() -> Any:
            name = _name(bucket, "bucket")
            path = _artifact_path(artifact_path)
            plan = _plan("S3", "GetObject", {"bucket": name, "key": key,
                                               "artifact_path": str(path.relative_to(_ARTIFACT_ROOT.resolve())),
                                               "overwrite": overwrite}, dry_run)
            if plan:
                return plan
            if path.exists() and not overwrite:
                raise ValueError("El archivo local ya existe; usa overwrite=true con intención explícita.")
            client = _client("s3")
            metadata = client.head_object(Bucket=name, Key=key)
            size = metadata["ContentLength"]
            if size > _MAX_ARTIFACT:
                raise ValueError("El objeto supera 100 MB.")
            path.parent.mkdir(parents=True, exist_ok=True)
            obj = client.get_object(Bucket=name, Key=key)
            temp_path: Path | None = None
            created_target = False
            try:
                if overwrite:
                    temp = tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False)
                    temp_path = Path(temp.name)
                    output = temp
                else:
                    output = path.open("xb")
                    created_target = True
                with output:
                    remaining = _MAX_ARTIFACT + 1
                    for chunk in obj["Body"].iter_chunks(chunk_size=1024 * 1024):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError("El objeto supera 100 MB.")
                        output.write(chunk)
                if temp_path:
                    os.replace(temp_path, path)
            except Exception:
                if temp_path:
                    temp_path.unlink(missing_ok=True)
                elif created_target:
                    path.unlink(missing_ok=True)
                raise
            finally:
                obj["Body"].close()
            return {"status": "downloaded", "artifact_path": str(path), "bytes": size}
        return _call(run)

    @mcp.tool()
    def glue_list_databases(limit: int = 100) -> str:
        """Lista bases de datos Glue con paginación acotada."""
        return _call(lambda: [d["Name"] for d in _pages(_client("glue"), "get_databases", "DatabaseList", limit=limit)])

    @mcp.tool()
    def glue_get_database(name: str) -> str:
        """Consulta ubicación y propiedades de una base Glue."""
        return _call(lambda: _client("glue").get_database(Name=_name(name, "name"))["Database"])

    @mcp.tool()
    def glue_list_tables(database: str, limit: int = 100) -> str:
        """Lista tablas Glue de una base de datos."""
        return _call(lambda: [{"name": t["Name"], "type": t.get("TableType")}
                              for t in _pages(_client("glue"), "get_tables", "TableList", limit=limit,
                                              DatabaseName=_name(database, "database"))])

    @mcp.tool()
    def glue_get_table(database: str, table: str) -> str:
        """Obtiene schema, storage descriptor, partitions y propiedades de tabla Glue."""
        return _call(lambda: _client("glue").get_table(DatabaseName=_name(database, "database"),
                                                        Name=_name(table, "table"))["Table"])

    @mcp.tool()
    def glue_list_partitions(database: str, table: str, limit: int = 100) -> str:
        """Lista particiones y ubicaciones de una tabla Glue."""
        return _call(lambda: _pages(_client("glue"), "get_partitions", "Partitions", limit=limit,
                                    DatabaseName=_name(database, "database"), TableName=_name(table, "table")))

    @mcp.tool()
    def glue_list_crawlers(limit: int = 100) -> str:
        """Descubre crawlers Glue existentes."""
        return _call(lambda: [{"name": c["Name"], "state": c.get("State")}
                              for c in _pages(_client("glue"), "get_crawlers", "Crawlers", limit=limit)])

    @mcp.tool()
    def glue_create_database(name: str, description: str = "", dry_run: bool = True) -> str:
        """Planifica o crea una base de datos Glue."""
        def run() -> Any:
            value = _name(name, "name")
            plan = _plan("Glue", "CreateDatabase", {"name": value, "description": description}, dry_run)
            if plan:
                return plan
            _client("glue").create_database(DatabaseInput={"Name": value, "Description": description})
            return {"status": "created", "name": value}
        return _call(run)

    @mcp.tool()
    def glue_put_table(database: str, table_input: dict[str, Any], dry_run: bool = True,
                       update_existing: bool = False) -> str:
        """Planifica o crea/actualiza una tabla Glue con TableInput estructurado."""
        def run() -> Any:
            db = _name(database, "database")
            if not isinstance(table_input, dict):
                raise ValueError("table_input debe ser un objeto JSON.")
            table = _name(table_input.get("Name", ""), "TableInput.Name")
            if "StorageDescriptor" not in table_input and "ViewOriginalText" not in table_input:
                raise ValueError("Se requiere StorageDescriptor o ViewOriginalText.")
            operation = "UpdateTable" if update_existing else "CreateTable"
            plan = _plan("Glue", operation, {"database": db, "table": table,
                                              "columns": len(table_input.get("StorageDescriptor", {}).get("Columns", [])),
                                              "location": table_input.get("StorageDescriptor", {}).get("Location")}, dry_run)
            if plan:
                return plan
            client = _client("glue")
            if update_existing:
                client.update_table(DatabaseName=db, TableInput=table_input)
            else:
                client.create_table(DatabaseName=db, TableInput=table_input)
            return {"status": "updated" if update_existing else "created", "database": db, "table": table}
        return _call(run)

    @mcp.tool()
    def glue_add_partition(database: str, table: str, values: list[str], location: str,
                           dry_run: bool = True) -> str:
        """Planifica o agrega una partición Glue con valores y ubicación S3 explícitos."""
        def run() -> Any:
            db, tb = _name(database, "database"), _name(table, "table")
            if not location.startswith("s3://"):
                raise ValueError("location debe ser una URI s3://.")
            plan = _plan("Glue", "CreatePartition", {"database": db, "table": tb,
                                                        "values": values, "location": location}, dry_run)
            if plan:
                return plan
            client = _client("glue")
            source = client.get_table(DatabaseName=db, Name=tb)["Table"]["StorageDescriptor"]
            storage = {key: value for key, value in source.items()
                       if key in ("Columns", "InputFormat", "OutputFormat", "Compressed", "NumberOfBuckets",
                                  "SerdeInfo", "BucketColumns", "SortColumns", "Parameters", "SkewedInfo",
                                  "StoredAsSubDirectories")}
            storage["Location"] = location
            client.create_partition(DatabaseName=db, TableName=tb,
                                    PartitionInput={"Values": values, "StorageDescriptor": storage})
            return {"status": "created", "database": db, "table": tb, "values": values}
        return _call(run)

    @mcp.tool()
    def glue_list_jobs(limit: int = 100) -> str:
        """Lista definiciones de jobs Glue."""
        return _call(lambda: [{"name": j["Name"], "role": j.get("Role"), "command": j.get("Command", {})}
                              for j in _pages(_client("glue"), "get_jobs", "Jobs", limit=limit)])

    @mcp.tool()
    def glue_get_job(name: str) -> str:
        """Obtiene definición de un job Glue existente."""
        def run() -> Any:
            job = _client("glue").get_job(JobName=_name(name, "name"))["Job"]
            return {key: job.get(key) for key in ("Name", "Role", "Command", "GlueVersion",
                    "ExecutionProperty", "MaxCapacity", "WorkerType", "NumberOfWorkers", "Timeout", "Connections")}
        return _call(run)

    @mcp.tool()
    def glue_list_job_runs(name: str, limit: int = 50) -> str:
        """Consulta ejecuciones y estados de un job Glue."""
        return _call(lambda: _pages(_client("glue"), "get_job_runs", "JobRuns", limit=limit,
                                    JobName=_name(name, "name")))

    @mcp.tool()
    def glue_start_job(name: str, arguments: dict[str, str] | None = None, dry_run: bool = True) -> str:
        """Planifica o inicia un job Glue existente."""
        def run() -> Any:
            job = _name(name, "name")
            args = arguments or {}
            plan = _plan("Glue", "StartJobRun", {"name": job, "argument_names": list(args)}, dry_run)
            if plan:
                return plan
            return {"job_run_id": _client("glue").start_job_run(JobName=job, Arguments=args)["JobRunId"]}
        return _call(run)

    @mcp.tool()
    def lambda_list_functions(limit: int = 100) -> str:
        """Lista funciones Lambda visibles."""
        return _call(lambda: [{"name": f["FunctionName"], "runtime": f.get("Runtime"), "state": f.get("State")}
                              for f in _pages(_client("lambda"), "list_functions", "Functions", limit=limit)])

    @mcp.tool()
    def lambda_get_function(name: str) -> str:
        """Consulta configuración de Lambda sin devolver URL temporal del paquete de código."""
        def run() -> Any:
            result = _client("lambda").get_function(FunctionName=_name(name, "name"))
            config = result.get("Configuration", {})
            return {"configuration": {key: config.get(key) for key in ("FunctionName", "FunctionArn", "Runtime",
                    "Role", "Handler", "CodeSize", "Description", "Timeout", "MemorySize", "LastModified",
                    "State", "LastUpdateStatus", "Architectures")}}
        return _call(run)

    @mcp.tool()
    def lambda_invoke(name: str, payload_json: str = "{}", dry_run: bool = True) -> str:
        """Planifica o invoca Lambda de forma síncrona, con respuesta acotada."""
        def run() -> Any:
            function = _name(name, "name")
            json.loads(payload_json)
            plan = _plan("Lambda", "Invoke", {"name": function, "payload_bytes": len(payload_json)}, dry_run)
            if plan:
                return plan
            resp = _client("lambda").invoke(FunctionName=function, InvocationType="RequestResponse",
                                              Payload=payload_json.encode("utf-8"))
            body = resp["Payload"].read(_MAX_TEXT + 1)
            return {"status_code": resp["StatusCode"], "function_error": resp.get("FunctionError"),
                    "response": body[:_MAX_TEXT].decode("utf-8", errors="replace"), "truncated": len(body) > _MAX_TEXT}
        return _call(run)

    @mcp.tool()
    def eventbridge_list_rules(limit: int = 100) -> str:
        """Lista reglas EventBridge existentes."""
        return _call(lambda: _pages(_client("events"), "list_rules", "Rules", limit=limit))

    @mcp.tool()
    def eventbridge_get_rule(name: str) -> str:
        """Consulta una regla EventBridge y sus targets."""
        def run() -> Any:
            client = _client("events")
            rule = _name(name, "name")
            return {"rule": client.describe_rule(Name=rule),
                    "targets": _pages(client, "list_targets_by_rule", "Targets", Rule=rule, limit=100)}
        return _call(run)

    @mcp.tool()
    def stepfunctions_list_state_machines(limit: int = 100) -> str:
        """Lista state machines Step Functions existentes."""
        return _call(lambda: _pages(_client("stepfunctions"), "list_state_machines", "stateMachines", limit=limit))

    @mcp.tool()
    def stepfunctions_get_state_machine(arn: str) -> str:
        """Consulta definición y configuración de una state machine."""
        return _call(lambda: _client("stepfunctions").describe_state_machine(stateMachineArn=arn))

    @mcp.tool()
    def stepfunctions_start_execution(arn: str, input_json: str = "{}", dry_run: bool = True) -> str:
        """Planifica o inicia una ejecución Step Functions."""
        def run() -> Any:
            json.loads(input_json)
            plan = _plan("Step Functions", "StartExecution", {"arn": arn, "input_bytes": len(input_json)}, dry_run)
            if plan:
                return plan
            result = _client("stepfunctions").start_execution(stateMachineArn=arn, input=input_json)
            return {"execution_arn": result["executionArn"], "start_date": result["startDate"]}
        return _call(run)

    @mcp.tool()
    def stepfunctions_get_execution(arn: str) -> str:
        """Consulta estado y tiempos de una ejecución Step Functions."""
        def run() -> Any:
            result = _client("stepfunctions").describe_execution(executionArn=arn)
            return {key: result.get(key) for key in ("executionArn", "stateMachineArn", "status", "startDate", "stopDate", "error")}
        return _call(run)

    @mcp.tool()
    def cloudwatch_list_log_streams(log_group: str, limit: int = 50) -> str:
        """Lista streams recientes de un grupo CloudWatch Logs."""
        return _call(lambda: _pages(_client("logs"), "describe_log_streams", "logStreams", limit=limit,
                                    logGroupName=log_group, orderBy="LastEventTime", descending=True))

    @mcp.tool()
    def cloudwatch_get_log_events(log_group: str, log_stream: str, limit: int = 100) -> str:
        """Lee eventos acotados de CloudWatch Logs para diagnosticar pipelines."""
        def run() -> Any:
            response = _client("logs").get_log_events(logGroupName=log_group, logStreamName=log_stream,
                                                         limit=min(max(limit, 1), 500), startFromHead=False)
            return response.get("events", [])
        return _call(run)
