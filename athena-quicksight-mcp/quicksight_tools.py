"""QuickSight asset workflow: discovery, dataset, analysis, dashboard, publication.

Mutations require dry_run=False. No API response or plan includes credentials.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import botocore.session
from botocore.validate import validate_parameters
from dotenv import dotenv_values

from aws_data_tools import _call, _client as _base_client, _json, _plan


_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_COLUMN_TYPES = {"STRING", "INTEGER", "DECIMAL", "DATETIME", "BIT", "BOOLEAN", "JSON", "SEMISTRUCT"}
_DASHBOARD_READ_ACTIONS = ["quicksight:DescribeDashboard", "quicksight:QueryDashboard",
                           "quicksight:ListDashboardVersions"]


def _client(service: str) -> Any:
    """Load QuickSight credentials from QUICKSIGHT_ENV_FILE or ./quicksight.env."""
    configured = os.environ.get("QUICKSIGHT_ENV_FILE", "").strip()
    source = Path(configured) if configured else Path(__file__).resolve().parent / "quicksight.env"
    if not source.is_file():
        raise ValueError("No existe el archivo de credenciales QuickSight; no se usa otra cuenta como fallback.")
    values = dotenv_values(source)
    access = values.get("aws_access_key_id") or values.get("AWS_ACCESS_KEY_ID")
    secret = values.get("aws_secret_access_key") or values.get("AWS_SECRET_ACCESS_KEY")
    token = values.get("aws_session_token") or values.get("AWS_SESSION_TOKEN")
    if not access or not secret:
        raise ValueError("El archivo QuickSight requiere access key y secret key.")
    credentials = {"AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": secret}
    if token:
        credentials["AWS_SESSION_TOKEN"] = token
    return _base_client(service, credential_values=credentials)


def _id(value: str, label: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} debe contener letras, números, guiones o _, máximo 512.")
    return value


def _account(account_id: str = "") -> str:
    if account_id:
        if not re.fullmatch(r"[0-9]{12}", account_id):
            raise ValueError("aws_account_id debe tener 12 dígitos.")
        return account_id
    return _client("sts").get_caller_identity()["Account"]


def _list(operation: str, result_key: str, limit: int = 100, account_id: str = "", **kwargs: Any) -> list[Any]:
    client = _client("quicksight")
    account = _account(account_id)
    cap = min(max(limit, 1), 500)
    items: list[Any] = []
    token = ""
    while len(items) < cap:
        params: dict[str, Any] = {"AwsAccountId": account, "MaxResults": min(100, cap - len(items)), **kwargs}
        if token:
            params["NextToken"] = token
        response = getattr(client, operation)(**params)
        items.extend(response.get(result_key, []))
        token = response.get("NextToken", "")
        if not token:
            break
    return items[:cap]


def _validate(operation: str, request: dict[str, Any]) -> None:
    # Only the API model is needed, so dry-run plans work without credentials.
    shape = botocore.session.get_session().get_service_model("quicksight").operation_model(operation).input_shape
    validate_parameters(request, shape)


def _asset_response(response: dict[str, Any]) -> dict[str, Any]:
    return {key: response[key] for key in ("Arn", "DataSourceId", "DataSetId", "AnalysisId",
            "DashboardId", "CreationStatus", "IngestionId", "IngestionStatus", "VersionArn",
            "DashboardArn", "RequestId") if key in response}


def _glue_type_to_qs(glue_type: str) -> str:
    value = glue_type.lower().strip()
    if re.match(r"^(string|varchar|char|uuid)", value):
        return "STRING"
    if value in ("tinyint", "smallint", "int", "integer", "bigint"):
        return "INTEGER"
    if re.match(r"^(decimal|numeric|float|double|real)", value):
        return "DECIMAL"
    if re.match(r"^(timestamp|date)", value):
        return "DATETIME"
    if value in ("boolean", "bool"):
        return "BOOLEAN"
    raise ValueError(f"Tipo Glue no mapeado para QuickSight: {glue_type[:80]}. Proporciona columns explícitas.")


def _columns(database: str, table: str, columns: list[dict[str, str]] | None) -> list[dict[str, str]]:
    if columns is None:
        table_data = _client("glue").get_table(DatabaseName=database, Name=table)["Table"]
        source = table_data.get("StorageDescriptor", {}).get("Columns", []) + table_data.get("PartitionKeys", [])
        columns = [{"Name": col["Name"], "Type": _glue_type_to_qs(col["Type"])} for col in source]
    if not columns or len(columns) > 2048:
        raise ValueError("columns debe contener entre 1 y 2048 columnas.")
    result = []
    for column in columns:
        name = column.get("Name", "")
        column_type = column.get("Type", "").upper()
        if not name or len(name) > 128 or column_type not in _COLUMN_TYPES:
            raise ValueError("Cada columna requiere Name (máximo 128) y Type válido de QuickSight.")
        result.append({"Name": name, "Type": column_type})
    if len({c["Name"].lower() for c in result}) != len(result):
        raise ValueError("Los nombres de columnas deben ser únicos.")
    return result


def _dataset_request(account_id: str, dataset_id: str, name: str, data_source_arn: str,
                     database: str, table: str, columns: list[dict[str, str]] | None,
                     import_mode: str, catalog: str, permissions: list[dict[str, Any]] | None) -> dict[str, Any]:
    mode = import_mode.upper()
    if mode not in ("DIRECT_QUERY", "SPICE"):
        raise ValueError("import_mode debe ser DIRECT_QUERY o SPICE.")
    if not data_source_arn.startswith("arn:aws") or ":datasource/" not in data_source_arn:
        raise ValueError("data_source_arn debe ser un ARN de QuickSight datasource.")
    request: dict[str, Any] = {"AwsAccountId": _account(account_id), "DataSetId": _id(dataset_id, "dataset_id"),
                               "Name": name, "ImportMode": mode,
                               "PhysicalTableMap": {"main": {"RelationalTable": {
                                   "DataSourceArn": data_source_arn, "Catalog": catalog,
                                   "Schema": database, "Name": table,
                                   "InputColumns": _columns(database, table, columns)}}}}
    if permissions:
        request["Permissions"] = permissions
    return request


def _table_definition(data_set_arn: str, columns: list[str], sheet_name: str) -> dict[str, Any]:
    if not columns or len(columns) > 20:
        raise ValueError("Selecciona entre 1 y 20 columnas para la tabla inicial.")
    fields = [{"FieldId": f"column_{index + 1}",
               "Column": {"DataSetIdentifier": "main", "ColumnName": column}}
              for index, column in enumerate(columns)]
    return {"DataSetIdentifierDeclarations": [{"Identifier": "main", "DataSetArn": data_set_arn}],
            "Sheets": [{"SheetId": "overview", "Name": sheet_name,
                        "Visuals": [{"TableVisual": {"VisualId": "detail_table",
                            "ChartConfiguration": {"FieldWells": {"TableUnaggregatedFieldWells":
                                {"Values": fields}}}}}]}]}


def register_quicksight_tools(mcp: Any) -> None:
    """Register structured QuickSight tools on the existing AWS MCP."""

    @mcp.tool()
    def quicksight_identity() -> str:
        """Identidad STS usada por las tools QuickSight y fuente de credenciales."""
        def run() -> Any:
            identity = _client("sts").get_caller_identity()
            configured = os.environ.get("QUICKSIGHT_ENV_FILE", "").strip()
            source = Path(configured) if configured else Path(__file__).resolve().parent / "quicksight.env"
            return {"account": identity["Account"], "arn": identity["Arn"],
                    "region": os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
                    "source": "quicksight_env_file" if source.is_file() else "default_aws_chain"}
        return _call(run)

    @mcp.tool()
    def quicksight_status(aws_account_id: str = "") -> str:
        """Consulta estado y edición de la suscripción QuickSight."""
        def run() -> Any:
            account = _account(aws_account_id)
            info = _client("quicksight").describe_account_subscription(AwsAccountId=account)["AccountInfo"]
            return {"aws_account_id": account, "subscription_status": info.get("AccountSubscriptionStatus"),
                    "edition": info.get("Edition")}
        return _call(run)

    @mcp.tool()
    def quicksight_list_data_sources(limit: int = 100, aws_account_id: str = "") -> str:
        """Lista fuentes QuickSight con tipo, ARN y estado."""
        return _call(lambda: [{k: item.get(k) for k in ("DataSourceId", "Arn", "Name", "Type", "Status")}
                              for item in _list("list_data_sources", "DataSources", limit, aws_account_id)])

    @mcp.tool()
    def quicksight_get_data_source(data_source_id: str, aws_account_id: str = "") -> str:
        """Describe una fuente sin credenciales ni parámetros sensibles."""
        def run() -> Any:
            source = _client("quicksight").describe_data_source(
                AwsAccountId=_account(aws_account_id), DataSourceId=_id(data_source_id, "data_source_id"))["DataSource"]
            return {k: source.get(k) for k in ("DataSourceId", "Arn", "Name", "Type", "Status", "ErrorInfo")}
        return _call(run)

    @mcp.tool()
    def quicksight_list_data_sets(limit: int = 100, aws_account_id: str = "") -> str:
        """Lista datasets QuickSight y modo DIRECT_QUERY/SPICE."""
        return _call(lambda: [{k: item.get(k) for k in ("DataSetId", "Arn", "Name", "ImportMode", "CreatedTime", "LastUpdatedTime")}
                              for item in _list("list_data_sets", "DataSetSummaries", limit, aws_account_id)])

    @mcp.tool()
    def quicksight_get_data_set(data_set_id: str, aws_account_id: str = "") -> str:
        """Describe schema y tablas físicas del dataset sin devolver SQL almacenado."""
        def run() -> Any:
            data = _client("quicksight").describe_data_set(
                AwsAccountId=_account(aws_account_id), DataSetId=_id(data_set_id, "data_set_id"))["DataSet"]
            physical = {}
            for key, item in data.get("PhysicalTableMap", {}).items():
                kind = next(iter(item), "")
                table = item.get(kind, {})
                physical[key] = {"kind": kind, "name": table.get("Name"), "schema": table.get("Schema"),
                                 "data_source_arn": table.get("DataSourceArn"),
                                 "column_count": len(table.get("InputColumns", table.get("Columns", [])))}
            return {"data_set_id": data.get("DataSetId"), "arn": data.get("Arn"), "name": data.get("Name"),
                    "import_mode": data.get("ImportMode"), "status": data.get("Status"),
                    "output_columns": data.get("OutputColumns", []), "physical_tables": physical}
        return _call(run)

    @mcp.tool()
    def quicksight_build_table_definition(data_set_id: str, column_names: list[str] | None = None,
                                           sheet_name: str = "Overview", aws_account_id: str = "") -> str:
        """Genera Definition de una hoja con tabla a partir del schema de un dataset."""
        def run() -> Any:
            account = _account(aws_account_id)
            dataset = _client("quicksight").describe_data_set(
                AwsAccountId=account, DataSetId=_id(data_set_id, "data_set_id"))["DataSet"]
            available = {col["Name"] for col in dataset.get("OutputColumns", [])}
            selected = column_names if column_names is not None else [col["Name"] for col in dataset.get("OutputColumns", [])[:10]]
            if any(column not in available for column in selected):
                raise ValueError("Una o más columnas no están en OutputColumns del dataset.")
            definition = _table_definition(dataset["Arn"], selected, sheet_name)
            _validate("CreateAnalysis", {"AwsAccountId": account, "AnalysisId": "validation",
                                         "Name": "validation", "Definition": definition})
            return {"definition": definition, "columns": selected, "data_set_arn": dataset["Arn"]}
        return _call(run)

    @mcp.tool()
    def quicksight_list_analyses(limit: int = 100, aws_account_id: str = "") -> str:
        """Lista análisis existentes que pueden inspirar o alimentar dashboards."""
        return _call(lambda: [{k: item.get(k) for k in ("AnalysisId", "Arn", "Name", "Status", "LastUpdatedTime")}
                              for item in _list("list_analyses", "AnalysisSummaryList", limit, aws_account_id)])

    @mcp.tool()
    def quicksight_get_analysis(analysis_id: str, aws_account_id: str = "") -> str:
        """Consulta estado, hojas y datasets de un análisis."""
        return _call(lambda: _client("quicksight").describe_analysis(
            AwsAccountId=_account(aws_account_id), AnalysisId=_id(analysis_id, "analysis_id"))["Analysis"])

    @mcp.tool()
    def quicksight_get_analysis_definition(analysis_id: str, aws_account_id: str = "") -> str:
        """Lee la definición completa de un análisis para adaptar sus visuales."""
        return _call(lambda: _client("quicksight").describe_analysis_definition(
            AwsAccountId=_account(aws_account_id), AnalysisId=_id(analysis_id, "analysis_id"))["Definition"])

    @mcp.tool()
    def quicksight_list_dashboards(limit: int = 100, aws_account_id: str = "") -> str:
        """Lista dashboards y versión publicada."""
        return _call(lambda: [{k: item.get(k) for k in ("DashboardId", "Arn", "Name", "PublishedVersionNumber", "LastPublishedTime")}
                              for item in _list("list_dashboards", "DashboardSummaryList", limit, aws_account_id)])

    @mcp.tool()
    def quicksight_get_dashboard(dashboard_id: str, aws_account_id: str = "") -> str:
        """Consulta estado, versión y hojas de un dashboard."""
        return _call(lambda: _client("quicksight").describe_dashboard(
            AwsAccountId=_account(aws_account_id), DashboardId=_id(dashboard_id, "dashboard_id"))["Dashboard"])

    @mcp.tool()
    def quicksight_get_dashboard_definition(dashboard_id: str, version_number: int = 0,
                                            aws_account_id: str = "") -> str:
        """Lee la definición publicada o una versión concreta del dashboard."""
        def run() -> Any:
            request: dict[str, Any] = {"AwsAccountId": _account(aws_account_id),
                                       "DashboardId": _id(dashboard_id, "dashboard_id")}
            if version_number:
                request["VersionNumber"] = version_number
            return _client("quicksight").describe_dashboard_definition(**request)["Definition"]
        return _call(run)

    @mcp.tool()
    def quicksight_list_dashboard_versions(dashboard_id: str, limit: int = 100,
                                           aws_account_id: str = "") -> str:
        """Lista versiones creadas de un dashboard antes de publicar."""
        return _call(lambda: _list("list_dashboard_versions", "DashboardVersionSummaryList", limit,
                                   aws_account_id, DashboardId=_id(dashboard_id, "dashboard_id")))

    @mcp.tool()
    def quicksight_get_dashboard_permissions(dashboard_id: str, aws_account_id: str = "") -> str:
        """Consulta a qué usuarios o grupos se comparte un dashboard."""
        return _call(lambda: _client("quicksight").describe_dashboard_permissions(
            AwsAccountId=_account(aws_account_id), DashboardId=_id(dashboard_id, "dashboard_id")).get("Permissions", []))

    @mcp.tool()
    def quicksight_list_templates(limit: int = 100, aws_account_id: str = "") -> str:
        """Lista templates reutilizables de QuickSight."""
        return _call(lambda: _list("list_templates", "TemplateSummaryList", limit, aws_account_id))

    @mcp.tool()
    def quicksight_list_users(limit: int = 100, namespace: str = "default", aws_account_id: str = "") -> str:
        """Lista ARNs de usuarios QuickSight para asignar permisos sin adivinar principals."""
        return _call(lambda: [{k: item.get(k) for k in ("Arn", "UserName", "Role", "Active")}
                              for item in _list("list_users", "UserList", limit, aws_account_id,
                                                Namespace=_id(namespace, "namespace"))])

    @mcp.tool()
    def quicksight_list_ingestions(data_set_id: str, limit: int = 50, aws_account_id: str = "") -> str:
        """Lista refrescos SPICE y sus estados."""
        return _call(lambda: _list("list_ingestions", "Ingestions", limit, aws_account_id,
                                   DataSetId=_id(data_set_id, "data_set_id")))

    @mcp.tool()
    def quicksight_get_ingestion(data_set_id: str, ingestion_id: str, aws_account_id: str = "") -> str:
        """Consulta estado, filas y errores resumidos de un refresco SPICE."""
        return _call(lambda: _client("quicksight").describe_ingestion(
            AwsAccountId=_account(aws_account_id), DataSetId=_id(data_set_id, "data_set_id"),
            IngestionId=_id(ingestion_id, "ingestion_id"))["Ingestion"])

    @mcp.tool()
    def quicksight_create_athena_data_source(data_source_id: str, name: str, workgroup: str = "primary",
                                              role_arn: str = "", permissions: list[dict[str, Any]] | None = None,
                                              aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o crea una fuente Athena QuickSight; reusa una existente si sirve."""
        def run() -> Any:
            params: dict[str, Any] = {"WorkGroup": _id(workgroup, "workgroup")}
            if role_arn:
                params["RoleArn"] = role_arn
            request: dict[str, Any] = {"AwsAccountId": _account(aws_account_id),
                "DataSourceId": _id(data_source_id, "data_source_id"), "Name": name,
                "Type": "ATHENA", "DataSourceParameters": {"AthenaParameters": params}}
            if permissions:
                request["Permissions"] = permissions
            _validate("CreateDataSource", request)
            plan = _plan("QuickSight", "CreateDataSource", {"data_source_id": data_source_id,
                "name": name, "workgroup": workgroup, "role_arn": role_arn,
                "permission_principals": [p.get("Principal") for p in permissions or []]}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").create_data_source(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_create_athena_data_set(data_set_id: str, name: str, data_source_arn: str,
                                           database: str, table: str, columns: list[dict[str, str]] | None = None,
                                           import_mode: str = "DIRECT_QUERY", catalog: str = "AwsDataCatalog",
                                           permissions: list[dict[str, Any]] | None = None,
                                           aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o crea dataset Athena; columnas se infieren de Glue si se omiten."""
        def run() -> Any:
            request = _dataset_request(aws_account_id, data_set_id, name, data_source_arn,
                                       database, table, columns, import_mode, catalog, permissions)
            _validate("CreateDataSet", request)
            plan = _plan("QuickSight", "CreateDataSet", {"data_set_id": data_set_id, "name": name,
                "database": database, "table": table, "data_source_arn": data_source_arn,
                "import_mode": request["ImportMode"],
                "columns": request["PhysicalTableMap"]["main"]["RelationalTable"]["InputColumns"],
                "permission_principals": [p.get("Principal") for p in permissions or []]}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").create_data_set(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_update_athena_data_set(data_set_id: str, name: str, data_source_arn: str,
                                           database: str, table: str, columns: list[dict[str, str]] | None = None,
                                           import_mode: str = "DIRECT_QUERY", catalog: str = "AwsDataCatalog",
                                           aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o reemplaza la definición completa de un dataset existente."""
        def run() -> Any:
            request = _dataset_request(aws_account_id, data_set_id, name, data_source_arn,
                                       database, table, columns, import_mode, catalog, None)
            _validate("UpdateDataSet", request)
            plan = _plan("QuickSight", "UpdateDataSet", {"data_set_id": data_set_id, "name": name,
                "database": database, "table": table, "import_mode": request["ImportMode"],
                "warning": "UpdateDataSet reemplaza la definición: revisar dependencias y permisos existentes."}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").update_data_set(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_create_analysis(analysis_id: str, name: str, definition: dict[str, Any] | None = None,
                                    source_entity: dict[str, Any] | None = None,
                                    permissions: list[dict[str, Any]] | None = None,
                                    aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o crea análisis desde Definition o template SourceEntity."""
        def run() -> Any:
            if (definition is None) == (source_entity is None):
                raise ValueError("Proporciona exactamente uno de definition o source_entity.")
            request: dict[str, Any] = {"AwsAccountId": _account(aws_account_id),
                                       "AnalysisId": _id(analysis_id, "analysis_id"), "Name": name}
            request["Definition" if definition is not None else "SourceEntity"] = definition if definition is not None else source_entity
            if permissions:
                request["Permissions"] = permissions
            _validate("CreateAnalysis", request)
            plan = _plan("QuickSight", "CreateAnalysis", {"analysis_id": analysis_id, "name": name,
                "source_type": "Definition" if definition is not None else "SourceEntity",
                "sheet_count": len((definition or {}).get("Sheets", [])),
                "permission_principals": [p.get("Principal") for p in permissions or []]}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").create_analysis(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_update_analysis(analysis_id: str, name: str, definition: dict[str, Any],
                                    aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o actualiza la definición de un análisis existente."""
        def run() -> Any:
            request = {"AwsAccountId": _account(aws_account_id), "AnalysisId": _id(analysis_id, "analysis_id"),
                       "Name": name, "Definition": definition}
            _validate("UpdateAnalysis", request)
            plan = _plan("QuickSight", "UpdateAnalysis", {"analysis_id": analysis_id, "name": name,
                "sheet_count": len(definition.get("Sheets", []))}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").update_analysis(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_create_dashboard(dashboard_id: str, name: str, definition: dict[str, Any] | None = None,
                                     source_entity: dict[str, Any] | None = None,
                                     permissions: list[dict[str, Any]] | None = None,
                                     aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o crea la primera versión de dashboard desde Definition o template."""
        def run() -> Any:
            if (definition is None) == (source_entity is None):
                raise ValueError("Proporciona exactamente uno de definition o source_entity.")
            request: dict[str, Any] = {"AwsAccountId": _account(aws_account_id),
                                       "DashboardId": _id(dashboard_id, "dashboard_id"), "Name": name}
            request["Definition" if definition is not None else "SourceEntity"] = definition if definition is not None else source_entity
            if permissions:
                request["Permissions"] = permissions
            _validate("CreateDashboard", request)
            plan = _plan("QuickSight", "CreateDashboard", {"dashboard_id": dashboard_id, "name": name,
                "source_type": "Definition" if definition is not None else "SourceEntity",
                "sheet_count": len((definition or {}).get("Sheets", [])),
                "permission_principals": [p.get("Principal") for p in permissions or []]}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").create_dashboard(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_update_dashboard(dashboard_id: str, name: str, definition: dict[str, Any],
                                     aws_account_id: str = "", dry_run: bool = True) -> str:
        """Planifica o crea una nueva versión sin publicarla todavía."""
        def run() -> Any:
            request = {"AwsAccountId": _account(aws_account_id), "DashboardId": _id(dashboard_id, "dashboard_id"),
                       "Name": name, "Definition": definition}
            _validate("UpdateDashboard", request)
            plan = _plan("QuickSight", "UpdateDashboard", {"dashboard_id": dashboard_id, "name": name,
                "sheet_count": len(definition.get("Sheets", [])),
                "note": "La nueva versión no se publica hasta quicksight_publish_dashboard_version."}, dry_run)
            if plan:
                return plan
            return _asset_response(_client("quicksight").update_dashboard(**request))
        return _call(run)

    @mcp.tool()
    def quicksight_publish_dashboard_version(dashboard_id: str, version_number: int,
                                              aws_account_id: str = "", dry_run: bool = True,
                                              confirm_publish: bool = False) -> str:
        """Publica una versión validada de dashboard; requiere confirm_publish=true."""
        def run() -> Any:
            if version_number < 1:
                raise ValueError("version_number debe ser mayor que cero.")
            account = _account(aws_account_id)
            dashboard = _id(dashboard_id, "dashboard_id")
            plan = _plan("QuickSight", "UpdateDashboardPublishedVersion",
                         {"dashboard_id": dashboard, "version_number": version_number}, dry_run)
            if plan:
                return plan
            if not confirm_publish:
                raise ValueError("Para publicar usa confirm_publish=true después de revisar el plan.")
            client = _client("quicksight")
            version = client.describe_dashboard(AwsAccountId=account, DashboardId=dashboard,
                                                VersionNumber=version_number)["Dashboard"]["Version"]
            if version.get("Status") not in ("CREATION_SUCCESSFUL", "UPDATE_SUCCESSFUL"):
                raise ValueError("La versión elegida aún no tiene estado exitoso.")
            response = client.update_dashboard_published_version(AwsAccountId=account, DashboardId=dashboard,
                                                                   VersionNumber=version_number)
            return _asset_response(response)
        return _call(run)

    @mcp.tool()
    def quicksight_grant_dashboard_read(dashboard_id: str, principal_arn: str,
                                        aws_account_id: str = "", dry_run: bool = True,
                                        confirm_share: bool = False) -> str:
        """Comparte lectura con un principal QuickSight explícito; nunca acceso público."""
        def run() -> Any:
            account = _account(aws_account_id)
            dashboard = _id(dashboard_id, "dashboard_id")
            if not principal_arn.startswith("arn:aws:quicksight:") or f":{account}:" not in principal_arn:
                raise ValueError("principal_arn debe ser un usuario/grupo/namespace QuickSight de la misma cuenta.")
            if "publicAnonymousUser" in principal_arn:
                raise ValueError("Esta tool no concede acceso público anónimo.")
            plan = _plan("QuickSight", "UpdateDashboardPermissions",
                         {"dashboard_id": dashboard, "principal_arn": principal_arn,
                          "actions": _DASHBOARD_READ_ACTIONS}, dry_run)
            if plan:
                return plan
            if not confirm_share:
                raise ValueError("Para compartir usa confirm_share=true después de revisar el principal.")
            response = _client("quicksight").update_dashboard_permissions(AwsAccountId=account,
                DashboardId=dashboard, GrantPermissions=[{"Principal": principal_arn,
                                                           "Actions": _DASHBOARD_READ_ACTIONS}])
            return {"dashboard_arn": response.get("DashboardArn"), "principal_arn": principal_arn,
                    "actions": _DASHBOARD_READ_ACTIONS}
        return _call(run)

    @mcp.tool()
    def quicksight_start_ingestion(data_set_id: str, ingestion_id: str, aws_account_id: str = "",
                                   dry_run: bool = True) -> str:
        """Planifica o inicia refresco SPICE de un dataset; no aplica a DIRECT_QUERY."""
        def run() -> Any:
            account, dataset = _account(aws_account_id), _id(data_set_id, "data_set_id")
            ingestion = _id(ingestion_id, "ingestion_id")
            plan = _plan("QuickSight", "CreateIngestion", {"data_set_id": dataset,
                                                              "ingestion_id": ingestion}, dry_run)
            if plan:
                return plan
            client = _client("quicksight")
            data = client.describe_data_set(AwsAccountId=account, DataSetId=dataset)["DataSet"]
            if data.get("ImportMode") != "SPICE":
                raise ValueError("Solo los datasets SPICE admiten ingestion.")
            return _asset_response(client.create_ingestion(AwsAccountId=account, DataSetId=dataset,
                                                             IngestionId=ingestion))
        return _call(run)

    @mcp.tool()
    def quicksight_wait_asset(resource_type: str, resource_id: str, aws_account_id: str = "",
                              timeout_seconds: int = 120) -> str:
        """Espera creación/actualización de datasource, dataset, analysis o dashboard."""
        def run() -> Any:
            operations = {"data_source": ("describe_data_source", "DataSourceId", "DataSource"),
                          "data_set": ("describe_data_set", "DataSetId", "DataSet"),
                          "analysis": ("describe_analysis", "AnalysisId", "Analysis"),
                          "dashboard": ("describe_dashboard", "DashboardId", "Dashboard")}
            if resource_type not in operations:
                raise ValueError("resource_type debe ser data_source, data_set, analysis o dashboard.")
            operation, id_key, result_key = operations[resource_type]
            client = _client("quicksight")
            request = {"AwsAccountId": _account(aws_account_id), id_key: _id(resource_id, "resource_id")}
            deadline = time.monotonic() + min(max(timeout_seconds, 1), 600)
            while True:
                resource = getattr(client, operation)(**request)[result_key]
                version = resource.get("Version", {}) if resource_type == "dashboard" else {}
                status = version.get("Status") or resource.get("Status", "UNKNOWN")
                if status.endswith("SUCCESSFUL") or status.endswith("FAILED") or time.monotonic() >= deadline:
                    return {"resource_type": resource_type, "resource_id": resource_id, "status": status,
                            "version_number": version.get("VersionNumber")}
                time.sleep(2)
        return _call(run)
