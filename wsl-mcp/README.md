# wsl-mcp

Servidor MCP (stdio) para ejecutar comandos y operaciones de archivos en WSL desde Windows.
Paquete nuevo que reemplaza el script suelto `C:\DVB\mcp\wsl_mcp_server.py` (no modificado).

## Instalación

```bash
C:\DVB\python\python.exe -m pip install -e "C:\DVB\mcp\wsl-mcp[dev]"
```

Sin instalar:

```bash
set PYTHONPATH=C:\DVB\mcp\wsl-mcp\src
C:\DVB\python\python.exe -m wsl_mcp --handshake
```

Registro MCP de ejemplo: [claude-mcp.example.json](claude-mcp.example.json).

## Configuración

Carga `wsl-mcp\.env` y, sin pisar variables ya definidas, `C:\DVB\mcp\.env`.
Ver [.env.example](.env.example).

| Variable | Uso | Default |
|---|---|---|
| `WSL_DISTRO` | Distribución WSL | `mynoble24.04` |
| `WSL_USER` | Usuario Linux opcional | (vacío) |
| `WSL_DEFAULT_CWD` | cwd inicial | (home) |
| `WSL_COMMAND_TIMEOUT` | Timeout de comandos (s) | `120` |
| `WSL_MAX_OUTPUT` | Truncado stdout/stderr (chars) | `30000` |
| `WSL_SHELL` | Shell | `/bin/bash` |

Parámetro `distro` opcional en casi todas las tools (default `WSL_DISTRO`).

## Tools

**Ejecución:** `run_command` (timeout hasta 3600 s, `stdin` opcional), `run_script`, jobs
`start_job`, `get_job`, `list_jobs`, `stop_job` (salida en `%LOCALAPPDATA%\wsl-mcp\jobs\`;
sobreviven entre llamadas MCP, **no** al reiniciar el proceso del servidor).

**Archivos:** `read_file` (líneas o `offset_bytes`/`max_bytes`), `write_file`, `tail_file`,
`list_directory`, `file_info`, `find_files`, `grep_text`, `make_directory`, `copy_between`.

**Sistema:** `wsl_status`, `list_distros`, `list_processes`, `which`, `docker_ps`,
`systemctl_status`, `disk_usage`.

## CLI

```bash
C:\DVB\python\python.exe -m wsl_mcp --list
C:\DVB\python\python.exe -m wsl_mcp --test
C:\DVB\python\python.exe -m wsl_mcp --handshake
```

## Desarrollo

```bash
cd C:\DVB\mcp\wsl-mcp
C:\DVB\python\python.exe -m pytest -q
C:\DVB\python\python.exe -m ruff check src tests
```
