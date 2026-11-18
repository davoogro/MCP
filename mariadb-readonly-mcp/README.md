# mariadb-readonly-mcp

Servidor MCP en TypeScript para consultar **múltiples instancias MariaDB/MySQL** en modo **solo lectura**.

## Conexiones

| Alias | Host | Puerto | Database |
| --- | --- | --- | --- |
| `dbsrv01-3306` | `dbsrv01` | 3306 | `information_schema` |
| `dbsrv01-3309` | `dbsrv01` | 3309 | `planning` |

Credenciales vía `.env` (`MARIADB_USER` / `MARIADB_PASSWORD`). No van en el YAML.

## Uso en Cursor

Publicado en `%USERPROFILE%\.cursor\mcp.json` como `mariadb-readonly` para todas las conversaciones.

```bash
npm install
npm run build
npm test
npm run validate
```

## Uso en Claude Code

Claude Code no admite `cwd` ni `envFile`: config (`config/databases.yaml`) y `.env` se resuelven desde la carpeta del paquete, así que basta con la ruta absoluta. `MARIADB_MCP_CONFIG` es opcional.

```bash
claude mcp add mariadb-readonly --scope user -- "C:/Program Files/nodejs/node.exe" C:/DVB/mcp/mariadb-readonly-mcp/dist/src/index.js
npm run smoke -- dbsrv01-3309   # handshake stdio desde otra carpeta, sin variables de entorno
```

## Herramientas MCP

| Herramienta | Descripción |
| --- | --- |
| `list_connections` | Aliases configurados (sin credenciales) |
| `test_connection` | Conectividad, versión, latencia y si la sesión es READ ONLY |
| `list_databases` | Bases (schemas) del servidor; `includeSystem` opcional |
| `list_schemas` | Igual que antes: todos los schemas con charset |
| `list_tables` | Tablas/vistas con engine, filas aproximadas y comentario (`schema`, `pattern`) |
| `describe_table` | Columnas (key, extra, comentario), PK, FKs e índices |
| `search_columns` | Busca columnas por nombre (LIKE) en todas las bases |
| `sample_data` | Primeras N filas con identificadores entrecomillados |
| `execute_query` | SQL de solo lectura; `database`, `maxRows` (cap 10000), `timeoutMs` (cap 300000), `maxCellChars`; streaming + `truncated` |
| `explain_query` | Plan `EXPLAIN FORMAT=JSON` resumido; `analyze=true` → `EXPLAIN ANALYZE` (solo SELECT/WITH) |
| `table_stats` | Tamaños y filas estimadas por tabla (`information_schema.TABLES`, `top`) |
| `list_indexes` | Índices de una tabla (columnas, unicidad, cardinalidad) |
| `get_definition` | `SHOW CREATE` para vista, procedimiento, función, trigger o evento |
| `list_routines` | Procedimientos y funciones almacenados |
| `get_activity` | `SHOW FULL PROCESSLIST` resumido |
| `server_info` | Versión, uptime, variables clave, tamaño por schema |
| `query_all` | Misma SQL en paralelo en varios aliases |
| `search_tables` | Busca tablas por nombre en uno o todos los aliases |
| `export_query` | CSV en streaming a `C:\DVB\exports\mariadb\` (tope 50M filas) |

## Seguridad

- Validador con lexer propio: una sola sentencia que empiece por SELECT/WITH/SHOW/DESCRIBE/DESC/EXPLAIN/VALUES.
- Rechaza `SELECT ... INTO OUTFILE/DUMPFILE/@var`, `FOR UPDATE`, `LOCK IN SHARE MODE`, `LOAD_FILE`, `SLEEP`, `BENCHMARK`, `GET_LOCK`, `NEXTVAL`/`NEXT VALUE FOR`, comentarios ejecutables `/*! */`, y `--` sin espacio no se trata como comentario (igual que MariaDB). Si un literal usa `\'`, se valida también como si el servidor tuviera `NO_BACKSLASH_ESCAPES`.
- `REPLACE()`, `INSERT()`, `CHARACTER SET`, `USE INDEX` y palabras dentro de literales ya no dan falsos positivos.
- Sesión: `SET SESSION TRANSACTION READ ONLY`, `max_statement_time` / `MAX_EXECUTION_TIME`, `START TRANSACTION READ ONLY` + `ROLLBACK`, límite de filas por streaming en SELECT (no solo `sql_select_limit`), `multipleStatements: false`.
- Fechas devueltas tal como están almacenadas (`dateStrings`) y BIGINT/DECIMAL como texto para no perder precisión.
