# Smoke test — OSS FM Oracle MCP

Prueba manual de conectividad y lectura mínima contra los 6 collectors.

## Prerrequisitos

1. `.env` con `ORACLE_FM_USER` y `ORACLE_FM_PASSWORD` (copiar desde `.env.example`).
2. Paquete instalado: `pip install -e ".[dev]"` desde la raíz del repo.
3. Red hasta `ORACLE_FM_HOST` (default `192.0.2.204`) y puertos RC.

## Cómo ejecutarlo

```powershell
cd C:\DVB\mcp\oracle-alarms-readonly-mcp
C:\DVB\python\python.exe -m oracle_alarms_readonly_mcp --smoke
```

Equivalente vía herramientas MCP:

1. `test_connection(rc="all")`
2. Por cada RC (o `query_all`):

```sql
SELECT COUNT(*) AS ACTIVE_ALARMS
FROM ALARMS.ALARM_LOG
WHERE CANCEL_TIME IS NULL
```

## Qué esperar

### `test_connection("all")`

Por cada RC:

- `ok: true`, `status: "connected"`
- `oracle_version`: banner Oracle
- `server_time`: `YYYY-MM-DD HH:MM:SS` (hora de red UTC-5, sin conversión)
- `port` según tabla (rc1=8001 … rc10=8004)

Si un RC no responde: `ok: false`, `error_kind: "connection"` y el resto de RCs igual se listan.

### Conteo de alarmas activas

- `ok: true` + `row_count: 1` + valor `ACTIVE_ALARMS` ≥ 0 → collector alcanzable y tabla visible.
- `ok: true` + `ACTIVE_ALARMS = 0` → **resultado válido** (no hay activas en ese RC en ese momento), no es error de conexión.
- `ok: false` → fallo de red/auth/SQL; ver `error` / `error_kind`.

## Registro de resultados

Completá al correr el smoke (fecha / entorno):

| RC   | Puerto | test_connection | ACTIVE_ALARMS | Notas |
|------|--------|-----------------|---------------|-------|
| rc1  | 8001   |                 |               |       |
| rc2  | 8002   |                 |               |       |
| rc7  | 8006   |                 |               |       |
| rc8  | 8008   |                 |               |       |
| rc9  | 8009   |                 |               |       |
| rc10 | 8004   |                 |               |       |

## Si no hay credenciales

El CLI imprime `SKIP smoke` y sale con código 2. No hay forma de verificar Oracle sin usuario/password; las unitarias de `tests/test_safety.py` sí corren sin DB.
