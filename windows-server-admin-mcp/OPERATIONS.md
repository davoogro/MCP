# Operations

Day-to-day runbook for windows-server-admin-mcp.

## Installation

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\DVB\mcp\windows-server-admin-mcp\scripts\install.ps1
```

Install script:

1. Backs up `%USERPROFILE%\.cursor\mcp.json`
2. Runs `uv sync` or falls back to `C:\DVB\python\python.exe -m pip install -e`
3. Writes `scripts/launch.ps1` (gitignored, machine-specific)
4. Registers `windows-server-admin` MCP entry (STDIO via PowerShell launcher)
5. Runs `validate_mcp.ps1`

## Update

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\update.ps1
```

## Uninstall

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

Removes MCP entry; does not delete local config or audit logs.

## Environment diagnosis

Non-destructive local + remote checks:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\diagnose_environment.ps1 -RemoteServer integration-server-01
```

Override `-RemoteServer` with hostname from your `servers.local.yaml`. Output: `reports/environment_diagnostic.json` (gitignored).

## Adding a server

1. Copy `config/servers.local.yaml.example` → `config/servers.local.yaml`
2. Add entry with `authentication: current_windows_identity`, port 5985
3. Set permissions conservatively (`restart_*: false` for read-only pilots)
4. If using authorized scripts, add server ID to `config/scripts.local.yaml` overlay
5. Validate: `diagnose_server_access`, `test_remote_execution`

See [docs/ADDING_SERVERS.md](docs/ADDING_SERVERS.md).

## Read-only health check workflow

1. `list_servers` — confirm server appears with expected permissions
2. `diagnose_server_access` — WinRM, PSSession, ports
3. `get_server_health` + `get_disks` — capacity planning
4. `get_services` — critical service state
5. `get_windows_events` — recent errors (limit `max_events`)

## Authorized script workflow

1. Add `.ps1` under `authorized_scripts/`
2. Register in `config/scripts.yaml` with `sha256: auto` or pinned hash
3. Set `allowed_servers` list (use `scripts.local.yaml` for real hostnames)
4. Call `run_authorized_script` with validated parameters

## Planned restart workflow

Only when `restart_server: true` on target (never on integration hosts):

1. `prepare_server_restart` → receive one-time token
2. `confirm_server_restart` with same reason/delay parameters
3. `wait_for_server` after maintenance window
4. `cancel_server_restart` if plans change

See [docs/RESTART_WORKFLOW.md](docs/RESTART_WORKFLOW.md).

## Audit log

Default location: `logs/audit/` (gitignored). Query via MCP tool `query_audit_log`.

## Test matrix

| Suite | Command | When |
|-------|---------|------|
| Unit + security | `pytest tests/unit tests/security` | Every change |
| Integration | `WSA_MCP_INTEGRATION=1 pytest tests/integration` | After config/network change |
| MCP validate | `scripts/validate_mcp.ps1` | After install |
| Python smoke | `python authorized_scripts/smoke_test.py` | Local package sanity |
| PS smoke | `powershell -File authorized_scripts/smoke_test.ps1` | Local PS sanity |

## Rollback

- Restore MCP config from `backups/cursor-mcp/mcp.json.bak-*`
- Run `scripts/uninstall.ps1`
- No remote changes are made by install/diagnose scripts
