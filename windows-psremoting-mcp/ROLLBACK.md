# Rollback Guide — windows-psremoting-mcp

Procedures to **undo** changes made by the PowerShell preparation scripts on target Windows Servers. Use this when pilot enablement fails, certificates are misissued, firewall rules are too broad, or you decommission MCP-managed remoting.

> **Important:** Rollback scripts revert **connectivity preparation** on servers. They do **not** remove MCP from Cursor, delete allowlist entries, or revoke CredMan secrets — handle those separately.

## Before you rollback

1. **Identify scope** — Which servers received `Prepare-PSSessionOverSSH.ps1` and/or `Prepare-WinRM-Https.ps1`?
2. **Capture state** — Export current config if you may need to re-apply later:

   ```powershell
   # On target server — save to admin share, not git
   Get-Service sshd, WinRM -ErrorAction SilentlyContinue | Export-Clixml C:\Temp\pre-rollback-services.xml
   Get-WSManInstance -ResourceURI winrm/config/Listener -Enumerate -ErrorAction SilentlyContinue |
     Export-Clixml C:\Temp\pre-rollback-winrm-listeners.xml
   ```

3. **Maintenance window** — Rollback may disconnect active remoting sessions.
4. **Change record** — Document server name, script version, operator, reason.
5. **MCP side** — Remove or disable the server in `config/servers.json` and reload Cursor so the MCP does not retry failed hosts during rollback.

## Rollback scripts

| Script | Undoes |
| --- | --- |
| `scripts/Rollback-PSSessionOverSSH.ps1` | OpenSSH Server config, PowerShell subsystem, related firewall rules **added by prep** |
| `scripts/Rollback-WinRM-Https.ps1` | HTTPS WinRM listener, prep firewall rules, optional service disable |

Both support:

```powershell
.\Rollback-<Script>.ps1 -WhatIf    # show planned changes
.\Rollback-<Script>.ps1 -Verbose   # execute with detail
```

Always run `-WhatIf` first on production-adjacent systems.

---

## Rollback: PowerShell over SSH prep

**Script:** `scripts/Rollback-PSSessionOverSSH.ps1`  
**Run on:** Each target server where `Prepare-PSSessionOverSSH.ps1` was executed  
**Requires:** Local Administrator

### What the prep script typically changed

- Installed/enabled `OpenSSH.Server` capability
- Configured `sshd_config` (port, subsystem for PowerShell)
- Created or modified firewall rule for TCP 22 (scoped to admin subnet if prep was followed)
- Set `sshd` service to Automatic

### Rollback procedure

```powershell
# 1. Copy script to server (or run from admin share)
Set-Location C:\DVB\mcp\windows-psremoting-mcp\scripts

# 2. Preview
.\Rollback-PSSessionOverSSH.ps1 -WhatIf

# 3. Execute
.\Rollback-PSSessionOverSSH.ps1 -Confirm:$false
```

### Expected actions (default behavior)

| Step | Action |
| --- | --- |
| 1 | Stop `sshd` service if running |
| 2 | Remove firewall rule created by prep (name pattern documented in script header, e.g. `WinAdminMCP-SSH-In`) |
| 3 | Restore `sshd_config` from backup if prep created `sshd_config.bak-winadmin-*` |
| 4 | Optionally disable `sshd` startup type (parameter `-DisableService`) |
| 5 | Optionally uninstall OpenSSH Server feature (parameter `-RemoveCapability` — use only if OpenSSH was installed solely for MCP) |

### Parameters (if supported by script)

```powershell
.\Rollback-PSSessionOverSSH.ps1 `
  -DisableService `
  -RemoveFirewallRuleOnly   # minimal: remove MCP rule only, leave OpenSSH for other uses
```

### Verification after SSH rollback

```powershell
Get-Service sshd
Get-NetFirewallRule -DisplayName "*WinAdminMCP*" -ErrorAction SilentlyContinue
Test-NetConnection -ComputerName localhost -Port 22
```

From admin workstation — should **fail** if rollback complete and no other SSH listener:

```powershell
Enter-PSSession -HostName server-pilot-01.example.local -SSHTransport
# Expected: connection refused or timeout
```

### Partial rollback (keep OpenSSH, remove MCP rule only)

If other teams use OpenSSH on the same server:

```powershell
.\Rollback-PSSessionOverSSH.ps1 -RemoveFirewallRuleOnly -WhatIf
.\Rollback-PSSessionOverSSH.ps1 -RemoveFirewallRuleOnly
```

Manually review `sshd_config` for PowerShell subsystem lines added by prep.

---

## Rollback: WinRM HTTPS prep

**Script:** `scripts/Rollback-WinRM-Https.ps1`  
**Run on:** Each target server where `Prepare-WinRM-Https.ps1` was executed  
**Requires:** Local Administrator

### What the prep script typically changed

