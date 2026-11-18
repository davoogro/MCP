# Security

## Threat model

windows-guarded-admin-mcp exposes Windows Server administration to an LLM host (Cursor) over MCP STDIO. Primary risks:

1. **Unauthorized remote execution** — arbitrary PowerShell on production servers
2. **Credential theft** — passwords or tokens in config or logs
3. **Privilege escalation** — injection via server IDs, script paths, or parameters
4. **Destructive operations** — unplanned reboots or service restarts
5. **Data exfiltration** — event logs, process lists, disk info over MCP

## Design principles (fail closed)

| Control | Implementation |
|---------|----------------|
| Server allowlist | Only `server_id` keys in merged YAML inventory |
| No embedded credentials | `current_windows_identity` only; credential blocks rejected at parse |
| No arbitrary PowerShell MCP tool | Remote ops via fixed dispatcher PS1 + catalog `.ps1` only |
| Script integrity | SHA256 catalog (`auto` computes at runtime; mismatch blocks run) |
| Path confinement | Scripts must resolve under project `authorized_scripts/` or `powershell/` |
| Input validation | Server IDs, hostnames, service names reject shell metacharacters |
| Permission matrix | Per-server flags: `read_operations`, `authorized_scripts`, `restart_*` |
| Service denylist | Global block on critical services (RpcSs, NTDS, MSEXCHANGE*) |
| Restart confirmation | Two-step token (`prepare` → `confirm`) with TTL and parameter binding |
| Audit | JSONL tool calls with sanitized parameters (no confirmation tokens logged) |
| Localhost / wildcard block | `localhost`, `*`, UNC paths rejected for hostnames |

## Secrets handling

- **Never commit:** `config/servers.local.yaml`, `config/scripts.local.yaml`, `.env`, diagnostic reports with real hostnames
- **No passwords in source:** static scan in `tests/security/test_no_secrets.py`
- **MCP config backups:** stored under `backups/cursor-mcp/` (gitignored)

## Integrated authentication

Uses the operator's Windows logon token via PowerShell remoting. No password storage. Requires:

- Kerberos/NTLM reachability to target
- Remote admin or delegated WinRM permissions
- WinRM enabled on target (HTTP 5985 or HTTPS 5986)

See [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md).

## Restart safety

Integration servers MUST have:

```yaml
permissions:
  restart_server: false
  restart_service: false
  cancel_restart: false
```

Real server/service restarts were **not executed** during v0.1.0 validation.

## Reporting vulnerabilities

Internal use only. Do not publish repository or logs containing real hostnames, usernames, or network addresses without redaction.

## Comparison to windows-psremoting-mcp (TypeScript)

The prior TypeScript MCP stored credentials in config/env. This Python rewrite **eliminates credential config** and adds script catalog hashing, confirmation tokens, and stricter validation. See [docs/LEGACY_ANALYSIS.md](docs/LEGACY_ANALYSIS.md).
