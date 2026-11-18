# windows-fleet-mcp — especificación

Servidor MCP (stdio) para administrar la flota de Windows Server de la empresa desde Claude Code,
sucesor de `C:\DVB\mcp\windows-server-admin-mcp` (Python) y `C:\DVB\mcp\windows-admin-mcp` (TS).
Objetivo: MUCHO más rápido (sesiones PowerShell persistentes), consultas a varios servidores en paralelo,
muchas más herramientas de lectura y, para el operador (admin de dominio en su PC corporativo),
capacidad completa: PowerShell arbitrario, copiar archivos, gestionar servicios/procesos/tareas,
todo auditado y controlado por permisos en el inventario.

## Restricciones generales

- Python >= 3.10 (intérprete real `C:\DVB\python\python.exe`, 3.14). Paquete `windows_fleet_mcp` en
  `C:\DVB\mcp\windows-fleet-mcp\src\windows_fleet_mcp`. Mismo layout que `C:\DVB\mcp\unc-fs-mcp`:
  `pyproject.toml` (hatchling), `__init__.py` (`__version__ = "1.0.0"`), `__main__.py`, `config.py`,
  `worker.py`, `probes.ps1`, `worker_host.ps1`, `ops.py`, `audit.py`, `util.py`, `server.py`, `tests/`,
  `README.md`, `.env.example`, `.gitignore`, `claude-mcp.example.json`.
  LEE primero `C:\DVB\mcp\unc-fs-mcp\src\unc_fs_mcp\server.py`, `config.py`, `util.py` y copia su estilo
  (CLI `--list`, `--test`, `--handshake`, `bound_response`, carga de `.env` relativo al paquete).
  Mira también `C:\DVB\mcp\windows-server-admin-mcp\src\windows_server_admin_mcp\` (confirmations.py,
  policies.py, audit.py, services\restart.py) para reutilizar ideas del token de reinicio y auditoría.
- SDK `mcp` 2.0: `from mcp.server import MCPServer`, `from mcp.types import ToolAnnotations`
  (NO existe `mcp.server.fastmcp`). Dependencias: `mcp>=2.0.0`, `python-dotenv>=1.0`, `pyyaml>=6`. Nada más.
- Nunca depender del cwd: config, scripts .ps1 y logs se resuelven desde `Path(__file__)` o env vars.
- Logging solo a stderr. Mensajes, docstrings e `instructions` en español.
- Cada tool devuelve `dict` JSON-serializable. Éxito `{"ok": true, ...}`; error
  `{"ok": false, "error": "...", "error_code": "NotFound|NotAllowed|Unreachable|AuthFailed|Timeout|InvalidArgument|RemoteError"}`.
  Nunca propagar excepciones ni stacktraces.
- Respuesta acotada por `WINFLEET_MAX_RESPONSE_CHARS` (default 100000) → `"response_truncated": true`.
- Tamaños: bytes + `*_human` (base 1024). Fechas ISO-8601.

## Inventario (`config/servers.yaml`)

```yaml
defaults:
  port: 5985
  use_ssl: false
  open_timeout_seconds: 20
  operation_timeout_seconds: 180
  permissions:            # OPT-IN: solo lectura por defecto; lo que modifica se habilita por servidor
    read: true
    manage_services: false     # start/stop/restart servicios
    manage_processes: false    # stop_process
    manage_tasks: false        # run/enable/disable tareas programadas
    run_powershell: false      # run_powershell arbitrario
    file_transfer: false       # copy_to_server / copy_from_server / write_remote_file
    restart_server: false      # prepare/confirm/cancel_server_restart (siempre 2 pasos con token)
  file_read_denylist:          # globs que read_remote_file nunca lee
    - "*\\config\\SAM"
    - "*\\config\\SECURITY"
    - "*.pfx"
    - "*.p12"
servers:
  appsrv01: {hostname: appsrv01, environment: integration, tags: [windows, apps]}
  appsrvb03:   {hostname: appsrvb03,   environment: integration, tags: [windows, ccos]}
  appsrv05: {hostname: appsrv05, environment: integration, tags: [windows, apps]}
  appsrv03: {hostname: appsrv03, environment: integration, tags: [windows, apps]}
  dbsrvb05:   {hostname: dbsrvb05,   environment: integration, tags: [windows, ccos]}
