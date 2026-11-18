# NiFi MCP (solo lectura)

Servidor [MCP](https://modelcontextprotocol.io) multi-instancia para **Apache NiFi** en modo **solo lectura**. Expone inventario, estado, diagnósticos y búsqueda normalizados para agentes LLM (Cursor, Claude Desktop, etc.).

Implementado con **Python ≥ 3.10** y **MCP SDK v2** (`mcp>=2,<3`).

## Características

- Varios servidores NiFi desde un único YAML (`config/nifi_servers.yaml`)
- Cliente **async httpx** con reintentos acotados, TLS estricto y descubrimiento automático de URL
- Autenticación: `none`, `username_password` (POST `/access/token`, token solo en memoria), `bearer_token`, `client_certificate`
- Caché TTL en memoria (inventario / estado) con `refresh`
- Concurrencia multi-servidor limitada por `max_parallel_servers`
- Redacción recursiva de propiedades sensibles
- Chequeos de salud configurables (colas, bulletins, componentes inválidos)
- **Sin herramientas mutativas** — GET estricto salvo POST interno de token y consultas read-only de provenance/cola (POST+GET+DELETE)

## Instalación

```powershell
cd C:\DVB\mcp\nifi-readonly-mcp
C:\DVB\python\python.exe -m pip install -e ".[dev]"
copy .env.example .env
```

**Importante:** `python -m nifi_readonly_mcp` (vía `.cursor/mcp.json`) requiere la instalación editable anterior para resolver el paquete.

Edite `config/nifi_servers.yaml` (o copie desde `config/servers.example.yaml`). **No almacene secretos en YAML**; use variables de entorno referenciadas en `auth.*_env`.

Servidores actuales (puertos conocidos):

| ID | Host | Puerto | Estado observado |
|----|------|--------|------------------|
| `appsrv01` | appsrv01 | 8443 HTTPS | Autenticado (usuario/password); CA pin local |
| `appsrv02` | appsrv02 | 8080 HTTP | Sin password; API abierta |
| `appsrv03` | appsrv03 | 8080 HTTP | Sin password; API abierta |
| `nifisrv05` | nifisrv05 | 8445 HTTPS | Autenticado (usuario/password); CA pin local |

## Configuración

| Variable | Descripción |
|----------|-------------|
| `NIFI_MCP_CONFIG_DIR` | Directorio con `nifi_servers.yaml` (default: `<proyecto>\config`, independiente del cwd) |
| `NIFI_MCP_ENV_FILE` | Ruta alternativa del `.env` (default: `<proyecto>\.env`, independiente del cwd) |
| `NIFI_MCP_MAX_RESPONSE_CHARS` | Tope del JSON devuelto por herramienta (default `80000`) |
| `NIFI_<SERVER>_USER` / `PASSWORD` / `TOKEN` | Credenciales por servidor según `auth.*_env` |
| `NIFI_MCP_LIVE_TESTS=1` | Habilita pruebas de integración read-only |

### Añadir un servidor

```yaml
servers:
  mi-nifi:
    name: "Mi NiFi"
    host: nifi.example.local
    enabled: true
    base_url: null          # auto: https://host/nifi-api, :8443, :8080
    tls:
      verify: true
      ca_cert: null
      client_cert: null
      client_key: null
    auth:
      type: username_password
      username_env: NIFI_MI_NIFI_USER
      password_env: NIFI_MI_NIFI_PASSWORD
      bearer_token_env: NIFI_MI_NIFI_TOKEN
```

### Tipos de autenticación

| Tipo | Comportamiento |
|------|----------------|
| `none` | Sin cabeceras de auth |
| `username_password` | POST form a `/access/token`; bearer en memoria |
| `bearer_token` | Token desde env `bearer_token_env` |
| `client_certificate` | mTLS vía `tls.client_cert` / `client_key` |

## Cursor — `.cursor/mcp.json`

Formato exacto (stdio, sin secretos en el JSON):

```json
{
  "mcpServers": {
    "nifi": {
      "type": "stdio",
      "command": "C:\\DVB\\python\\python.exe",
      "args": ["-m", "nifi_readonly_mcp"],
      "envFile": "C:\\DVB\\mcp\\nifi-readonly-mcp\\.env"
    }
  }
}
```

Copie `cursor-mcp.example.json` o use `.cursor/mcp.json` incluido en el proyecto.

## Claude Code

Claude Code (stdio) solo admite `command`/`args`/`env` (sin `cwd` ni `envFile`). El
paquete localiza `.env` y `config/` desde su propia ruta, así que basta con:

```powershell
claude mcp add nifi-readonly --scope user `
  -e PYTHONPATH=C:\DVB\mcp\nifi-readonly-mcp\src -e PYTHONIOENCODING=utf-8 `
  -- C:\DVB\python\python.exe -m nifi_readonly_mcp
```

## Herramientas disponibles

| Herramienta | Descripción | Multi-servidor |
|-------------|-------------|----------------|
| `nifi_list_servers` | Servidores configurados | Sí |
| `nifi_server_info` | Metadatos y estado live | No |
| `nifi_test_connection` | Conectividad, versión, cluster | No |
| `nifi_test_all_connections` | Prueba paralela de conexiones | Sí |
| `nifi_get_root_flow` | Process group raíz | No |
| `nifi_list_process_groups` | Hijos (recursivo opcional) | No |
| `nifi_get_process_group` | Detalle de PG | No |
| `nifi_list_processors` | Procesadores en PG | No |
| `nifi_get_processor` | Detalle de procesador | No |
| `nifi_search` | Búsqueda global o por servidor | Sí |
| `nifi_get_status` | Estado del flow raíz | No |
| `nifi_get_process_group_status` | Estado de PG | No |
| `nifi_get_processor_status` | Estado de procesador | No |
| `nifi_get_connection_status` | Colas de conexión | No |
| `nifi_list_connections` | Conexiones con colas y backpressure | Sí |
| `nifi_get_provenance` | Eventos provenance (consulta POST/GET/DELETE) | No |
| `nifi_list_queue` | Flowfiles en cola de una conexión | No |
| `nifi_cluster_status` | Nodos y estado de cluster | Sí |
| `nifi_get_controller_service` | CS con propiedades y referencias | No |
| `nifi_flow_versions` | PGs bajo control de versiones | Sí |
| `nifi_get_parameter_context` | Un contexto de parámetros | No |
| `nifi_system_diagnostics` | Heap, threads, repos JVM | Sí |
| `nifi_get_bulletins` | Tablero de bulletins | No |
| `nifi_get_errors` | Bulletins ERROR/WARN | Sí |
| `nifi_health_check` | Salud individual o global | Sí |
| `nifi_health_overview` | Resumen compacto de salud | Sí |
| `nifi_find_processors` | Filtro por tipo/nombre/estado | Sí |
| `nifi_find_large_queues` | Colas sobre umbral | Sí |
| `nifi_find_stopped_processors` | STOPPED (informativo) | Sí |
| `nifi_find_invalid_components` | Inválidos / deshabilitados | Sí |
| `nifi_compare_servers` | Comparativa entre hosts | Sí |
| `nifi_list_controller_services` | Controller services | No |
| `nifi_find_problem_controller_services` | CS disabled/invalid | Sí |
| `nifi_list_parameter_contexts` | Contextos de parámetros | No |
| `nifi_processor_status_history` | Historial procesador | No |
| `nifi_process_group_status_history` | Historial PG | No |
| `nifi_debug_get` | GET limitado por allowlist | No |

## Ejemplos de prompts

- *«Revisa todos mis NiFi.»*
- *«Busca processors INVALID.»*
- *«¿Qué está fallando en appsrv03?»*
- *«Muéstrame las colas más grandes de toda la plataforma.»*
- *«Busca todos los ExecuteSQL.»*
- *«Compara appsrv01 contra appsrv03.»*

## Validación en vivo

Script read-only (DNS, TCP, `/flow/about`, salud):

```powershell
C:\DVB\python\python.exe scripts\validate_live.py --json
```

## Desarrollo

```powershell
C:\DVB\python\python.exe -m ruff check src tests
C:\DVB\python\python.exe -m pytest
```

## Limitaciones

- Solo lectura: no start/stop, no cambios de configuración; provenance y listado de cola usan POST/DELETE solo para crear/cerrar consultas read-only
- Caché en memoria (no compartida entre procesos MCP)
- Descubrimiento de URL prueba las plantillas habituales más el `port`/`scheme` configurados; no modifica YAML
- Historial de estado depende de retención NiFi
- `nifi_debug_get` usa una allowlist estricta; no sustituye herramientas tipadas

## Licencia

Uso interno — Proprietary
