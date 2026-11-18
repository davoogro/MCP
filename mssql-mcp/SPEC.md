# mssql-mcp — especificación

Servidor MCP (stdio) **de SOLO LECTURA** para dos SQL Server 2022 (on-prem) con autenticación SQL.
Debe permitir explorar y consultar todo (metadatos, datos, definiciones, planes, estado) pero
**jamás modificar nada**.

Servidores (verificados con pyodbc, login no es sysadmin, solo `CONNECT SQL` + `VIEW ANY DATABASE`):

| alias | host | hostname | bases accesibles |
|---|---|---|---|
| `sql01` | `192.0.2.61,49152` | SQLHOST01 | OPSDB, CM_TELCOVENDORA_4G_STAGING |
| `sql05` | `192.0.2.65,49152` | SQLHOST05 | ANALYTICS_4G, ANALYTICS_DW, KPIDB |

## Restricciones generales

- Python ≥ 3.10, paquete `mssql_mcp` en `C:\DVB\mcp\mssql-mcp\src\mssql_mcp`, mismo layout que
  `C:\DVB\mcp\fabric-mcp` (pyproject con hatchling, `__init__.py`, `__main__.py`, `config.py`, `db.py`,
  `safety.py`, `server.py`, tests en `tests/`, `README.md`, `.env.example`, `claude-mcp.example.json`).
  **Leer primero** `fabric-mcp/src/fabric_mcp/*.py` y reutilizar su estilo/patrones (es también T-SQL vía pyodbc),
  incluida la opción CLI `--handshake` y `--test`.
- SDK `mcp` 2.0: `from mcp.server import MCPServer` y `from mcp.types import ToolAnnotations`
  (NO existe `mcp.server.fastmcp`). **Todas** las tools con `ToolAnnotations(readOnlyHint=True, ...)`.
- Dependencias: `mcp>=2.0.0`, `pyodbc>=5.0`, `python-dotenv>=1.0`, `sqlparse>=0.5` (ya instaladas en
  `C:\DVB\python\python.exe`; NO instalar nada nuevo).
- Logging solo a stderr. Mensajes, docstrings e `instructions` del servidor en español.
- Cada tool devuelve un `dict` JSON-serializable: éxito `{"ok": true, ...}`, error
  `{"ok": false, "error": "...", "connection": ..., "sql": ...}`. Nunca propagar excepciones/stacktraces.
  Convertir `datetime`→ISO, `Decimal`→str, `bytes`→hex (recortado), `uuid`→str.
- Nunca exponer la contraseña en respuestas ni logs.
- Respuestas acotadas: `MSSQL_MAX_RESPONSE_CHARS` (default 80000) → recortar filas y marcar `"response_truncated": true`.
  Celdas de texto largas recortadas a `MSSQL_MAX_CELL_CHARS` (default 2000).

## Configuración (`config.py`)

Carga `.env` propio (`mssql-mcp/.env`, opcional) y luego el compartido `C:\DVB\mcp\.env` (`override=False`),
más `MSSQL_MCP_ENV_FILE` si existe (igual que `fabric_mcp.config`). Ruta del .env compartido calculada relativa
al archivo (`Path(__file__).resolve().parents[3] / ".env"`), no al cwd.

| Variable | Uso | Default |
|---|---|---|
| `MSSQL_USER` / `MSSQL_PASS` | credenciales SQL (ya existen en el `.env` compartido) | — |
| `MSSQL_SERVERS` | `alias=host,port;alias2=host,port` | `sql01=192.0.2.61,49152;sql05=192.0.2.65,49152` |
| `MSSQL_DRIVER` | driver ODBC | `ODBC Driver 18 for SQL Server` |
| `MSSQL_ENCRYPT` / `MSSQL_TRUST_CERT` | | `yes` / `yes` |
| `MSSQL_LOGIN_TIMEOUT` | s | `10` |
| `MSSQL_QUERY_TIMEOUT` | s (`cursor.timeout` / `conn.timeout`) | `120` |
| `MSSQL_MAX_ROWS` | máximo filas devueltas por `query` | `1000` |
| `MSSQL_DEFAULT_CONNECTION` | alias por defecto | `sql01` |

