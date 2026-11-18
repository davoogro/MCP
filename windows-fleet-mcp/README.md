# windows-fleet-mcp

Servidor MCP (stdio) para administrar la flota Windows Server de la empresa: sesiones PowerShell persistentes, consultas en paralelo, inventario YAML con permisos opt-in y auditoría JSONL.

## Requisitos

- Python ≥ 3.10 (`C:\DVB\python\python.exe`)
- Windows PowerShell 5.1 en el equipo operador
- WinRM/Kerberos hacia los servidores del inventario

## Instalación

```powershell
cd C:\DVB\mcp\windows-fleet-mcp
C:\DVB\python\python.exe -m pip install -e ".[dev]"
```

Copia `.env.example` a `.env` si necesitas overrides. Permisos de escritura: copia `config/servers.local.yaml.example` a `config/servers.local.yaml` (gitignored).

## CLI

| Comando | Descripción |
|---------|-------------|
| `python -m windows_fleet_mcp` | Servidor MCP stdio |
| `python -m windows_fleet_mcp --list` | Configuración e inventario |
| `python -m windows_fleet_mcp --test` | `test_connection` a `all` (JSON) |
| `python -m windows_fleet_mcp --handshake` | Verifica 40 tools MCP |

## Claude Code

```bash
claude mcp add windows-fleet -- C:\DVB\python\python.exe -m windows_fleet_mcp
```

Ver `claude-mcp.example.json` con `PYTHONPATH=src`.

## Inventario

`config/servers.yaml` define servidores y `defaults`. Override local: `config/servers.local.yaml` o `WINFLEET_CONFIG`.

Selector `servers` en tools: `all`, `tag:apps`, `env:integration`, ID o lista.

## Tools (40)

### Lectura (28)

| Tool | Descripción |
|------|-------------|
| `list_servers` | Inventario y permisos |
| `test_connection` | DNS, TCP, sesión, whoami |
| `fleet_overview` | Salud agregada y alertas |
| `get_server_health` | Salud detallada y reinicio pendiente |
| `get_disks` | Discos y volúmenes |
| `get_services` | Servicios con filtros |
| `get_processes` | Top procesos |
| `get_events` | Event Viewer |
| `list_event_logs` | Canales disponibles |
| `get_scheduled_tasks` | Tareas programadas |
| `get_installed_updates` | Hotfixes |
| `get_installed_software` | Uninstall registry |
| `get_network` | Red y puertos |
| `test_network_from_server` | Ping/TNC desde servidor |
| `get_sessions` | Sesiones usuario |
| `get_certificates` | Cert store |
| `get_local_admins` | Grupo Administradores |
| `get_shares` | SMB shares |
| `get_iis` | IIS si instalado |
| `get_performance` | Contadores |
| `get_firewall_rules` | Firewall |
| `list_remote_dir` | Listado remoto |
| `read_remote_file` | Lectura texto |
| `tail_remote_file` | Tail remoto |
| `search_remote_files` | Búsqueda |
| `compare_servers` | Diferencias entre hosts |
| `query_audit_log` | Auditoría local |
| `wait_for_server` | Espera post-reinicio |

### Modificación (12)

| Tool | Permiso requerido |
|------|-------------------|
| `run_powershell` | `run_powershell` |
| `manage_service` | `manage_services` |
| `set_service_start_mode` | `manage_services` |
| `stop_process` | `manage_processes` |
| `manage_scheduled_task` | `manage_tasks` |
| `write_remote_file` | `file_transfer` |
| `copy_to_server` / `copy_from_server` | `file_transfer` |
| `prepare_server_restart` / `confirm_server_restart` / `cancel_server_restart` | `restart_server` |
| `disconnect_sessions` | (local, sin permiso) |

## Tests

```powershell
C:\DVB\python\python.exe -m pytest -q
```

Sin red: `FakeWorker` y tests de configuración/validación. En Windows: ping e `invoke_local` contra `worker_host.ps1`.

## Variables de entorno

| Variable | Default |
|----------|---------|
| `WINFLEET_CONFIG` | `config/servers.yaml` |
| `WINFLEET_AUDIT_DIR` | `logs/` |
| `WINFLEET_WORKERS` | `3` |
| `WINFLEET_POWERSHELL` | `powershell.exe` |
| `WINFLEET_MAX_RESPONSE_CHARS` | `100000` |
