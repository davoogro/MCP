# postgres-readonly-mcp

Servidor [Model Context Protocol (MCP)](https://modelcontextprotocol.io) en TypeScript para administrar y consultar **múltiples servidores PostgreSQL** desde una sola instancia, en **modo de solo lectura**.

## Propósito

Este MCP permite a Cursor (u otros hosts MCP) descubrir conexiones preconfiguradas, inspeccionar esquemas/tablas y ejecutar consultas de lectura contra bases PostgreSQL corporativas, sin exponer credenciales ni aceptar cadenas de conexión arbitrarias.

## Requisitos

- Node.js 20+
- Acceso de red a los hosts PostgreSQL definidos en YAML
- Credenciales provistas por variables de entorno (no en el repositorio)

## Instalación

```bash
cd postgres-readonly-mcp
npm install
cp config/databases.example.yaml config/databases.yaml
cp .env.example .env
```

Edita `config/databases.yaml` y define tus aliases. Coloca credenciales reales solo en `.env` o en el gestor de secretos corporativo.

## Compilación

```bash
npm run build
npm run lint
npm test
```

## Variables de entorno

| Variable | Descripción |
| --- | --- |
| `PG_MCP_CONFIG` | Opcional. Ruta al YAML de conexiones. Por defecto `config/databases.yaml` del paquete; las rutas relativas se resuelven contra la carpeta del paquete (y luego contra el cwd) |
| `TEST_POSTGRES_USER` | Usuario para el alias `test-postgres` |
| `TEST_POSTGRES_PASSWORD` | Contraseña para el alias `test-postgres` |

Ejemplo de `.env` local (no versionado):

```bash
PG_MCP_CONFIG=./config/databases.yaml
TEST_POSTGRES_USER=your_readonly_user
TEST_POSTGRES_PASSWORD=your_password
```

### Carga opcional de `.env`

El servidor carga el `.env` de la carpeta del paquete (resuelto desde la ubicación de `dist/src/index.js`, no desde el cwd); solo si no existe, prueba `.env` del directorio de trabajo. Las variables ya definidas por el cliente MCP tienen prioridad. En producción o Cursor corporativo, define las variables en el proceso padre o en el gestor de secretos aprobado por la empresa.

## Estructura del YAML

```yaml
defaults:
  port: 5432
  sslmode: prefer
  statementTimeoutMs: 60000
  connectionTimeoutMs: 10000
  maxRows: 2000
  readonly: true

connections:
  test-postgres:
    description: Servidor PostgreSQL usado para validar el MCP
    host: appsrvb01
    port: 55432
    database: postgres
    userEnv: TEST_POSTGRES_USER
    passwordEnv: TEST_POSTGRES_PASSWORD
    sslmode: prefer
```

### Agregar otro servidor

```yaml
connections:
  another-server:
    description: Otra base corporativa
    host: postgres-example.internal
    port: 5432
    database: application_db
    userEnv: ANOTHER_POSTGRES_USER
    passwordEnv: ANOTHER_POSTGRES_PASSWORD
    sslmode: require
```

Cada conexión se identifica por **alias**. El MCP solo acepta aliases declarados en YAML.

### `readonly` en el YAML

La clave `readonly: false` en `defaults` o en una conexión **no desactiva** el modo solo lectura del MCP: todas las herramientas validan SQL de lectura, usan `BEGIN READ ONLY` y hacen rollback. Si el YAML declara `readonly: false`, el servidor emite un **warning en stderr** al arrancar para dejar claro que esa bandera es informativa (p. ej. permisos del rol) y no autoriza escrituras desde el MCP.

## Integración con Cursor

Copia `cursor-mcp.example.json` a tu configuración MCP de Cursor y ajusta rutas absolutas:

```json
{
  "mcpServers": {
    "postgres-readonly": {
      "command": "node",
      "args": ["C:\\DVB\\mcp\\postgres-readonly-mcp\\dist\\src\\index.js"],
      "env": {
        "PG_MCP_CONFIG": "C:\\DVB\\mcp\\postgres-readonly-mcp\\config\\databases.yaml"
      }
    }
  }
}
```

Las variables `userEnv` / `passwordEnv` deben estar disponibles para el proceso que inicia Cursor (o en el `.env` del paquete).

## Integración con Claude Code

Claude Code no admite `cwd` ni `envFile`: basta con la ruta absoluta, porque config y `.env` se resuelven desde el paquete.

```bash
claude mcp add postgres-readonly --scope user -- "C:/Program Files/nodejs/node.exe" C:/DVB/mcp/postgres-readonly-mcp/dist/src/index.js
```

Opcionalmente `-e PG_MCP_CONFIG=<ruta>` para otro YAML. Comprobación de extremo a extremo (handshake stdio lanzado desde otra carpeta, sin variables de entorno):

```bash
npm run build
npm run smoke -- dbsrv01
```

## Herramientas MCP

| Herramienta | Descripción |
| --- | --- |
| `list_connections` | Lista aliases configurados (sin credenciales) |
| `test_connection` | Prueba conectividad y latencia |
| `list_databases` | Bases del servidor y qué aliases apuntan a cada una (`includeSize` opcional) |
| `list_schemas` | Lista esquemas accesibles |
| `list_tables` | Tablas, tablas particionadas, vistas, vistas materializadas y foráneas, con filas estimadas (`pattern`, `includePartitions`) |
| `describe_table` | Columnas (tipo completo, comentarios), PK, FKs e índices |
| `search_columns` | Busca columnas por nombre (ILIKE) en todos los esquemas |
| `sample_data` | Primeras N filas de una tabla con identificadores entrecomillados |
| `execute_query` | SQL de solo lectura (`timeoutMs`, `maxRows`, `maxCellChars`; `cursor`/`nextOffset` si `truncated`) |
| `fetch_more` | Siguiente página de un `cursor` de `execute_query` (TTL 10 min, máx. 5 abiertos) |
| `explain_query` | Plan `EXPLAIN (FORMAT JSON)` resumido; `analyze=true` con rollback |
| `table_stats` | Tamaños, `n_live_tup`/`n_dead_tup`, vacuum/analyze, `seq_scan` vs `idx_scan` |
| `list_indexes` | Índices con `idx_scan` y tamaño |
| `get_definition` | DDL de vista, matview, función o trigger |
| `list_functions` | Funciones (`pg_proc`) con búsqueda opcional |
| `get_activity` | Sesiones en `pg_stat_activity` (consulta recortada) |
| `get_locks` | Esperas de bloqueo bloqueante/bloqueado |
| `server_info` | Versión, uptime, tamaños por base, extensiones, parámetros clave |
| `query_all` | Misma SQL en varios aliases en paralelo |
| `search_tables` | Busca tablas por nombre en uno o todos los aliases |
| `export_query` | Export CSV streaming a `C:\DVB\exports\postgres\` (tope 50M filas; solo CSV) |

Todas las herramientas declaran `readOnlyHint: true`, y el servidor envía `instructions` con los aliases y el flujo recomendado.

### Ejemplos

```json
{ "connection": "test-postgres" }
```

```json
{
  "connection": "test-postgres",
  "sql": "SELECT current_database(), current_user, now()"
}
```

## Modelo de seguridad

Capas aplicadas en código:

1. Aliases permitidos solo desde YAML
2. Validación Zod de entradas
3. Validación de una sola sentencia SQL
4. Rechazo de operaciones de escritura/administración
5. Sesión PostgreSQL `default_transaction_read_only = on`
6. Transacción explícita `BEGIN READ ONLY` + `ROLLBACK`
7. Timeouts de conexión y consulta
8. Límite de filas configurable (sin alterar semántica con `LIMIT` inyectado)
9. Sanitización de errores y secretos
10. Validador con lexer propio (comentarios anidados, `E''`, `$`): rechaza `SELECT ... INTO`, `FOR UPDATE/SHARE`, CTEs con DML, `EXPLAIN ANALYZE` de escrituras y funciones con efectos (`pg_sleep`, `set_config`, `dblink*`, `lo_*`, `pg_read_file`, `nextval`, advisory locks, ...), sin falsos positivos por `CASE ... END`, `FETCH FIRST` o palabras dentro de literales
11. Valores largos de celdas truncados (`maxCellChars`); fechas/timestamps devueltos tal cual los imprime PostgreSQL
12. `sslmode: prefer` reintenta sin TLS si el servidor no lo soporta (como libpq)

## Validación manual

```bash
npm run validate
```

Desde una máquina con acceso a la red corporativa/VPN:

```bash
export PG_MCP_CONFIG=./config/databases.yaml
export TEST_POSTGRES_USER=your_readonly_user
export TEST_POSTGRES_PASSWORD=your_password
npm run validate
```

Para ejecutar la prueba de integración automatizada:

```bash
export RUN_PG_INTEGRATION=1
npm test
```

## Solución de problemas

| Síntoma | Causa probable |
| --- | --- |
| DNS resolution failed | Hostname no resuelve fuera de la red corporativa |
| Connection refused | Puerto cerrado o firewall |
| Connection timed out | VPN requerida o host inaccesible |
| PostgreSQL rejected the credentials | Usuario/contraseña incorrectos |
| SSL/TLS negotiation failed | Ajustar `sslmode` (`prefer`, `require`, etc.) |

## Limitaciones

- `execute_query` devuelve como máximo `maxRows` filas por página; use `fetch_more` o filtros/`LIMIT` en SQL para el resto.
- `export_query` solo escribe CSV (Parquet requeriría dependencias adicionales).
- No hay herramientas para modificar YAML ni credenciales en caliente.
- El modo solo lectura depende también de permisos del rol PostgreSQL.

## Licencia

MIT
