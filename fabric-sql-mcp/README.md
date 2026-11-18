# fabric-sql-mcp

MCP server para **Microsoft Fabric SQL** (Data Warehouse y SQL analytics endpoints de lakehouses)
vía `pyodbc` + Entra ID. Reemplaza y unifica `../fabric_mcp_server.py` y `../fabric_mcp_server2.py`
(que siguen funcionando para Cursor) en un único servidor **multi-conexión**.

## Configuración

Reutiliza el `.env` existente en `C:\DVB\mcp\.env` (o `fabric-sql-mcp\.env` si lo creas, que tiene prioridad).
Cada prefijo `FABRIC<n>_` es una conexión:

| Prefijo      | Alias por defecto | Alias custom     |
|--------------|-------------------|------------------|
| `FABRIC_`    | `default`         | `FABRIC_NAME`    |
| `FABRIC2_`   | `fabric2`         | `FABRIC2_NAME`   |
| `FABRIC3_`…  | `fabric3`…        | `FABRIC3_NAME`…  |

Variables por conexión: `SERVER`, `DATABASE`, `USER`, `PASSWORD`, `AUTH`
(`ActiveDirectoryPassword` por defecto, `ActiveDirectoryServicePrincipal`, `ActiveDirectoryInteractive`,
`ActiveDirectoryIntegrated`, `ActiveDirectoryMsi`), `DRIVER`, `READONLY` (default `true`),
`QUERY_TIMEOUT`, `CACHE_TTL`, `POOL`. Ver [.env.example](.env.example).

## Tools

Todas aceptan `connection` (alias) y `database` (warehouse/lakehouse del mismo endpoint) opcionales.

| Tool | Descripción |
|------|-------------|
| `list_connections` | Conexiones configuradas (sin secretos) |
| `test_connection` | Usuario autenticado, base, versión |
| `list_databases` | Warehouses / lakehouses del endpoint |
| `list_schemas`, `list_tables` | Exploración (filtro `schema`, `pattern`) |
| `describe_table`, `sample_data` | Columnas y muestra `TOP n` |
| `table_row_count` | Filas aproximadas (default) o `COUNT_BIG(*)` con `exact=true` |
| `search_columns` | Buscar columnas por nombre |
| `get_view_definition` | T-SQL de vistas / funciones / SPs (legacy) |
| `get_object_definition` | Definición de vista, proc, función |
| `list_procedures` | Procedimientos almacenados |
| `column_stats` | min/max/nulos/distintos aprox por columna (máx 30) |
| `query` | T-SQL con `next_cursor` / `fetch_more` (readonly: un solo `SELECT`/`WITH`) |
| `fetch_more` | Siguiente página de un `next_cursor` |
| `export_query` | CSV/Parquet local (`C:\DVB\exports\fabric`) o `s3://` |
| `query_history` | Últimas consultas de la sesión MCP |
| `explain_query` | Plan estimado `SHOWPLAN_XML` |
| `get_query_stats` | Tiempos de una ejecución previa |
| `execute` | Solo si alguna conexión tiene `READONLY=false` |

## Registro en Claude (todas las conversaciones)

```bash
claude mcp add --scope user fabric-sql -e PYTHONPATH=C:\DVB\mcp\fabric-sql-mcp\src -e PYTHONIOENCODING=utf-8 -- C:\DVB\python\python.exe -m fabric_sql_mcp
```

Ver [claude-mcp.example.json](claude-mcp.example.json) para Claude Desktop / Cursor.

## CLI

```bash
set PYTHONPATH=C:\DVB\mcp\fabric-sql-mcp\src
python -m fabric_sql_mcp --list        # conexiones
python -m fabric_sql_mcp --test        # prueba todas (o: --test default fabric2)
python -m fabric_sql_mcp --handshake   # lista tools vía cliente MCP
python -m pytest -q                # tests
```
