# WinRM and PSSession

## Transport stack

windows-guarded-admin-mcp does **not** use the Python `pywinrm` library. All remote communication goes through **PowerShell 5.1 remoting**:

| Mechanism | Default | Config keys |
|-----------|---------|-------------|
| WinRM WS-Man | Port 5985 (HTTP) | `connection.port`, `connection.use_ssl` |
| PSSession | Enabled | `connection.use_pssession: true` |
| SMB fallback | Enabled | `connection.allow_smb_fallback: true` |

HTTPS WinRM uses port 5986 when `use_ssl: true`.

## Connection flow

1. Local `powershell.exe` invoked by `powershell_runner.py`
2. Dispatcher script (e.g. `Test-McpServerAccess.ps1`) runs `Test-WSMan`, `New-PSSession`
3. Authentication: `-Authentication Default` (integrated Windows identity)
4. Remote commands executed in session; JSON returned on stdout

## File transfer

For authorized scripts:

1. **Primary:** PSSession `Copy-Item` to remote staging (`GlobalDefaults.staging_root`)
2. **Fallback:** SMB admin share `\\HOST\C$\...` when PSSession copy fails

Implemented in `execution/pssession_transfer.py` and `execution/smb_fallback.py`.

## Firewall ports

Approved diagnostic TCP ports (policies): **5985, 5986, 445, 135**

| Port | Service |
|------|---------|
| 5985 | WinRM HTTP |
| 5986 | WinRM HTTPS |
| 445 | SMB (fallback transfer) |
| 135 | RPC endpoint mapper |

## Prerequisites on target server

- WinRM service running (`winrm quickconfig` or GPO)
- Firewall rule for WinRM
- Operator account in local Administrators or WinRM remote management group
- Kerberos/NTLM connectivity from operator workstation

## Diagnostics

Use MCP tool `diagnose_server_access` or:

```powershell
scripts\diagnose_environment.ps1 -RemoteServer <hostname>
```

Checks: DNS, port probes, `Test-WSMan`, PSSession smoke, SMB C$ read/write test.

## Limitations

- No SSH transport in v0.1.0 (unlike TypeScript windows-psremoting-mcp pilot)
- No certificate pinning for HTTPS WinRM in v0.1.0
- Single-hop remoting only (no double-hop/credential delegation)

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for access denied, TrustedHosts, and double-hop issues.
