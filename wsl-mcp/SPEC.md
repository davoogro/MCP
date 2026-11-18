# wsl-mcp — empaquetar y mejorar (lee antes C:\DVB\mcp\_specs\COMMON.md)

Hoy existe `C:\DVB\mcp\wsl_mcp_server.py` (archivo suelto, registrado y en uso). NO lo modifiques ni lo
borres. Crea un paquete nuevo en `C:\DVB\mcp\wsl-mcp` con layout de `C:\DVB\mcp\unc-fs-mcp`
(`pyproject.toml`, `src/wsl_mcp/{__init__,__main__,config,ops,util,server}.py`, `tests/`, `README.md`,
`.env.example`, `claude-mcp.example.json`), que porte TODAS las tools existentes con los mismos nombres y
parámetros (lee el archivo suelto completo), cargando `C:\DVB\mcp\.env` igual que hoy (`WSL_*`).

Mejoras:
1. **Jobs en segundo plano**: `start_job(command, cwd=None, distro=None)` → `job_id`; `get_job(job_id,
   tail_lines=200)` (estado, exit code, duración, salida acumulada con offset), `list_jobs()`,
   `stop_job(job_id)`. Salida a archivos en `%LOCALAPPDATA%\wsl-mcp\jobs\`. Los jobs sobreviven a
   llamadas, no al proceso del MCP (documentar).
2. `run_command` acepta `timeout` hasta 3600 s y `stdin` opcional (texto).
3. `read_file` con `offset_bytes` y `max_bytes` además de líneas.
4. `copy_between(src, dst)` — copia entre Windows y WSL (rutas C:\ ↔ /mnt/c, o rutas Linux), archivos o
   carpetas, vía `cp -a` dentro de WSL.
5. `list_distros` + parámetro `distro` en todas las tools (default `WSL_DISTRO`).
6. `docker_ps`/`systemctl_status(unit=None)`/`disk_usage(path)` como atajos de lectura (si el comando no
   existe en la distro, error claro).
Tests unitarios (sin WSL real, con mocks de subprocess) + un test real marcado skip si `wsl.exe` no existe.
