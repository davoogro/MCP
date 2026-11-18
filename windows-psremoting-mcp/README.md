# windows-psremoting-mcp

Local [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for **controlled Windows Server administration** from Cursor. Supports read-only health checks, approved diagnostics, and guarded reboot operations against an explicit server allowlist (~20 hosts).

> **Spanish-friendly:** Operational terms and examples use placeholders (`SERVER-EXAMPLE-01`, `tu.usuario@empresa.com`). See linked docs for step-by-step procedures in English with Spanish notes where helpful.

## Purpose

This MCP lets Cursor (or any MCP host) remotely:

- List and test allowlisted Windows Server targets
- Query uptime, disk, services, events, and pending reboot status
- Run a **closed set** of approved diagnostics (no arbitrary PowerShell)
- Restart services or servers only when configuration and tool parameters explicitly permit it

Connectivity prefers **PowerShell over SSH**, then **WinRM HTTPS**. The server **fails closed**: unknown hosts, missing credentials, invalid certificates, or disabled reboot flags block the action.

## Requirements

- Windows 10/11 or Windows Server workstation running Cursor
- Node.js 20+ (or runtime documented in `package.json`)
- Network access to target servers on approved ports (SSH 22, WinRM HTTPS 5986)
- Administrative credentials stored outside the repository (see [SECURITY.md](./SECURITY.md))
- Completed `config/servers.json` allowlist (from `config/servers.example.json`)

## Quick start

```powershell
cd C:\DVB\mcp\windows-psremoting-mcp
npm install
Copy-Item config\servers.example.json config\servers.json
Copy-Item .env.example .env
npm run build
npm run validate
```

1. Edit `config/servers.json` — start with **one pilot server** only.
2. Load credentials via Credential Manager, SecretManagement, or environment variables ([OPERATIONS.md](./OPERATIONS.md#credential-loading)).
3. Register the MCP in Cursor (see [Cursor registration](#cursor-registration)).
4. Run read-only tests (`test_server_connection`, `get_server_health`) before any write or reboot action.

## Project layout

| Path | Description |
| --- | --- |
| `src/` | MCP server, transport layer, tools, validation, audit logging |
| `config/` | `servers.json` allowlist, JSON Schema, timeouts and concurrency |
| `scripts/` | Prerequisites, SSH/WinRM prep, MCP install, rollback, smoke tests |
| `tests/` | Unit and integration tests |
| `logs/` | Structured audit logs (gitignored) |
| `OPERATIONS.md` | Day-to-day runbook |
| `SECURITY.md` | Threat model, secrets, and controls |
| `ROLLBACK.md` | Undo SSH and WinRM preparation |

## MCP tools

| Tool | Type | Description |
| --- | --- | --- |
| `list_managed_servers` | Read | Enumerate allowlisted servers and tags |
| `test_server_connection` | Read | Probe transport and authentication |
| `get_server_health` | Read | Summary health snapshot |
| `get_server_uptime` | Read | Boot time and uptime |
| `get_disk_usage` | Read | Volume free/used space |
| `get_service_status` | Read | Status of named services |
| `get_recent_system_events` | Read | Filtered System/Application events |
| `get_pending_reboot_status` | Read | Pending reboot indicators |
| `run_approved_diagnostic` | Read | Closed-list diagnostic operations |
| `restart_service` | Write | Restart an allowlisted service |
| `restart_server` | **Admin** | Reboot server (`dryRun` default `true`) |
| `wait_for_server_online` | Read | Poll until host responds |

There is **no** public `execute_any_powershell` tool.

## Configuration

### Server allowlist (`config/servers.json`)

Copy from `config/servers.example.json`. Each entry requires at least:

- `name` — logical alias (e.g. `SERVER-PILOT-01`)
- `address` — FQDN or resolvable hostname
- `transport` — `auto`, `ssh`, or `winrm`
- `allowReboot` — default `false`; must be `true` for real reboots
- `protectedRole` — default `true` for DCs, cluster nodes, etc.

Global defaults include `connectionTimeoutSeconds`, `operationTimeoutSeconds`, `rebootTimeoutSeconds`, `maxConcurrency`, and `verifyTls`.

### Environment variables

| Variable | Description |
| --- | --- |
| `WINMCP_CONFIG` | Path to `servers.json` (default: `<package>/config/servers.json`; relative paths resolve against the package folder, never the cwd) |
| `WINMCP_AUDIT_DIR` | Audit log directory (default: `<package>/logs/audit`) |
| `WINMCP_POWERSHELL` | PowerShell used for the helper scripts (default: `pwsh` if on PATH, else Windows PowerShell 5.1) |
| `WINMCP_LOCAL_HOSTNAME` / `WINMCP_LOCAL_FQDN` | Extra names treated as "this machine" by the localhost guard |
| per-server `usernameEnv` / `passwordEnv` / `credentialTargetEnv` | Credential references named in `servers.json` |

An optional `.env` next to `package.json` is loaded (existing env vars win). See `.env.example`. **Never commit real secrets.**

## Claude Code registration

Claude Code stdio servers take only `command`, `args` and `env` (no cwd); the server resolves its
config, audit dir and helper scripts from its own location, so it starts from any cwd:

```powershell
claude mcp add windows-psremoting --scope user `
  -e WINMCP_CONFIG=C:\DVB\mcp\windows-psremoting-mcp\config\servers.json `
  -e WINMCP_AUDIT_DIR=C:\DVB\mcp\windows-psremoting-mcp\logs\audit `
  -- "C:\Program Files\nodejs\node.exe" C:\DVB\mcp\windows-psremoting-mcp\dist\src\index.js
```

Tools carry MCP annotations (`readOnlyHint` for reads, `destructiveHint` for `restart_service` /
`restart_server`) and the server sends instructions describing the dry-run / allowlist / audit model.

## Cursor registration

Add to your MCP config (merge with existing servers; do not remove others):

**User-level:** `%USERPROFILE%\.cursor\mcp.json`  
**Project-level:** `.cursor/mcp.json` in the workspace

Example entry (adjust paths):

```json
{
  "mcpServers": {
    "windows-psremoting": {
      "command": "node",
      "args": ["C:\\DVB\\mcp\\windows-psremoting-mcp\\dist\\src\\index.js"],
      "env": {
        "WIN_ADMIN_MCP_CONFIG": "C:\\DVB\\mcp\\windows-psremoting-mcp\\config\\servers.json",
        "WIN_ADMIN_MCP_CREDENTIAL_MODE": "credman",
        "WIN_ADMIN_MCP_LOG_DIR": "C:\\DVB\\mcp\\windows-psremoting-mcp\\logs"
      }
    }
  }
}
```

After editing:

1. Save a backup of the previous MCP config.
2. Reload Cursor (**Developer: Reload Window**) or restart Cursor.
3. Confirm the server appears under MCP settings and tools enumerate.

A template is also provided at `cursor-mcp.example.json`.

## Documentation map

| Document | Use when |
| --- | --- |
| [OPERATIONS.md](./OPERATIONS.md) | Enabling pilot servers, credentials, allowlist rollout, start/stop, examples |
| [SECURITY.md](./SECURITY.md) | Auth, TLS, audit, reboot guards, compliance |
| [ROLLBACK.md](./ROLLBACK.md) | Reverting SSH or WinRM prep on targets |
| [TEST-RESULTS.md](./TEST-RESULTS.md) | Latest validation run (when populated) |

## Common commands

```powershell
# Build and validate
npm run build
npm run validate

# Start MCP manually (stdio; Cursor normally starts this)
npm start

# Diagnostics
npm run doctor
node dist/scripts/validate-server.js

# Run tests
npm test
```

Full operational detail: [OPERATIONS.md](./OPERATIONS.md).

## Examples

### Health query (read-only)

In Cursor, invoke the `get_server_health` tool:

```json
{
  "server": "SERVER-PILOT-01"
}
```

Expected: structured JSON with connectivity, uptime, disk summary, critical service states, and pending reboot flag — no secrets in the response.

### Simulated reboot (default safe mode)

```json
{
  "server": "SERVER-PILOT-01",
  "reason": "Validating reboot workflow during pilot — no restart performed",
  "dryRun": true
}
```

### Real reboot (admin operation)

> **ADMIN OPERATION — causes downtime.** Requires `allowReboot: true` in `servers.json` and explicit `dryRun: false`.

```json
{
  "server": "SERVER-MAINT-02",
  "reason": "Post-patch reboot approved in CHANGE-000000",
  "dryRun": false,
  "waitForRecovery": true
}
```

See [OPERATIONS.md](./OPERATIONS.md) for prerequisites and change-control checklist.

## Pilot-first rollout

Do **not** enable remoting on all ~20 servers at once.

1. Add one pilot entry to `config/servers.json`.
2. Run `scripts\Test-Prerequisites.ps1` on the admin workstation.
3. Prepare connectivity on the pilot only (`scripts/Prepare-PSSessionOverSSH.ps1` or `scripts/Prepare-WinRM-Https.ps1`).
4. Validate read-only MCP tools.
5. Test `restart_server` with `dryRun: true` only.
6. Expand the allowlist incrementally ([OPERATIONS.md](./OPERATIONS.md#completing-the-20-server-allowlist)).

## Logging and audit

Structured JSON audit logs are written to `logs/` (configurable). Logs include tool name, target, duration, result, and sanitized errors — **never passwords or tokens**.

Configure log rotation: [OPERATIONS.md](./OPERATIONS.md#log-rotation).

## License and support

Internal infrastructure tool. Maintain allowlist and credentials according to organizational policy. For rollback of target-side changes, see [ROLLBACK.md](./ROLLBACK.md).
