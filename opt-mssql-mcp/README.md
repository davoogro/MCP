# opt-mssql-mcp

MCP (stdio) de **lectura y escritura** para el SQL Server 2022 de **winsrv01**
(SQLHOST06, 192.0.2.38, instancia MSSQLSERVER en el puerto 49152).

winsrv01 no es alcanzable directo desde el PC: el MCP abre solo un túnel SSH
`127.0.0.1:15439 -> winsrv01 127.0.0.1:49152` (ProxyJump `jumphost`, llave de `~/.ssh/config`)
y lo deja vivo para otras instancias; si se cae, la siguiente conexión lo reabre.

Credenciales: `OPT_USER` / `OPT_PASS` en `C:\DVB\mcp\.env`. Resto en `.env.example`.

## Lectura

Las mismas tools que mssql-mcp (list_databases, list_tables, describe_table, sample_data,
query con cursor/fetch_more, export_query, explain_query, search_*, table_stats, ...).
`query` sigue aceptando solo un SELECT/WITH.

## Escritura

| tool | qué hace |
|---|---|
| `execute_sql` | Cualquier T-SQL (DML, DDL, EXEC, scripts con `GO`). Una transacción por script con `XACT_ABORT ON`: si algo falla se deshace todo. `dry_run=true` ejecuta y deshace. |
| `insert_rows` | INSERT parametrizado de una lista de objetos JSON (fast_executemany, todo o nada). |
| `classify_sql` | Riesgo de un script sin ejecutarlo: read / write / dangerous. |
| `write_audit_log` | Bitácora de escrituras (`logs/write_audit.jsonl`). |

Lo **destructivo** (DROP, TRUNCATE, DELETE/UPDATE sin WHERE, ALTER TABLE ... DROP, ALTER DATABASE/LOGIN,
GRANT/REVOKE/DENY, KILL, BACKUP/RESTORE, DBCC, xp_*, sp_configure, sp_rename, MERGE ... NOT MATCHED BY
SOURCE THEN DELETE) devuelve `error_code=ConfirmationRequired` hasta que se repita con `confirm=true`.
El cuerpo de un `CREATE/ALTER PROCEDURE|FUNCTION|TRIGGER|VIEW` no cuenta como destructivo (no se ejecuta).
`CREATE/ALTER/DROP DATABASE`, `BACKUP`, `RESTORE` y `KILL` corren en autocommit (SQL Server no los
admite en transacción) y no tienen dry-run.

## Uso

```bash
cd C:\DVB\mcp\opt-mssql-mcp
set PYTHONPATH=src
C:\DVB\python\python.exe -m opt_mssql_mcp --test
C:\DVB\python\python.exe -m opt_mssql_mcp --handshake
C:\DVB\python\python.exe -m pytest
```
