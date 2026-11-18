# Troubleshooting

## MCP server won't start in Cursor

| Symptom | Cause | Fix |
|---------|-------|-----|
| MCP shows red/disconnected | Launcher path wrong | Re-run `scripts/install.ps1` |
| `ModuleNotFoundError` | Package not installed | `C:\DVB\python\python.exe -m pip install -e .` |
| Immediate exit code 1 | Config validation error | Check YAML syntax in `config/` |
| No tools listed | Cursor not reloaded | Restart Cursor completely |

Check launcher manually:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\DVB\mcp\windows-guarded-admin-mcp\scripts\launch.ps1
```

## validate_mcp.ps1 failures

| Result | Fix |
|--------|-----|
| `Runtime FAIL` | Install Python at `C:\DVB\python\python.exe` or uv |
| `MCP handshake FAIL` | Read stderr; fix import/config errors |
| `Tool enumeration WARN` | Server module loaded but tools empty — check FastMCP registration |
| `list_servers WARN` | No `servers.local.yaml`; expected until inventory added |

## WinRM / access denied

1. Run `scripts/diagnose_environment.ps1 -RemoteServer <hostname>`
2. Verify port 5985 open: `Test-NetConnection hostname -Port 5985`
3. Confirm operator is in Administrators on target
4. Check WinRM: `winrm get winrm/config` on target
5. Review Remote UAC: `LocalAccountTokenFilterPolicy` registry if needed

### TrustedHosts

If workgroup or cross-domain without Kerberos, may need:

```powershell
Set-Item WSMan:\localhost\Client\TrustedHosts -Value 'hostname' -Concatenate
```

Prefer domain join + Kerberos over TrustedHosts in production.

## Script catalog errors

| Error | Meaning |
|-------|---------|
| `Script not in catalog` | Unknown `script_id` |
| `not authorized for server` | Add server to `allowed_servers` or `scripts.local.yaml` |
| `SHA256 mismatch` | File changed since hash pinned; update hash or use `auto` |
| `Script path escapes` | Path traversal attempt blocked |
| `Only .ps1 scripts` | `.py`/`.bat` not allowed remotely |

## Empty server list

`list_servers` returns example servers from `servers.example.yaml` plus local overlay.

If missing real server: create `config/servers.local.yaml` and set `WSA_MCP_CONFIG_DIR` in MCP env (install script does this).

## Integration tests skipped

Set environment variable:

```powershell
$env:WSA_MCP_INTEGRATION='1'
```

Without it, integration tests skip by design.

## uv not found

Expected on this workstation. Install script falls back to:

```
C:\DVB\python\python.exe -m pip install -e C:\DVB\mcp\windows-guarded-admin-mcp
```

## Hyphenated MCP server name

PowerShell `ConvertFrom-Json` cannot set `$config.mcpServers.windows-guarded-admin` directly. Fixed in `McpConfig.ps1` using hashtable indexing.

## Logs

- MCP server logs: stderr (Cursor MCP output panel)
- Audit JSONL: `logs/audit/` (gitignored)
- Diagnostic: `reports/environment_diagnostic.json` (gitignored)

## Getting help

1. `scripts/validate_mcp.ps1`
2. `scripts/diagnose_environment.ps1`
3. Unit tests: `pytest tests/unit tests/security -q`
4. Review [WINRM.md](WINRM.md) and [AUTHENTICATION.md](AUTHENTICATION.md)
