# OSS FM Oracle MCP (solo lectura)

Servidor [MCP](https://modelcontextprotocol.io) **independiente** para consultar las **6 instancias regionales** Oracle de alarmas OSS FM (TelcoVendorB). Solo lectura: defensa en profundidad (whitelist SQL + rechazo DML/DDL/PL/SQL + `SET TRANSACTION READ ONLY` + tope de filas + timeout).

## Collectors

| RC   | Host         | Puerto |
|------|--------------|--------|
| rc1  | 192.0.2.204 | 8001 |
| rc2  | 192.0.2.204 | 8002 |
| rc7  | 192.0.2.204 | 8006 |
| rc8  | 192.0.2.204 | 8008 |
| rc9  | 192.0.2.204 | 8009 |
| rc10 | 192.0.2.204 | 8004 |

- Service name: `OSS` (configurable)
- Tabla principal: `ALARMS.ALARM_LOG`
- Alarma activa: `CANCEL_TIME IS NULL`
- Severidad: `1=critical`, `2=major`, `3=minor`, `4=warning`
- Hora de red: **UTC-5 fijo** (sin DST). Salida `YYYY-MM-DD HH:MM:SS` sin conversión
- **rc8**: el proxy corta ~11 s; ventanas amplias sobre `ALARM_TIME` se trocean en tramos de ~3 días (`ORACLE_FM_RC8_CHUNK_DAYS`)

> Un RC equivocado puede devolver **0 filas sin error**. Usá `query_all` o `rc="all"` cuando no sepas en qué collector vive el elemento.

## Instalación

```powershell
cd C:\DVB\mcp\oss-fm-oracle-mcp
C:\DVB\python\python.exe -m pip install -e ".[dev]"
copy .env.example .env
# Editar .env con ORACLE_FM_USER / ORACLE_FM_PASSWORD
```

## Variables de entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `ORACLE_FM_USER` | Usuario Oracle | *(requerido)* |
| `ORACLE_FM_PASSWORD` | Password | *(requerido)* |
| `ORACLE_FM_HOST` | Host del proxy | `192.0.2.204` |
| `ORACLE_FM_SERVICE` | Service name | `OSS` |
| `ORACLE_FM_MAX_ROWS` | Techo duro de filas por RC | `5000` |
| `ORACLE_FM_DEFAULT_ROWS` | Filas por RC si la llamada no pasa `max_rows` | `500` |
| `ORACLE_FM_EXPORT_DIR` | Directorio base para `export_query` | `C:\DVB\exports\oss` |
| `ORACLE_FM_MAX_RESPONSE_CHARS` | Tope del JSON devuelto (recorta filas, `response_truncated`) | `80000` |
| `ORACLE_FM_MAX_CELL_CHARS` | Tope por celda de texto/CLOB | `4000` |
| `ORACLE_FM_ENV_FILE` | Ruta alternativa del `.env` | `<proyecto>\.env` |
| `ORACLE_FM_QUERY_TIMEOUT_S` | Timeout por round-trip (s) | `30` |
| `ORACLE_FM_RC8_CHUNK_DAYS` | Días por tramo en rc8 (`0`=off) | `3` |

Nunca commitees `.env`.

## Registro en Cursor

Ejemplo en `cursor-mcp.example.json`. En la config MCP del cliente:

```json
{
  "mcpServers": {
    "oss-fm-oracle": {
      "command": "C:\\DVB\\python\\python.exe",
      "args": ["-m", "oss_fm_oracle_mcp"],
      "cwd": "C:\\DVB\\mcp\\oss-fm-oracle-mcp",
      "env": {
        "ORACLE_FM_USER": "tu_usuario",
        "ORACLE_FM_PASSWORD": "tu_password",
        "ORACLE_FM_HOST": "192.0.2.204",
        "ORACLE_FM_SERVICE": "OSS"
      }
    }
  }
}
```

También podés dejar las credenciales solo en `.env` del proyecto (se carga al arrancar).
El `.env` se resuelve relativo al paquete (no al cwd), así que `cwd` es opcional.

## Registro en Claude Code

Claude Code (stdio) solo admite `command`/`args`/`env` (sin `cwd` ni `envFile`); no hace
falta nada más porque el `.env` se localiza desde el propio paquete:

```powershell
claude mcp add oss-fm-oracle --scope user `
  -e PYTHONPATH=C:\DVB\mcp\oss-fm-oracle-mcp\src -e PYTHONIOENCODING=utf-8 `
  -- C:\DVB\python\python.exe -m oss_fm_oracle_mcp
```

## Herramientas MCP

| Tool | Descripción |
|------|-------------|
| `list_rcs(probe=true)` | Collectors, puertos, DSN y límites; probe paralelo de conectividad |
| `test_connection(rc)` | Versión Oracle + `SYSDATE`. `rc="all"` prueba las seis |
| `describe_table(rc, owner, table)` | Columnas / tipos / nullability |
| `query(rc, sql, max_rows?, timeout_s?)` | SELECT/WITH; `rc` = uno, lista o `"all"`; default 500 filas; `max_rows` hasta 20000; `timeout_s` hasta 300 |
| `query_all(sql, …)` | Broadcast paralelo a los 6; etiqueta por RC; un fallo no aborta hermanos |
| `active_alarms_summary(rcs?, group_by?)` | Activas (`CANCEL_TIME IS NULL`): conteos y top textos |
| `alarm_history(dn_contains, …)` | Historial por fragmento de DN |
| `top_alarms(since_hours?, by?, top?)` | Alarmas más frecuentes en la ventana |
| `alarm_timeline(…)` | Conteos por hora o día |
| `list_tables(rc, owner?, search?)` | Tablas/vistas en ALL_TABLES / ALL_VIEWS |
| `explain_query(rc, sql)` | EXPLAIN PLAN + DBMS_XPLAN resumido |
| `export_query(sql, rcs?, format?)` | CSV/Parquet en `ORACLE_FM_EXPORT_DIR` (un archivo por RC) |

Toda respuesta de consulta incluye: `rows`, `columns`, `row_count`, `rc`, `execution_time_s`, `truncated`.

## Ejemplos

Alarmas activas (conteo) en todos los RCs:

```text
query_all:
  sql: SELECT COUNT(*) AS N FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL
```

Severidad de activas en rc1:

```text
query:
  rc: rc1
  sql: SELECT PERCEIVED_SEVERITY, COUNT(*) AS N
       FROM ALARMS.ALARM_LOG
       WHERE CANCEL_TIME IS NULL
       GROUP BY PERCEIVED_SEVERITY
```

Ventana temporal (en rc8 se trocea sola si supera `ORACLE_FM_RC8_CHUNK_DAYS`):

```sql
SELECT DN, ALARM_NUMBER, ALARM_TIME, PERCEIVED_SEVERITY
FROM ALARMS.ALARM_LOG
WHERE ALARM_TIME BETWEEN TO_DATE('2026-07-01 00:00:00', 'YYYY-MM-DD HH24:MI:SS')
                    AND TO_DATE('2026-07-15 00:00:00', 'YYYY-MM-DD HH24:MI:SS')
  AND CANCEL_TIME IS NULL
```

## CLI

```powershell
# Servidor MCP (stdio)
C:\DVB\python\python.exe -m oss_fm_oracle_mcp

# Smoke test manual
C:\DVB\python\python.exe -m oss_fm_oracle_mcp --smoke
```

Detalle del smoke: [docs/SMOKE_TEST.md](docs/SMOKE_TEST.md).

## Pruebas unitarias (sin DB)

```powershell
C:\DVB\python\python.exe -m pytest -q
```

Verifican rechazo de `UPDATE` / `DROP` / PL/SQL / multi-statement **antes** del driver, y que `SELECT`/`WITH` pasan.

## Seguridad

1. Solo `SELECT` / `WITH … SELECT`
2. Keywords DML/DDL/PL/SQL rechazadas con mensaje claro
3. Una sentencia por llamada
4. Sesión `SET TRANSACTION READ ONLY` (nunca `commit`)
5. Tope de filas obligatorio (`ROWNUM` / `FETCH FIRST` o envoltorio)
6. Timeout configurable

**Nunca** se escribe en Oracle desde este MCP.
