# mssql-mcp

MCP (stdio) de **solo lectura** para los SQL Server 2022 de OPSDB:

| alias | host | bases |
|---|---|---|
| `sql01` | 192.0.2.61,49152 (SQLHOST01) | OPSDB, CM_TELCOVENDORA_4G_STAGING |
| `sql05` | 192.0.2.65,49152 (SQLHOST05) | ANALYTICS_4G, ANALYTICS_DW, KPIDB |

Credenciales: `MSSQL_USER` / `MSSQL_PASS` en `C:\DVB\mcp\.env`. Ver `.env.example` para el resto.

## Solo lectura (3 capas)

1. `safety.py`: tokenizador + bloqueo por posición de sentencia; una única `SELECT`/`WITH`.
2. `db.py`: pool de conexiones con rollback; cursores paginados; nunca `commit()`.
3. `ApplicationIntent=ReadOnly` en la conexión.

## Tools (25)

Conexión y exploración: list_connections, test_connection, list_databases, list_schemas, list_tables,
describe_table, sample_data, table_row_count, search_columns, search_objects, list_routines,
get_object_definition, get_dependencies.

Consultas: **query** (paginación con `next_cursor`, `page_size`, `max_cell_chars`), **fetch_more**,
query_all, **export_query** (CSV/Parquet local o `s3://`), explain_query (resumen + XML recortado).

Metadatos y frescura: **table_stats**, **list_indexes**, **table_freshness**, **compare_row_counts**.

Operación: server_activity, list_jobs, **query_history**.

`server_activity` y `list_jobs` devuelven `error_code: PermissionDenied` y el permiso que falta
(VIEW SERVER PERFORMANCE STATE / msdb SQLAgentReaderRole) si el login no tiene acceso.

## Límites por defecto

| Variable | Descripción | Default |
|---|---|---|
| `MSSQL_MAX_ROWS` | Filas por página en query | 5000 |
| `MSSQL_MAX_CELL_CHARS` | Recorte de celdas | 4000 |
| `MSSQL_EXPORT_DIR` | Carpeta de exportación | `C:\DVB\exports\mssql` |
| `MSSQL_EXPORT_MAX_ROWS` | Tope export_query | 50 000 000 |

## Uso

```bash
cd C:\DVB\mcp\mssql-mcp
set PYTHONPATH=src
C:\DVB\python\python.exe -m mssql_mcp --test        # prueba ambos servidores
C:\DVB\python\python.exe -m mssql_mcp --handshake   # lista las tools
C:\DVB\python\python.exe -m pytest
```
