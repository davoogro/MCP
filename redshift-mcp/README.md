# redshift-mcp

Servidor MCP (Python, FastMCP) de **solo lectura** para Amazon Redshift vía la
[Redshift Data API](https://docs.aws.amazon.com/redshift/latest/mgmt/data-api.html) (boto3):
`execute_statement → describe_statement (polling con backoff) → get_statement_result (paginado)`.
No necesita driver JDBC/ODBC ni acceso de red directo al clúster.

## Tools

| Tool | Descripción |
|---|---|
| `run_query` | Ejecuta `SELECT`/`WITH` y devuelve filas en JSON (tope `REDSHIFT_MAX_ROWS`) |
| `list_tables` | Tablas y vistas de un esquema (`svv_tables`), filtro LIKE opcional |
| `describe_table` | Columnas, tipos, nullable, default (`svv_columns`) |
| `test_connection` | Clúster, base, usuario efectivo y versión |

## Seguridad

- Solo `SELECT`/`WITH`; se rechazan multi-statement y palabras de escritura
  (`INSERT/UPDATE/DELETE/DROP/CREATE/COPY/UNLOAD/SELECT ... INTO`, ...) antes de llegar a AWS.
- Usa además un usuario de base de datos de solo lectura: el filtro es un guardrail, no el control real.
- `REDSHIFT_AWS_ACCOUNT_ID` (opcional) rechaza credenciales que pertenezcan a otra cuenta.

## Instalación

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env
python smoke_redshift_mcp.py
```

```json
{ "mcpServers": { "redshift": {
  "command": "C:/path/to/redshift-mcp/.venv/Scripts/python.exe",
  "args": ["C:/path/to/redshift-mcp/redshift_mcp_server.py"] } } }
```
