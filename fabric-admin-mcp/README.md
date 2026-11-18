# fabric-admin-mcp

MCP sobre Microsoft Fabric CLI (`fab` 1.8, `C:\DVB\python\Scripts\fab.exe`) para crear, modificar,
mover, exportar/importar y borrar todo lo que `fab` permite en Fabric y Power BI, más llamadas REST
directas (`api`, audiences fabric/powerbi/storage/azure). Complementa a `fabric-sql-mcp` (solo T-SQL).

Usa la sesión de `fab auth login` del usuario de Windows: los permisos son los de esa cuenta.

## Login (una vez, en tu terminal)

```powershell
fab auth logout
fab auth login     # elegir "Interactive with a web browser"
fab auth status    # Logged In: True, con tokens Fabric/PowerBI, Storage y Azure
```

## Tools (26)

auth_status, ls, get, exists, find, desc_element, fab_help, mkdir, set_property, cp, mv, rm,
assign, start_stop, export_item, import_item, job_run, job_status, job_cancel, job_schedule,
table, acl_list, acl_set, acl_remove, api, fab_command (cualquier comando fab).

`rm`, `acl_remove`, borrar schedules y comandos destructivos vía `fab_command` exigen `confirm=true`.

## Variables

`FAB_PATH`, `FAB_MCP_TIMEOUT` (300 s), `FAB_MCP_MAX_CHARS` (80000),
`FAB_MCP_EXPORT_DIR` (`C:\DVB\exports\fabric-admin`).

## Pruebas

```powershell
$env:PYTHONPATH="C:\DVB\mcp\fabric-admin-mcp\src"
C:\DVB\python\python.exe -m fabric_admin_mcp --status
C:\DVB\python\python.exe -m fabric_admin_mcp --handshake
```

Registro en Claude Code: `registrar.ps1`.
