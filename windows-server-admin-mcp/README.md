# windows-server-admin-mcp

Servidor MCP en Python para administración de Windows Server vía STDIO. Usa remoting PowerShell 5.1 (WinRM / PSSession) con autenticación integrada de Windows, operaciones mutables controladas por políticas, catálogo de scripts autorizados y auditoría JSONL.

> **English:** Python MCP server for Windows Server administration over STDIO using integrated Windows auth, policy-gated operations, and an authorized script catalog.

## Requisitos

- Windows 10/11 o Windows Server (máquina operador)
- Python 3.10+ (`C:\DVB\python\python.exe` o `uv`)
- Acceso WinRM HTTP (5985) o HTTPS (5986) a servidores inventariados
- Cuenta de dominio con permisos de administración remota (sin contraseñas en config)

## Inicio rápido

```powershell
cd C:\DVB\mcp\windows-server-admin-mcp
# Opcional: uv sync
C:\DVB\python\python.exe -m pip install -e .

# Copiar inventario local (gitignored)
Copy-Item config\servers.local.yaml.example config\servers.local.yaml
# Editar hostname real; mantener authentication: current_windows_identity

# Instalar en Cursor global
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1

# Validar handshake MCP
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\validate_mcp.ps1
```

Reinicie Cursor o recargue servidores MCP después de instalar.

## Herramientas MCP (17)

| Herramienta | Tipo | Descripción |
|-------------|------|-------------|
| `list_servers` | Lectura | Inventario y permisos por servidor |
| `diagnose_server_access` | Lectura | Diagnóstico WinRM/PSSession |
| `get_server_identity` | Lectura | Hostname, OS, dominio |
| `get_server_health` | Lectura | CPU, memoria, uptime, reboot pendiente |
| `get_disks` | Lectura | Uso de discos lógicos |
| `get_services` | Lectura | Servicios Windows |
| `get_processes` | Lectura | Procesos top por recursos |
| `get_windows_events` | Lectura | Eventos recientes |
| `test_remote_execution` | Lectura | Prueba ejecución remota |
| `run_authorized_script` | Escritura | Script del catálogo (.ps1) |
| `restart_service` | Escritura | Reinicio de servicio (si permitido) |
| `prepare_server_restart` | Escritura | Token de confirmación |
| `confirm_server_restart` | Escritura | Ejecutar reinicio planificado |
| `cancel_server_restart` | Escritura | Cancelar reinicio pendiente |
| `wait_for_server` | Lectura | Esperar disponibilidad post-mantenimiento |
| `get_operation_status` | Lectura | Estado de operación mutante |
| `query_audit_log` | Lectura | Consulta auditoría JSONL |

## Ejemplos de uso (Cursor / MCP)

Listar servidores:

```json
{ "tool": "list_servers", "arguments": {} }
```

Salud del servidor de integración:

```json
{
  "tool": "get_server_health",
  "arguments": { "server_id": "integration-server-01" }
}
```

Ejecutar smoke test autorizado:

```json
{
  "tool": "run_authorized_script",
  "arguments": {
    "server_id": "integration-server-01",
    "script_id": "smoke_test",
    "parameters": { "message": "Hola desde MCP" }
  }
}
```

## Configuración

| Variable | Descripción |
|----------|-------------|
| `WSA_MCP_CONFIG_DIR` | Directorio de YAML (default: `config/` del proyecto, resuelto desde el paquete, no desde el cwd) |
| `WSA_MCP_AUDIT_DIR` | Directorio del audit JSONL (default: `%LOCALAPPDATA%\WindowsServerAdminMCP\logs`) |
| `WSA_MCP_PROJECT_ROOT` | Raíz del proyecto (tests/dev) |
| `WSA_MCP_INTEGRATION=1` | Habilita tests de integración live |

Archivos:

| Archivo | Tracked | Descripción |
|---------|---------|-------------|
| `config/servers.example.yaml` | Sí | Plantilla inventario |
| `config/servers.local.yaml` | **No** | Hostnames reales |
| `config/policies.example.yaml` | Sí | Políticas globales |
| `config/scripts.yaml` | Sí | Catálogo scripts |
| `config/scripts.local.yaml` | **No** | Overlay allowlist local |

Ver [docs/CURSOR_INSTALLATION.md](docs/CURSOR_INSTALLATION.md), [docs/ADDING_SERVERS.md](docs/ADDING_SERVERS.md).

### Registro en Claude Code

Claude Code solo admite `command`/`args`/`env` (sin cwd). Lanzar Python directamente:

```powershell
claude mcp add windows-server-admin --scope user `
  -e PYTHONPATH=C:\DVB\mcp\windows-server-admin-mcp\src `
  -e WSA_MCP_CONFIG_DIR=C:\DVB\mcp\windows-server-admin-mcp\config `
  -- C:\DVB\python\python.exe -m windows_server_admin_mcp.server
```

Compatible con `mcp` 1.28+ y 2.x (`MCPServer`). Las herramientas declaran anotaciones MCP
(`readOnlyHint` / `destructiveHint`) y el servidor envía instrucciones con el modelo de seguridad.

## Pruebas

```powershell
# Unit + security (82 tests)
C:\DVB\python\python.exe -m pytest tests/unit tests/security -q

# Integración live (requiere servers.local.yaml)
$env:WSA_MCP_INTEGRATION='1'
C:\DVB\python\python.exe -m pytest tests/integration -v

# Smoke local Python
C:\DVB\python\python.exe authorized_scripts\smoke_test.py
```

## Documentación

- [SECURITY.md](SECURITY.md) — Modelo de amenazas y controles
- [OPERATIONS.md](OPERATIONS.md) — Runbook operativo
- [CHANGELOG.md](CHANGELOG.md) — Historial de versiones
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Diseño interno
- [docs/WINRM.md](docs/WINRM.md) — WinRM y PSSession
- [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) — Auth integrada
- [docs/SCRIPT_CATALOG.md](docs/SCRIPT_CATALOG.md) — Catálogo autorizado
- [docs/RESTART_WORKFLOW.md](docs/RESTART_WORKFLOW.md) — Reinicio con confirmación
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — Resolución de problemas
- [docs/LEGACY_ANALYSIS.md](docs/LEGACY_ANALYSIS.md) — Análisis código legado

## Licencia

MIT — uso interno. Repositorio **LOCAL ONLY**; no publicar sin revisión de secretos e inventario.
