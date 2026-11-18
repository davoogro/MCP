# ssh-linux-mcp

MCP de **solo lectura** para servidores Linux vía SSH (paramiko) a través de un proxy SOCKS5 local
(`127.0.0.1:18080`, debe estar levantado).

- Un `SSHClient` persistente por host (keepalive 15 s). Si la conexión cae, se reconecta una vez y se reintenta.
- Credenciales: variables `LINUX_SSH_USER` / `LINUX_SSH_PASSWORD` de `.env` (`ssh-linux-mcp\.env`, luego `C:\DVB\mcp\.env`, luego
  `SSHLX_ENV_FILE`). La contraseña nunca se loguea ni se devuelve.
- Host keys TOFU en `%LOCALAPPDATA%\ssh-linux-mcp\known_hosts`: la primera vez se acepta y guarda (warning con la
  huella); si cambia, se rechaza la conexión.
- `run_command` valida cada línea con `readonly.validate` (allowlist de comandos de diagnóstico, sin redirecciones
  salvo a `/dev/null`, sin `$(...)`/backticks/asignaciones/`&`, reglas por comando: `sed -i`, `find -delete/-exec`,
  `systemctl restart`, `curl -o`, `tar -x`...). Las tools estructuradas también pasan por el validador.
- SFTP solo para leer (`listdir_attr`, `stat`, `open('rb')`, `get`).
- Auditoría local en `%LOCALAPPDATA%\ssh-linux-mcp\audit.jsonl`; respuestas recortadas a `SSHLX_MAX_CHARS` (60000).

## Hosts

`config/hosts.json` (o `SSHLX_CONFIG`): `hostname`, `port`, `user_env`, `password_env`, `proxy` opcional
(`socks5://host:puerto`; sin proxy = conexión directa) y `description`.

## Tools (18)

| Área | Tools |
|---|---|
| Conexión / ejecución | list_hosts, test_connection, run_command |
| Sistema | get_system_info, disk_usage |
| Archivos | list_directory, get_file_info, read_file, tail_file, search_files, grep_text, download_file |
| Procesos / red / servicios | get_processes, get_listening_ports, get_services, service_status, get_journal |
| Auditoría | query_audit_log |

Descargas por defecto en `C:\DVB\exports\ssh-linux\<host>\` (`SSHLX_TRANSFER_DIR`), máx. 500 MB.

## Uso

```
$env:PYTHONPATH = "C:\DVB\mcp\ssh-linux-mcp\src"
C:\DVB\python\python.exe -m ssh_linux_mcp --test        # prueba conexión al host por defecto
C:\DVB\python\python.exe -m ssh_linux_mcp --handshake   # lista tools
C:\DVB\python\python.exe -m pytest C:\DVB\mcp\ssh-linux-mcp\tests -q
.\registrar.ps1                                         # registra en Claude Code (scope user)
```