Connection string: `DRIVER={..};SERVER=host,port;UID=..;PWD=..;Encrypt=..;TrustServerCertificate=..;APP=mssql-mcp;ApplicationIntent=ReadOnly`
(+ `DATABASE=` cuando la tool recibe `database`). Una conexión nueva por llamada (o pool pequeño por alias+database),
cerrada siempre.

## Garantías de solo lectura (`safety.py`) — CRÍTICO

Defensa en profundidad, las tres capas obligatorias:

1. **Validación del SQL** del usuario (`query`, `explain_query`, `query_all`) con `sqlparse` + regex sobre el
   texto con comentarios y literales eliminados:
   - Exactamente **una** sentencia; debe empezar por `SELECT` o `WITH` (CTE que termina en SELECT).
   - Rechazar palabras clave (como token, case-insensitive): INSERT, UPDATE, DELETE, MERGE, TRUNCATE, DROP, CREATE,
     ALTER, EXEC, EXECUTE, SP_EXECUTESQL, GRANT, REVOKE, DENY, BACKUP, RESTORE, DBCC, KILL, SHUTDOWN, RECONFIGURE,
     BULK, OPENROWSET, OPENQUERY, OPENDATASOURCE, OPENXML, WAITFOR, USE, SET, DECLARE, BEGIN, COMMIT, ROLLBACK,
     SAVE, TRANSACTION, INTO (bloquea `SELECT ... INTO`), `NEXT VALUE FOR`, `xp_`, `sp_` (prefijos de procedimientos),
     `;` intermedio, `GO`.
   - Rechazar hints `WITH (UPDLOCK|XLOCK|HOLDLOCK|TABLOCKX|PAGLOCK)` y `FOR UPDATE`.
   - Permitir: `FOR XML`, `FOR JSON`, `OPTION (...)`, `NOLOCK`, funciones de sistema / DMVs.
   - Error claro en español indicando la palabra rechazada.
2. **Transacción con rollback**: `autocommit=False`; antes del SQL ejecutar
   `SET TRANSACTION ISOLATION LEVEL READ COMMITTED; SET LOCK_TIMEOUT 30000;` (internos, no del usuario) y al terminar
   **siempre** `conn.rollback()` en `finally`. Nunca llamar `commit()` en todo el paquete.
3. `ApplicationIntent=ReadOnly` en la conexión.

Las tools de metadatos usan SQL fijo y parametrizado (`?`); identificadores validados (nombre de base contra
`sys.databases`, esquema/tabla con regex `^[\w@#$ .-]{1,128}$` y siempre entre `QUOTENAME`/corchetes escapando `]`).

## Tools (todas aceptan `connection: str | None` = alias; las que aplican, `database: str | None`)

1. `list_connections()` — alias, host, puerto, usuario (enmascarado parcial), default.
2. `test_connection(connection=None)` — sin `connection` prueba TODOS en paralelo: `@@SERVERNAME`, versión,
   edición, `SERVERPROPERTY('ProductVersion')`, bases accesibles, latencia ms, permisos de servidor del login.
3. `list_databases(connection=None)` — `sys.databases` con `HAS_DBACCESS(name)=1`: nombre, estado, recovery,
   compat level, collation, tamaño MB (si hay permiso; tolerante a error).
4. `list_schemas(connection, database)` — con número de tablas/vistas por esquema.
5. `list_tables(connection, database, schema=None, pattern=None, include_views=True, limit=500)` —
   tipo (TABLE/VIEW), filas aprox. (`sys.partitions`), tamaño reservado MB si hay permiso, create/modify date.
6. `describe_table(table, schema='dbo', connection, database)` — columnas (tipo completo, nullable, default,
   identity, computed, collation), PK, FKs (entrantes y salientes), índices (columnas clave/incluidas, unique,
   filtro), check constraints, triggers, descripción `MS_Description` si existe.
