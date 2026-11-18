# Operations Guide — windows-admin-mcp

Day-to-day procedures for deploying, operating, and expanding the Windows Admin MCP. Documentation is in English; placeholders are safe for Spanish-speaking teams (`SERVER-EJEMPLO-01`, `CHANGE-000000`).

## Table of contents

1. [Pilot server enablement](#pilot-server-enablement)
2. [Credential loading](#credential-loading)
3. [Completing the 20-server allowlist](#completing-the-20-server-allowlist)
4. [MCP start, stop, and diagnostics](#mcp-start-stop-and-diagnostics)
5. [Health query example](#health-query-example)
6. [Simulated reboot example](#simulated-reboot-example-dryruntrue)
7. [Real reboot example](#real-reboot-example-admin-operation)
8. [Log rotation](#log-rotation)
9. [Troubleshooting](#troubleshooting)

---

## Pilot server enablement

Enable remote administration on **one pilot server** before rolling out to the full fleet.

### Prerequisites (admin workstation)

```powershell
cd C:\DVB\mcp\windows-admin-mcp
.\scripts\Test-Prerequisites.ps1 -Verbose
```

Confirm:

- PowerShell 7+ available
- Node.js runtime matches `package.json`
- `config/servers.json` contains exactly **one** pilot entry
- Credentials are loaded (see [Credential loading](#credential-loading))
- You can resolve the pilot FQDN (`Resolve-DnsName server-pilot-01.example.local`)

### Step 1 — Add pilot to allowlist

Edit `config/servers.json`:

```json
{
  "name": "SERVER-PILOT-01",
  "address": "server-pilot-01.example.local",
  "transport": "auto",
  "port": null,
  "allowReboot": false,
  "protectedRole": false,
  "criticalServices": ["Spooler", "W32Time"],
  "tags": ["pilot", "non-production"]
}
```

Validate:

```powershell
npm run validate
# or
node dist/scripts/validate-server.js --config config/servers.json
```

### Step 2 — Choose transport (pilot target)

**Preferred: PowerShell over SSH**

On the **pilot server** (as Administrator):

```powershell
# Copy script from repo or approved share
.\Prepare-PSSessionOverSSH.ps1 -WhatIf   # review changes
.\Prepare-PSSessionOverSSH.ps1 -Confirm:$false
```

On the **admin workstation**:

```powershell
# Optional: register SSH host key after first connect policy review
ssh -o StrictHostKeyChecking=accept-new server-pilot-01.example.local
Enter-PSSession -HostName server-pilot-01.example.local -SSHTransport
```

**Alternative: WinRM HTTPS** (only if SSH is not viable)

On the **pilot server**:

```powershell
.\Prepare-WinRM-Https.ps1 -CertificateThumbprint "<THUMBPRINT_FROM_CA>" -WhatIf
.\Prepare-WinRM-Https.ps1 -CertificateThumbprint "<THUMBPRINT_FROM_CA>" -Confirm:$false
```

Verify listener (HTTPS only, port 5986):

```powershell
Get-WSManInstance -ResourceURI winrm/config/Listener -Enumerate
Test-NetConnection server-pilot-01.example.local -Port 5986
```

> Do **not** enable HTTP (5985), Basic auth, or `TrustedHosts *`. See [SECURITY.md](./SECURITY.md).

### Step 3 — Register MCP in Cursor

Merge `cursor-mcp.example.json` into your Cursor MCP config. Reload Cursor.

### Step 4 — Read-only validation

From Cursor, run in order:

1. `list_managed_servers` — pilot appears once
2. `test_server_connection` with `"server": "SERVER-PILOT-01"`
3. `get_server_health` (see [example below](#health-query-example))
4. `get_disk_usage`, `get_service_status`, `get_recent_system_events`

All must succeed before any write operation.

### Step 5 — Simulated reboot only

During pilot, keep `allowReboot: false`. Invoke `restart_server` with `dryRun: true` only ([example](#simulated-reboot-example-dryruntrue)).

### Step 6 — Promote pilot

After stable read-only operation:

- Document transport chosen and firewall rules in your change record
- Clone pilot pattern to the next server
- Enable `allowReboot: true` only on hosts approved for maintenance reboots

Rollback if prep fails: [ROLLBACK.md](./ROLLBACK.md).

---

## Credential loading

Credentials must **never** appear in `servers.json`, source code, MCP config committed to git, or audit logs.

Supported modes (`WIN_ADMIN_MCP_CREDENTIAL_MODE`):

| Mode | Best for | Per-server naming |
| --- | --- | --- |
| `credman` | Interactive admin workstation | See CredMan section |
| `secretmanagement` | Team vault / automation | See SecretManagement section |
| `env` | CI or Cursor `env` block with OS-protected vars | See Environment section |

Default prefix: `WIN_ADMIN_MCP_CRED_PREFIX` (example: `WinAdminMCP/`).

### Option A — Windows Credential Manager (CredMan)

Store **generic** credentials per server:

```powershell
# Pattern: WinAdminMCP/<SERVER-NAME>
cmdkey /generic:"WinAdminMCP/SERVER-PILOT-01" /user:"EXAMPLE\svc-winadmin" /pass:"<SET_VIA_SECURE_PROMPT>"
```

Or interactively (preferred — password not echoed in shell history):

```powershell
$cred = Get-Credential -Message "CredMan entry for SERVER-PILOT-01"
# Use scripts/Set-Credential.ps1 if provided:
.\scripts\Set-Credential.ps1 -ServerName "SERVER-PILOT-01" -Credential $cred -Store CredMan
```

Set MCP mode:

```powershell
$env:WIN_ADMIN_MCP_CREDENTIAL_MODE = "credman"
```

Verify (does not print password):

```powershell
.\scripts\Test-Credentials.ps1 -ServerName "SERVER-PILOT-01"
```

### Option B — PowerShell SecretManagement

Register a vault (example module: `SecretManagement.KeePass` or corporate module):

```powershell
Install-Module Microsoft.PowerShell.SecretManagement -Scope CurrentUser
Register-SecretVault -Name CorporateVault -ModuleName <YourVaultModule> -VaultParameters @{ ... }

Set-Secret -Name "WinAdminMCP/SERVER-PILOT-01" -Secret (Get-Credential) -Vault CorporateVault
```

Set MCP mode:

```powershell
$env:WIN_ADMIN_MCP_CREDENTIAL_MODE = "secretmanagement"
$env:WIN_ADMIN_MCP_SECRET_VAULT = "CorporateVault"
```

List secret **names** only (never values):

```powershell
Get-SecretInfo -Vault CorporateVault | Where-Object Name -like "WinAdminMCP/*"
```

### Option C — Environment variables

Use for Cursor MCP `env` block or session-scoped vars. **Not for production secrets in plain `.env` files on shared machines.**

Naming convention:

```text
WIN_ADMIN_MCP_USER_SERVER_PILOT_01=EXAMPLE\svc-winadmin
WIN_ADMIN_MCP_PASS_SERVER_PILOT_01=<from-secure-store>
```

Rules:

- Derive env suffix from `name` in `servers.json`: uppercase, non-alphanumeric → `_`
- Set variables in Cursor MCP config or Windows User/System environment — not in git
- MCP redacts these values in logs

Example Cursor fragment:

```json
"env": {
  "WIN_ADMIN_MCP_CREDENTIAL_MODE": "env",
  "WIN_ADMIN_MCP_USER_SERVER_PILOT_01": "EXAMPLE\\svc-winadmin",
  "WIN_ADMIN_MCP_PASS_SERVER_PILOT_01": "<from-vault>"
}
```

### Credential rotation

1. Add/update secret in CredMan or vault
2. Run `test_server_connection` for affected servers
3. Revoke old password in AD/local account policy
4. No MCP restart required if env vars unchanged; restart Cursor if MCP env changed

---

## Completing the 20-server allowlist

Expand incrementally after pilot success. Target: ~20 entries in `config/servers.json`.

### Recommended batch plan

| Phase | Servers | `allowReboot` | Activities |
| --- | --- | --- | --- |
| Pilot | 1 | `false` | Transport prep, read-only tools, `dryRun` reboot test |
| Wave 1 | 2–5 | `false` | Non-prod app servers, same transport template |
| Wave 2 | 6–15 | `false` | Production read-only monitoring |
| Wave 3 | subset | `true` only where approved | Maintenance hosts with change control |

### Per-server checklist

For each new server `SERVER-EXAMPLE-NN`:

- [ ] Change ticket / owner approval recorded
- [ ] DNS forward and reverse resolution verified
- [ ] Entry added to `config/servers.json` with correct `tags`, `protectedRole`, `criticalServices`
- [ ] Credentials stored (`WinAdminMCP/SERVER-EXAMPLE-NN`)
- [ ] Transport prepared (SSH or WinRM HTTPS) using pilot scripts
- [ ] Firewall rule scoped to admin jump host / MCP workstation subnet only
- [ ] `npm run validate` passes
- [ ] `test_server_connection` succeeds from Cursor
- [ ] `get_server_health` baseline captured for comparison
- [ ] Rollback script path documented in [ROLLBACK.md](./ROLLBACK.md)

### Bulk editing tips

1. Maintain a **CSV working copy** (not executed by MCP directly) with columns: `name,address,transport,tags,allowReboot,protectedRole,criticalServices`
2. Convert approved rows to JSON (script `scripts/Import-ServersFromCsv.ps1` if available)
3. Never paste passwords into CSV
4. Run schema validation before merge

Example template row:

```csv
name,address,transport,tags,allowReboot,protectedRole,criticalServices
SERVER-EXAMPLE-02,server-example-02.example.local,auto,"app;prod",false,false,"W3SVC;W32Time"
```

### Protected servers

Keep defaults for sensitive roles:

```json
{
  "name": "SERVER-DC-EXAMPLE-01",
  "address": "server-dc-example-01.example.local",
  "allowReboot": false,
  "protectedRole": true,
  "tags": ["domain-controller", "protected"]
}
```

`restart_server` refuses protected entries unless explicitly overridden in config with documented exception approval.

---

## MCP start, stop, and diagnostics

### Start (manual)

Cursor normally launches the MCP via stdio. To run interactively for debugging:

```powershell
cd C:\DVB\mcp\windows-admin-mcp
$env:WIN_ADMIN_MCP_CONFIG = "C:\DVB\mcp\windows-admin-mcp\config\servers.json"
npm start
# equivalent: node dist/src/index.js
```

Successful start: process stays attached, no stack trace, stderr shows `MCP server listening` (exact message may vary by version).

### Stop

- **Cursor-managed:** Disable or remove the `windows-admin` entry in MCP settings, then reload Cursor
- **Manual process:** `Ctrl+C` in the terminal, or:

```powershell
Get-Process node -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "*windows-admin-mcp*" } |
  Stop-Process -Force
```

### Diagnostics

```powershell
# Config + schema + credential presence (no secret values)
npm run doctor

# Validate servers.json
node dist/scripts/validate-server.js --config config\servers.json

# Workstation prerequisites
.\scripts\Test-Prerequisites.ps1 -Verbose

# Test one server outside Cursor
.\scripts\Test-RemoteConnection.ps1 -ServerName "SERVER-PILOT-01"

# MCP tool smoke test (if script present)
.\scripts\Invoke-McpSmokeTest.ps1
```

### Cursor-side verification

1. Open MCP panel → `windows-admin` → status **Connected**
2. Tool list shows 12 tools (names per README)
3. Invoke `list_managed_servers` — count matches `servers.json`

### Reload after config change

| Change | Action |
| --- | --- |
| `servers.json` | Reload Cursor or restart MCP |
| Credential store | Usually no restart; run `test_server_connection` |
| MCP code / `npm run build` | Reload Cursor |
| Cursor `mcp.json` env | Reload Cursor |

---

## Health query example

**Tool:** `get_server_health`  
**Risk:** Read-only  
**Server:** pilot placeholder

### MCP parameters

```json
{
  "server": "SERVER-PILOT-01"
}
```

### Expected response shape (illustrative)

```json
{
  "server": "SERVER-PILOT-01",
  "address": "server-pilot-01.example.local",
  "transport": "ssh",
  "reachable": true,
  "timestampUtc": "2026-07-19T04:30:00Z",
  "uptime": {
    "bootTimeUtc": "2026-07-10T12:00:00Z",
    "uptimeSeconds": 756000
  },
  "diskSummary": {
    "volumesOk": true,
    "lowSpaceVolumes": []
  },
  "criticalServices": [
    { "name": "W32Time", "status": "Running" }
  ],
  "pendingReboot": false,
  "correlationId": "550e8400-e29b-41d4-a716-446655440000"
}
```

### PowerShell equivalent (manual verification)

```powershell
Invoke-Command -ComputerName server-pilot-01.example.local -ScriptBlock {
  [PSCustomObject]@{
    Hostname = $env:COMPUTERNAME
    Uptime   = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    Disks    = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" |
               Select-Object DeviceID, @{N='FreeGB';E={[math]::Round($_.FreeSpace/1GB,2)}}
  }
}
```

Audit log entry is written to `logs/audit-YYYYMMDD.jsonl` without credential material.

---

## Simulated reboot example (`dryRun=true`)

**Tool:** `restart_server`  
**Risk:** None — no restart performed  
**Default:** `dryRun` defaults to `true` if omitted

### MCP parameters

```json
{
  "server": "SERVER-PILOT-01",
  "reason": "Pilot validation — confirm guards and audit trail without downtime",
  "dryRun": true,
  "waitForRecovery": false
}
```

### Expected behavior

1. Validates server is in allowlist
2. Checks `allowReboot` (may be `false` — dry run still records intent without rebooting)
3. Confirms target is not localhost
4. Writes audit log with `"dryRun": true`
5. Returns planned actions **without** calling `Restart-Computer`

### Illustrative response

```json
{
  "server": "SERVER-PILOT-01",
  "dryRun": true,
  "wouldReboot": false,
  "blockedReason": "allowReboot is false",
  "reason": "Pilot validation — confirm guards and audit trail without downtime",
  "correlationId": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "message": "Dry run complete — no restart command sent"
}
```

Use this for **every** initial deployment and after config changes.

---

## Real reboot example (ADMIN OPERATION)

> ### ⚠ ADMIN OPERATION — PRODUCTION DOWNTIME
>
> This **will restart** the target server when all guards pass. Requires:
>
> - Approved change record (e.g. `CHANGE-000000`)
> - `allowReboot: true` for the server in `config/servers.json`
> - Explicit `"dryRun": false`
> - Non-protected role (or documented exception)
> - Stakeholder notification completed
>
> **Never** run against domain controllers, cluster nodes, or localhost unless explicitly authorized in writing.

### Pre-flight checklist

- [ ] Maintenance window active
- [ ] `get_pending_reboot_status` reviewed
- [ ] `get_service_status` for `criticalServices` recorded
- [ ] `allowReboot: true` in config
- [ ] Simulated run completed successfully same day
- [ ] On-call engineer available

### MCP parameters

```json
{
  "server": "SERVER-MAINT-02",
  "reason": "Post-patch reboot — CHANGE-000000 approved 2026-07-19",
  "dryRun": false,
  "waitForRecovery": true,
  "force": false
}
```

### Expected sequence

1. Guard checks (allowlist, `allowReboot`, not local, not protected)
2. Audit log: `"action": "restart_server"`, `"dryRun": false`, reason, correlation ID
3. `Restart-Computer` (or equivalent) via selected transport
4. Wait for controlled shutdown (timeout: `rebootTimeoutSeconds`)
5. `wait_for_server_online` polling
6. Post-check: `test_server_connection`, `get_service_status` for critical services
7. Structured JSON result with `"success": true|false`

### Illustrative success response

```json
{
  "server": "SERVER-MAINT-02",
  "dryRun": false,
  "rebootInitiated": true,
  "rebootCompletedUtc": "2026-07-19T05:15:00Z",
  "recoveryVerified": true,
  "criticalServicesHealthy": true,
  "durationSeconds": 420,
  "correlationId": "7c9e6679-7425-40de-944b-e07fc1f90ae7"
}
```

On recovery failure: **stop batch operations**, escalate, do not proceed to next server.

---

## Log rotation

Audit logs live in `WIN_ADMIN_MCP_LOG_DIR` (default: `logs/`). Files are typically `audit-YYYYMMDD.jsonl`.

### Recommended policy

| Setting | Recommendation |
| --- | --- |
| Retention | 90 days hot, 1 year archive |
| Max file size | 50 MB per daily file |
| Rotation | Daily filename + size-based split if needed |
| Compression | Gzip archives older than 7 days |
| Permissions | Administrators and security audit group only |

### Option 1 — Scheduled Task + PowerShell

Create `scripts/Rotate-AuditLogs.ps1` task weekly:

```powershell
$LogDir = "C:\DVB\mcp\windows-admin-mcp\logs"
$Archive = Join-Path $LogDir "archive"
New-Item -ItemType Directory -Force -Path $Archive | Out-Null

Get-ChildItem $LogDir -Filter "audit-*.jsonl" |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
  ForEach-Object {
    $zip = Join-Path $Archive ($_.BaseName + ".zip")
    Compress-Archive -Path $_.FullName -DestinationPath $zip -Force
    Remove-Item $_.FullName
  }

Get-ChildItem $Archive -Filter "*.zip" |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-365) } |
  Remove-Item
```

### Option 2 — Windows Event Forwarding / SIEM

Forward JSON lines to corporate SIEM (Splunk, Sentinel, etc.) and treat local files as buffer only.

### Option 3 — `logrotate` on WSL jump host

If logs are synced to Linux for analysis, use `logrotate.d/windows-admin-mcp` with `copytruncate` or move+compress.

### What not to log

Rotate and archive assuming logs may contain hostnames and usernames but **never** passwords, ticket-granting tokens, or private keys. See [SECURITY.md](./SECURITY.md#audit-logging).

---

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Host rejected | Not in allowlist | Add to `servers.json`, validate schema |
| Certificate error | TLS mismatch, expired cert | Renew cert; never disable `verifyTls` |
| Access denied | Wrong credential or insufficient rights | Fix CredMan/vault entry; verify group membership |
| WinRM fails | HTTP listener, firewall | Use HTTPS 5986 only; see pilot prep |
| SSH fails | OpenSSH service stopped | Run SSH prep script on target |
| Reboot blocked | `allowReboot: false` or `protectedRole` | Expected — adjust config with approval |
| MCP not in Cursor | Config path wrong | Verify absolute paths, reload Cursor |

For undoing target-side changes: [ROLLBACK.md](./ROLLBACK.md).
