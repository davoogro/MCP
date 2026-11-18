# ssh-winserver-mcp

MCP para administrar Windows Server **con permisos de escritura** vía SSH (OpenSSH de Windows) + PowerShell 5.1.
Usa los alias de `~/.ssh/config` (ProxyJump, IdentityFile...), así que funciona igual que `ssh winsrv01`.

- Sesión `powershell.exe` persistente por host: la primera llamada tarda ~4 s (salto por jumphost), las siguientes ~0.1 s.
  Si un comando excede su timeout se mata la sesión y la siguiente llamada reconecta.
- Parámetros pasados como JSON/base64 (sin inyección por comillas). Cada script corre en un scope nuevo.
- Tools destructivas exigen `confirm=true`. Auditoría local en `%LOCALAPPDATA%\ssh-winserver-mcp\audit.jsonl`.

## Hosts

`config/hosts.json` (o `SSHWIN_CONFIG`). Añadir un servidor = un alias en `~/.ssh/config` con llave sin
passphrase (o en ssh-agent) + una entrada aquí. `allow_write: false` lo deja en solo lectura.

## Tools (62)

| Área | Tools |
|---|---|
| Sesión / ejecución | list_hosts, test_connection, run_powershell, run_cmd, run_background, get_background_job, list_background_jobs, remove_background_job |
| Sistema | get_system_info, get_performance, get_disks, extend_volume, get_events, list_event_logs, restart_server, cancel_restart, wait_for_server |
| Servicios / procesos | get_services, manage_service, configure_service, create_service, delete_service, get_processes, stop_process |
| Archivos | list_directory, read_file, write_file, replace_in_file, file_operation, search_files, get_file_info, set_permissions, upload_file, download_file |
| Roles / software | get_windows_features, install_windows_feature, uninstall_windows_feature, get_installed_software, get_updates, windows_update |
| Red / firewall | get_network, get_listening_ports, get_tcp_connections, test_network, get_firewall_rules, add_firewall_rule, manage_firewall_rule |
| Tareas programadas | get_scheduled_tasks, manage_scheduled_task, create_scheduled_task |
| Usuarios / registro / otros | get_local_users_groups, manage_local_group_member, manage_local_user, get_registry, set_registry, delete_registry, get_environment, set_environment, get_certificates, get_shares, manage_share |
| Auditoría | query_audit_log |

`run_background` y `windows_update` corren como tarea programada **SYSTEM** (sobreviven a la sesión SSH;
SYSTEM no tiene credenciales de red del usuario). Transferencias con `scp`; descargas por defecto en
`C:\DVB\exports\ssh-winserver\<host>\`.

## Uso

```
python -m ssh_winserver_mcp --test         # prueba conexión al host por defecto
python -m ssh_winserver_mcp --handshake    # lista tools
python tests/live_check.py                 # prueba en vivo (crea y borra cosas en C:\Windows\Temp)
.\registrar.ps1                            # registra en Claude Code (scope user)
```

Notas PowerShell: no ejecutar programas interactivos (leerían el stdin de la sesión); castear a `[string]`
las líneas de `Get-Content` antes de `Out-Json` (sus NoteProperties cuelgan ConvertTo-Json).
