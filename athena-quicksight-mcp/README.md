# athena-quicksight-mcp

Servidor MCP (Python, FastMCP) para ingeniería de datos en AWS: **Athena, S3, Glue, Lambda,
EventBridge, Step Functions, CloudWatch Logs y QuickSight**.

## Diseño de seguridad

- Las lecturas se ejecutan directamente.
- Toda operación que escribe (`s3_write_text`, `glue_put_table`, `lambda_invoke`, `quicksight_create_*`, ...)
  tiene `dry_run=true` por defecto y devuelve primero un **plan**. Para ejecutar hay que pasar `dry_run=false`.
- Reemplazos requieren `overwrite=true`; `DROP`/`DELETE` en Athena requieren `allow_destructive=true`;
  publicar dashboards requiere `confirm_publish=true`; nunca se concede acceso público en QuickSight.
- Subidas/descargas S3 limitadas a 100 MB y confinadas a la carpeta local `aws_artifacts/`.
- Credenciales nunca hardcodeadas: `.env` junto al script o la cadena por defecto de boto3.

## Tools

| Grupo | Tools |
|---|---|
| Athena (pyathena) | `query_athena`, `list_databases`, `list_tables`, `describe_table`, `show_create_table`, `sample_data`, `get_query_stats`, `test_connection`, `clear_cache` |
| Athena (API) | `athena_list_workgroups`, `athena_start_query`, `athena_wait_query`, `athena_get_results` |
| Identidad | `aws_identity`, `aws_capabilities` |
| S3 | `s3_list_buckets`, `s3_list_objects`, `s3_head_object`, `s3_read_text`, `s3_write_text`, `s3_upload_artifact`, `s3_download_artifact` |
| Glue | `glue_list_databases`, `glue_get_database`, `glue_list_tables`, `glue_get_table`, `glue_list_partitions`, `glue_list_crawlers`, `glue_create_database`, `glue_put_table`, `glue_add_partition`, `glue_list_jobs`, `glue_get_job`, `glue_list_job_runs`, `glue_start_job` |
| Lambda / EventBridge / Step Functions | `lambda_list_functions`, `lambda_get_function`, `lambda_invoke`, `eventbridge_list_rules`, `eventbridge_get_rule`, `stepfunctions_list_state_machines`, `stepfunctions_get_state_machine`, `stepfunctions_start_execution`, `stepfunctions_get_execution` |
| CloudWatch Logs | `cloudwatch_list_log_streams`, `cloudwatch_get_log_events` |
| QuickSight | `quicksight_identity`, `quicksight_status`, `quicksight_list_*`, `quicksight_get_*`, `quicksight_build_table_definition`, `quicksight_create_*`, `quicksight_update_*`, `quicksight_publish_dashboard_version`, `quicksight_grant_dashboard_read`, `quicksight_start_ingestion`, `quicksight_wait_asset` |

## Instalación

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env
```

Registro en el cliente MCP (usa rutas absolutas al intérprete):

```json
{
  "mcpServers": {
    "athena-quicksight": {
      "command": "C:/path/to/athena-quicksight-mcp/.venv/Scripts/python.exe",
      "args": ["C:/path/to/athena-quicksight-mcp/athena_mcp_server.py"]
    }
  }
}
```

QuickSight puede vivir en otra cuenta: pon sus credenciales en `quicksight.env`
(mismas claves `AWS_*`) o apunta `QUICKSIGHT_ENV_FILE` a otro archivo.

### Proxy corporativo con inspección TLS

`build_ca_bundle.py` (Windows) combina `certifi` con las CA del almacén de Windows en `corp-ca-bundle.pem`.
Apunta `AWS_CA_BUNDLE` a ese archivo. `AWS_DISABLE_SSL_VERIFY=1` existe solo como último recurso.

## Tests

```bash
python -m unittest test_aws_data_tools test_quicksight_tools
python smoke_aws_data.py   # contra una cuenta real, solo lecturas
```