- Enabled `WinRM` service
- Created **HTTPS** listener on port 5986 bound to certificate thumbprint
- Configured authentication (Kerberos/Negotiate — not Basic)
- Added firewall rule for TCP 5986 (admin-scoped)
- Possibly increased `MaxConcurrentOperationsPerUser` (documented in prep log)

### Rollback procedure

```powershell
Set-Location C:\DVB\mcp\windows-psremoting-mcp\scripts

# 1. Preview
.\Rollback-WinRM-Https.ps1 -WhatIf

# 2. Execute
.\Rollback-WinRM-Https.ps1 -Confirm:$false
```

### Expected actions (default behavior)

| Step | Action |
| --- | --- |
| 1 | Remove HTTPS listener matching prep thumbprint or listener name tag |
| 2 | Remove firewall rule created by prep (e.g. `WinAdminMCP-WinRM-HTTPS-In`) |
| 3 | Restore `winrm` custom settings from backup if prep exported config |
| 4 | If no listeners remain and WinRM was disabled before pilot: stop and disable `WinRM` service |
| 5 | Do **not** remove certificates from `LocalMachine\My` unless issued solely for MCP (optional `-RemoveCertificate` switch) |

### Parameters (if supported)

```powershell
.\Rollback-WinRM-Https.ps1 `
  -ListenerPort 5986 `
  -RemoveFirewallRuleOnly

# Nuclear option — only if WinRM was never used before pilot
.\Rollback-WinRM-Https.ps1 -DisableWinRmService
```

### Verification after WinRM rollback

```powershell
Get-Service WinRM
Get-WSManInstance -ResourceURI winrm/config/Listener -Enumerate
Get-NetFirewallRule -DisplayName "*WinAdminMCP*" -ErrorAction SilentlyContinue
Test-NetConnection -ComputerName localhost -Port 5986
```

From admin workstation:

```powershell
Test-WSMan -ComputerName server-pilot-01.example.local -UseSSL
# Expected: failure if listener removed
```

### Certificate handling

- **Do not delete** enterprise CA-issued certs still used by other services
- If prep used a dedicated cert, remove from store only with `-RemoveCertificate` and CA approval
- Document thumbprint in rollback change ticket

---

## Rollback both transports

If both scripts were applied to the same server (unusual — pick one transport in production):

```powershell
.\Rollback-WinRM-Https.ps1 -Confirm:$false
.\Rollback-PSSessionOverSSH.ps1 -Confirm:$false
```

Order: WinRM first if active sessions exist, then SSH — or reboot server if sessions hang.

---

## MCP and configuration cleanup (manual)

After target rollback, clean up the control plane:

### 1. Remove server from allowlist

Edit `config/servers.json` — delete or comment out the entry (JSON has no comments — remove the object).

```powershell
npm run validate
```

### 2. Remove credentials

**CredMan:**

```powershell
cmdkey /delete:"WinAdminMCP/SERVER-PILOT-01"
```

**SecretManagement:**

```powershell
Remove-Secret -Name "WinAdminMCP/SERVER-PILOT-01" -Vault CorporateVault
```

**Environment:** Remove `WIN_ADMIN_MCP_*` vars from Cursor MCP config.

### 3. Reload Cursor

Disable `windows-psremoting` MCP or reload window so tools no longer target decommissioned hosts.

### 4. Archive audit logs

Preserve logs for the pilot period before deletion per retention policy.

---

## Failure scenarios

| Situation | Action |
| --- | --- |
| Rollback script missing | Manually remove listener/firewall rule using tables above; restore from `*.bak-winadmin-*` |
| WinRM listener removal fails | `winrm delete winrm/config/Listener?Address=*+Transport=HTTPS` as admin; see Microsoft docs |
| SSH config syntax error after rollback | Restore `sshd_config.bak-winadmin-*`; restart `sshd` |
| Server unreachable | Console/iLO/KVM access; run rollback locally |
| Partial firewall removal | `Get-NetFirewallRule` audit; remove orphaned `WinAdminMCP*` rules |

---

## Re-applying after rollback

If pilot will retry:

1. Fix root cause (cert, DNS, firewall scope, credentials)
2. Re-run prep with `-WhatIf`
3. Re-add allowlist entry
4. Read-only MCP tests before `dryRun` reboot test

See [OPERATIONS.md](./OPERATIONS.md#pilot-server-enablement).

---

## Rollback checklist (per server)

- [ ] Change ticket updated
- [ ] `-WhatIf` output reviewed and saved
- [ ] Rollback script executed
- [ ] Listeners / firewall / service state verified
- [ ] Remote connection from MCP workstation fails (expected)
- [ ] Entry removed from `config/servers.json`
- [ ] Credentials revoked/removed
- [ ] Cursor MCP reloaded
- [ ] Audit logs archived

---

## Related documents

- [OPERATIONS.md](./OPERATIONS.md) — Pilot enablement (forward path)
- [SECURITY.md](./SECURITY.md) — Transport and credential requirements
- [README.md](./README.md) — Project overview