protected_services: [RpcSs, LanmanServer, NTDS, DNS, WinRM, EventLog, SamSs]   # stop/restart rechazado
```

- Cada servidor hereda `defaults` (merge profundo); puede sobreescribir `permissions`, `port`, etc.
- Los permisos que modifican NUNCA vienen habilitados en el repo: el operador los enciende por servidor
  en `config/servers.local.yaml` (crear `servers.local.yaml.example` mostrando cómo, con todo en false).
  `list_servers` muestra los permisos efectivos y cada `NotAllowed` dice qué clave habilitar y en qué archivo.
- Override opcional `config/servers.local.yaml` (gitignored) que se fusiona encima. Ruta configurable con
  `WINFLEET_CONFIG` (archivo) — default `<proyecto>/config/servers.yaml`.
- **Selector de servidores** (parámetro `servers` en casi todas las tools): string o lista. Acepta
  `server_id`, `"all"`, `"tag:<tag>"`, `"env:<environment>"`, o lista de ellos. Solo IDs del inventario;
  cualquier otra cosa → `InvalidArgument` con la lista de IDs válidos. Herramientas de un solo servidor
  usan `server` (un ID).

## Ejecución: worker PowerShell persistente (lo más importante)

`worker_host.ps1` es un proceso `powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File worker_host.ps1`
(Windows PowerShell 5.1; ejecutable configurable `WINFLEET_POWERSHELL`) que vive mientras vive el MCP:

- Protocolo: una línea JSON por petición en stdin → una línea JSON por respuesta en stdout, con `id`.
  Fuerza UTF-8 (`[Console]::InputEncoding/OutputEncoding = UTF8`). Todo lo demás (warnings, verbose) se
  suprime o va a stderr; stdout solo lleva líneas de respuesta.
- Petición: `{"id": "...", "op": "invoke", "hosts": ["appsrv01", ...], "probe": "<nombre>",
  "args": {...}, "timeout": 180}` o `{"op": "invoke_script", "script": "<texto>", ...}` para
  run_powershell, `{"op": "close_sessions", "hosts": [...]}`, `{"op": "ping"}`, `{"op": "copy_to"|"copy_from", ...}`.
- Mantiene un caché `hostname -> PSSession` (New-PSSession con `-Authentication Default` = Kerberos,
  identidad actual; `New-PSSessionOption -OpenTimeout/-OperationTimeout` en milisegundos). Reutiliza
  sesiones con `State -eq 'Opened'`; si está Broken/Closed la elimina y reabre. Si `Invoke-Command`
  falla por sesión rota, reintenta UNA vez con sesión nueva.
- Multi-host: un solo `Invoke-Command -Session $sessions -ScriptBlock $sb -ArgumentList $argsJson
  -ThrottleLimit 16 -ErrorAction SilentlyContinue -ErrorVariable errs` → resultados en paralelo nativo;
  agrupa por `PSComputerName`; errores por host desde `errs` (`TargetObject`/`OriginInfo`). Hosts cuya
  sesión no abre se reportan con `error_code` (`Unreachable` si DNS/TCP, `AuthFailed` si acceso denegado).
- Las probes se definen en `probes.ps1` como un hashtable `$Probes = @{ name = { param($a) ... } }`
  cargado por el worker; el scriptblock remoto recibe los argumentos como UN string JSON y hace
  `ConvertFrom-Json` dentro (no concatenar strings de usuario dentro del código). Devuelven objetos
  simples; el worker serializa con `ConvertTo-Json -Depth 8 -Compress`. Ojo en PS 5.1: arrays de 1
  elemento → forzar con `@()`; fechas → `.ToString('o')`.
- `worker.py`: clase `PowerShellWorker` thread-safe. Pool de `WINFLEET_WORKERS` (default 3) workers
  para que una llamada lenta no bloquee otras; cada petición toma un worker libre (cola). Lectura de
  stdout en hilo dedicado, emparejado por `id`. Timeout por petición: si vence, mata el árbol del worker
  (`taskkill /F /T /PID`) y lo recrea (las sesiones se reabren solas), devuelve `Timeout`.
  Arranque perezoso. `atexit` cierra sesiones (`Remove-PSSession`) y mata workers.
- La respuesta de cada tool multi-host: `{"ok": true, "results": {"<server_id>": {...datos...} |
  {"ok": false, "error":..., "error_code":...}}, "summary": {...}, "duration_ms": n}`.
  Para un solo servidor, el mismo formato (simple y uniforme).

## Tools de lectura (`readOnlyHint=True, openWorldHint=True`)

1. `list_servers()` — inventario: id, hostname, environment, tags, permisos efectivos, si hay sesión abierta.
2. `test_connection(servers="all")` — por host: DNS, TCP puerto WinRM (socket Python, 3 s), apertura de
   sesión, whoami remoto, versión PS, latencia. Rápido (paralelo), en vez de los 40 s del MCP viejo.
3. `fleet_overview(servers="all", disk_free_pct_warn=10, mem_used_pct_warn=90)` — snapshot de salud
   paralelo: OS, uptime, CPU %, memoria %, discos (libre %), reinicio pendiente, servicios Auto detenidos
   (excluye los de inicio retrasado/trigger que normalmente se detienen: `gupdate`, `MapsBroker`,
   `RemoteRegistry`, `sppsvc`, `edgeupdate`, `CDPSvc`, `tiledatamodelsvc`, `WbioSrvc` y similares),
   errores en System/Application últimas 24 h (conteo). Añade `alerts: [{server, severity, message}]`
   ordenado por severidad. Es la herramienta "¿cómo está todo?".
4. `get_server_health(servers)` — OS, build, dominio, uptime, CPU, memoria, pagefile, reinicio pendiente
   con TODOS los indicadores (CBS RebootPending, WU RebootRequired, PendingFileRenameOperations, SCCM si existe),
   última hotfix.
5. `get_disks(servers, include_volumes=false)` — discos lógicos (y volúmenes/mount points opcional), libre %.
6. `get_services(servers, name=None, state=None, start_mode=None, problems_only=false, limit=300)` —
   `name` admite comodines `*` sobre Name o DisplayName. Incluye pid, cuenta de servicio, path.
   `problems_only` = StartMode Auto y no Running.
7. `get_processes(servers, name=None, sort_by="cpu"|"memory", top=25, include_command_line=false, include_owner=false)`.
8. `get_events(servers, log="System", level=None (critical|error|warning|info), since_hours=24,
   provider=None, event_id=None, contains=None, max_events=50)` — `Get-WinEvent -FilterHashtable`;
   `contains` filtra el mensaje; mensajes recortados a 2000 chars. `log` puede ser cualquier canal
   (`Application`, `Security`, `Microsoft-Windows-TaskScheduler/Operational`, ...).
9. `list_event_logs(server, contains=None)` — canales disponibles con conteo y tamaño.
10. `get_scheduled_tasks(servers, path="\\", name=None, failed_only=false, include_microsoft=false)` —
    estado, última/próxima ejecución, LastTaskResult (hex), acciones (exe + args), usuario.
11. `get_installed_updates(servers, last=20)` — hotfixes ordenadas por fecha + fecha de última instalación.
12. `get_installed_software(servers, name=None)` — registro Uninstall 32/64 bits.
13. `get_network(server)` — IPs, gateways, DNS, rutas por defecto, puertos en escucha con proceso dueño
    (`Get-NetTCPConnection -State Listen` + `Get-Process`), resumen de conexiones establecidas por IP remota.
14. `test_network_from_server(servers, target, port=None)` — desde el servidor: ping/Test-NetConnection a
    `target` (host o IP, validado con regex de hostname/IP), puerto opcional, resolución DNS.
15. `get_sessions(servers)` — usuarios con sesión (`quser` parseado), sesiones RDP.
16. `get_certificates(servers, store="My", expiring_days=None)` — LocalMachine; subject, thumbprint, expiración, días restantes.
17. `get_local_admins(servers)` — miembros del grupo Administradores local.
18. `get_shares(servers)` — recursos compartidos SMB y su ruta.
19. `get_iis(servers)` — si WebAdministration existe: sitios, bindings, app pools y estado; si no, `{installed: false}`.
20. `get_performance(servers, samples=3, interval_seconds=1)` — contadores clave (CPU, cola de disco,
    memoria disponible, red bytes/s) promediados.
21. `get_firewall_rules(server, enabled_only=true, direction=None, name=None, limit=200)`.
22. `list_remote_dir(server, path, pattern="*", recurse=false, max_entries=500)` — path absoluto local del
    servidor (`D:\logs`) — nombre, tamaño, fechas; totales.
23. `read_remote_file(server, path, offset=0, max_bytes=1048576, encoding="auto")` — texto; respeta
    `file_read_denylist`. Detecta binario y lo rechaza (sugiere copy_from_server).
24. `tail_remote_file(server, path, lines=200, contains=None)` — `Get-Content -Tail`, filtro opcional.
25. `search_remote_files(server, path, name_pattern="*", contains=None, max_results=200, max_depth=8)` —
    busca por nombre y opcionalmente texto (Select-String, con tope).
26. `compare_servers(servers, aspect="services"|"hotfixes"|"software"|"scheduled_tasks")` — tabla de
    diferencias: elementos presentes en unos y no en otros, o con distinto estado/versión.
27. `query_audit_log(since_hours=24, tool=None, server=None, limit=100)`.
28. `wait_for_server(server, timeout_seconds=900, poll_seconds=15)` — espera a que vuelva WinRM tras reinicio.

## Tools que modifican (`readOnlyHint=False, destructiveHint=True`)

Todas: verifican el permiso del servidor (`NotAllowed` si falta), registran auditoría completa
(incluyendo el script de run_powershell), y aceptan `dry_run` cuando tiene sentido.

29. `run_powershell(servers, script, timeout_seconds=180)` — PowerShell ARBITRARIO en uno o varios
    servidores (en paralelo por las sesiones persistentes). Devuelve por host: `output` (objetos
    convertidos a JSON si se puede, si no texto con `Out-String -Width 250`), `errors`, `had_errors`.
    Es la herramienta "sin límites": la descripción debe decirle al modelo que prefiera las tools
    específicas para lectura y que use esta para lo que no esté cubierto.
30. `manage_service(servers, name, action="start"|"stop"|"restart", dry_run=true, force=false)` —
    nombre exacto (sin comodines); rechaza `protected_services`; devuelve estado antes/después y espera
    hasta 60 s al estado final.
31. `set_service_start_mode(server, name, start_mode="Automatic"|"Manual"|"Disabled", dry_run=true)`.
32. `stop_process(server, pid=None, name=None, dry_run=true)` — rechaza PIDs <= 4, csrss, lsass, wininit,
    winlogon, services, smss.
33. `manage_scheduled_task(server, task_path, task_name, action="run"|"stop"|"enable"|"disable", dry_run=true)`.
34. `write_remote_file(server, path, content, append=false, create_dirs=false, backup=true)` — texto UTF-8;
    `backup` copia el original a `<archivo>.bak-<yyyyMMddHHmmss>`.
35. `copy_to_server(server, local_path, remote_path)` / 36. `copy_from_server(server, remote_path, local_path)` —
    `Copy-Item -ToSession/-FromSession`, archivos o carpetas (`-Recurse`); devuelve bytes copiados.
37. `prepare_server_restart(server, reason, delay_seconds=60)` → token de un solo uso (TTL 10 min,
    ligado a server+reason+delay; muestra uptime, sesiones activas y servicios clave para que el usuario decida).
38. `confirm_server_restart(server, reason, delay_seconds, token)` → `shutdown /r /t <delay> /c <reason> /d p:0:0`.
39. `cancel_server_restart(server)` → `shutdown /a`.
40. `disconnect_sessions(servers=None)` — cierra las PSSession cacheadas (local, no afecta servidores);
    útil si cambian credenciales.

## Auditoría (`audit.py`)

JSONL diario en `WINFLEET_AUDIT_DIR` (default `<proyecto>/logs/`): timestamp, actor (usuario Windows),
tool, servers, parámetros (los `content`/`script` largos se guardan completos hasta 20 kB), ok,
error_code, duration_ms. Nunca guardar el token de reinicio.

## server.py

- `instructions` del MCP (español): lista de servidores, selector (`all`, `tag:`, `env:`), flujo
  recomendado (`fleet_overview` → herramienta específica → `run_powershell` si nada encaja), que las
  tools que modifican requieren que el usuario lo haya pedido explícitamente, y que el reinicio es en 2 pasos.
- CLI: `python -m windows_fleet_mcp --list` (tools), `--test` (test_connection all, imprime JSON),
  `--handshake` (initialize + tools/list vía stdio contra sí mismo), sin args = stdio.

## Tests (`tests/`, pytest, SIN red)

- Config: merge defaults/local, selector (`all`, `tag:`, `env:`, inválidos), permisos.
- Worker: test con un `FakeWorker` y también un test real del protocolo arrancando `worker_host.ps1`
  con una petición `ping` y una probe local simulada (`op: "invoke_local"` solo para tests: ejecuta la
  probe en el proceso local sin sesión remota) — marcar con `@pytest.mark.skipif(sys.platform != "win32")`.
- Validaciones: denylist de archivos, servicios protegidos, PIDs protegidos, regex de target.
- Token de reinicio: un solo uso, TTL, parámetros ligados.
- bound_response / truncado.
- Handshake MCP: lista las 40 tools con annotations correctas.
Objetivo: `C:\DVB\python\python.exe -m pytest -q` en verde.

## Entregables

Código + tests en verde + `README.md` (español, tabla de tools, ejemplo `claude mcp add`) +
`claude-mcp.example.json`. Al terminar escribe `DONE.md` con: archivos creados, resultado de pytest,
y cualquier cosa que no pudiste hacer. NO registres el MCP en ningún config (lo hago yo).
NO ejecutes nada que modifique los servidores reales. Sí puedes probar en vivo herramientas de
LECTURA contra `appsrv01` (p. ej. `python -m windows_fleet_mcp --test`).