7. `sample_data(table, schema='dbo', rows=10, columns=None, where=None, order_by=None, connection, database)` —
   `SELECT TOP (n)`; `where`/`order_by` pasan por la validación de safety (como fragmento).
8. `table_row_count(table, schema, exact=False, connection, database)` — aprox. por `sys.partitions` o `COUNT_BIG(*)`.
9. `search_columns(pattern, connection, database=None, schema=None, limit=200)` — LIKE sobre nombres de columna;
   sin `database` busca en todas las bases accesibles del servidor.
10. `search_objects(pattern, connection, database=None, object_types=None, search_definitions=False, limit=200)` —
    busca tablas/vistas/procs/funciones/triggers por nombre, y opcionalmente dentro del texto
    (`sys.sql_modules.definition LIKE`), en una o todas las bases.
11. `list_routines(connection, database, schema=None, routine_type=None)` — procedimientos y funciones con parámetros.
12. `get_object_definition(name, schema='dbo', connection, database)` — `OBJECT_DEFINITION` de vista/proc/función/
    trigger (texto completo hasta límite) + parámetros.
13. `get_dependencies(name, schema='dbo', connection, database)` — `sys.sql_expression_dependencies`: qué usa y quién lo usa.
14. `query(sql, connection=None, database=None, max_rows=None)` — SELECT validado, devuelve `columns` (nombre+tipo),
    `rows` (listas), `row_count`, `truncated`, `elapsed_ms`. Usa `fetchmany(max_rows+1)` para detectar truncado.
15. `query_all(sql, database=None, max_rows=200)` — ejecuta el mismo SELECT en todos los alias en paralelo
    (`ThreadPoolExecutor`), resultados etiquetados por alias; un servidor caído no aborta al resto.
16. `explain_query(sql, connection, database)` — plan estimado: `SET SHOWPLAN_XML ON` (sentencia interna, en batch
    propio), ejecutar el SQL validado, recoger XML, `SET SHOWPLAN_XML OFF`. Devolver resumen (operadores principales,
    coste estimado, filas estimadas, missing indexes) + XML recortado. Si falta permiso SHOWPLAN, error claro.
17. `server_activity(connection=None)` — sesiones/peticiones activas (`sys.dm_exec_requests` + `sys.dm_exec_sessions`
    + texto SQL), bloqueos. Requiere VIEW SERVER STATE: si no hay permiso devolver `ok:false` con explicación, sin romper.
18. `list_jobs(connection=None)` — SQL Agent (`msdb.dbo.sysjobs`, último resultado de `sysjobhistory`, próximo run);
    tolerante a falta de permisos.

`instructions` del servidor: describir alias y bases de cada servidor, dialecto T-SQL (`TOP`, no `LIMIT`;
`OFFSET ... FETCH`), flujo recomendado (`test_connection` → `list_databases` → `list_tables` → `describe_table` →
`sample_data`/`query`), que es solo lectura y qué se rechaza, y preferir agregaciones.

## Tests (`tests/`)

- `test_safety.py`: casos aceptados (SELECT, WITH CTE, FOR JSON, comentarios, literales con 'delete' dentro,
  `[update]` como identificador entre corchetes) y rechazados (cada palabra prohibida, múltiples sentencias,
  `SELECT INTO`, `EXEC`, `WAITFOR`, `xp_cmdshell`, `;DROP`, comentarios ocultando sentencias).
- `test_config.py`: parseo de `MSSQL_SERVERS`.
- Tests sin red (no conectar a los servidores en pytest).

## Verificación final (hazla tú)

1. `C:\DVB\python\python.exe -m pytest` en `mssql-mcp` → todo verde. `ruff check` limpio si está disponible.
2. `PYTHONPATH=src C:\DVB\python\python.exe -m mssql_mcp --handshake` lista las 18 tools.
3. `--test` contra ambos servidores reales (deben responder).
4. No registrar el servidor en Claude ni tocar archivos fuera de `C:\DVB\mcp\mssql-mcp`.
