# Legacy Analysis

Analysis of prior art and legacy scripts that informed windows-server-admin-mcp v0.1.0.

## remotest.py

**Path:** `C:\Users\youruser\Downloads\py\remotest.py`

Minimal remote diagnostic stub:

```python
print("Host:", socket.gethostname())
print("CWD:", os.getcwd())
print("Args:", sys.argv)
```

**Assessment:** Useful only as a trivial "did execution reach the remote host?" probe. No structured output, no auth model, no policy layer. Concept absorbed into `smoke_test.ps1` (JSON output) and `test_remote_execution` MCP tool.

## server_info.py

**Path:** `C:\Users\youruser\Downloads\py\server_info.py`

Jupyter-exported script collecting server metrics (RAM, disks) and posting to PostgreSQL via `file2df.conexion()`.

**Security finding:** **Hardcoded credentials present** in database connection calls (username/password literals and helper invocation with embedded secrets). These values must never be copied into MCP config or documentation.

**Assessment:** Operational monitoring script, not an admin API. Database coupling and inline secrets are anti-patterns. windows-server-admin-mcp returns metrics via MCP JSON envelopes without external DB writes.

## file2df.py

**Scope:** Out of scope for this MCP. Not analyzed or integrated.

## deploy_and_run.ps1 / runremote.ps1

**Status:** **NOT FOUND** on disk at expected locations.

Searched under `C:\Users\youruser\Downloads\py\` and project tree. No deploy/run remote orchestration scripts available for porting. Remote execution design built from PowerShell remoting best practices and new `powershell/Invoke-Mcp*.ps1` dispatchers instead.

## windows-admin-mcp (TypeScript)

**Path:** `C:\DVB\mcp\windows-admin-mcp\`

Related prior art — different project, same problem domain.

| Aspect | windows-admin-mcp (TS) | windows-server-admin-mcp (Python) |
|--------|------------------------|-----------------------------------|
| Runtime | Node.js / TypeScript | Python 3.10+ / FastMCP |
| Config | `servers.json` + credentials | YAML + **no credentials** |
| Auth | Credential Manager, `.env` | Integrated Windows only |
| Transport | SSH pilot + WinRM HTTPS | WinRM HTTP/PSSession + SMB fallback |
| Script execution | Approved diagnostics list | SHA256 catalog of `.ps1` files |
| Restart | dry-run default | Two-step confirmation tokens |
| Audit | JSONL | JSONL |

### Ideas kept

- Explicit server allowlist (fail closed)
- Separation of read vs mutating tools
- Audit logging per MCP invocation
- Cursor global MCP registration with backup
- Policy flags per server (`restart_server`, etc.)
- No arbitrary PowerShell MCP tool

### Problems fixed

- Removed credential storage from config surface
- Added script path confinement and hash verification
- Added confirmation token workflow for reboots
- Stricter input validation (metacharacter rejection)
- Python + PowerShell 5.1 alignment with existing enterprise tooling
- Local/gitignored overlays for real hostnames

## Why not consume the old API

1. **No stable API** — legacy files are ad-hoc scripts, not a versioned service contract
2. **Secret leakage risk** — server_info.py embeds credentials; incompatible with MCP security model
3. **Missing deployment scripts** — deploy_and_run.ps1 / runremote.ps1 not available
4. **Different transport assumptions** — TypeScript MCP targeted SSH/HTTPS; this environment validates WinRM 5985 + integrated auth
5. **MCP-native design** — clean-slate FastMCP tool surface with uniform `OperationResult` envelopes beats wrapping legacy subprocess calls

## Recommendation

Keep legacy scripts as historical reference only. Do not import credentials or hostnames from server_info.py. Use windows-server-admin-mcp catalog + gitignored local YAML for all new remote operations.
