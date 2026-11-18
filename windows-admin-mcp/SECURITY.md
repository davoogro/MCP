# Security — windows-admin-mcp

Security model, credential handling, transport requirements, and audit expectations for the Windows Admin MCP. Designed for fail-closed operation in enterprise environments (including Spanish-speaking teams using English runbooks).

## Security principles

1. **Allowlist only** — Remote actions target hosts defined in `config/servers.json`. Any other hostname or IP is rejected.
2. **Fail closed** — Invalid identity, missing credentials, TLS failure, or ambiguous config → no operation.
3. **No arbitrary code execution** — Tools use typed parameters and approved command templates; no public “run any script” API.
4. **Least privilege** — Service accounts receive only rights needed for read diagnostics and explicitly approved write actions.
5. **Secrets out of band** — Credentials live in CredMan, SecretManagement, or protected env vars — never in git, JSON config, or logs.
6. **Encrypted remoting** — SSH or WinRM **HTTPS** only; no HTTP WinRM, no Basic auth, no `TrustedHosts *`.
7. **Guarded reboots** — `restart_server` defaults to `dryRun: true`; real reboots need `allowReboot: true` and `dryRun: false`.
8. **Audit everything** — Structured logs with correlation IDs; sensitive fields redacted.

## Threat model (summary)

| Threat | Mitigation |
| --- | --- |
| MCP host compromise | Allowlist limits blast radius; no stored passwords in config; reboot guards |
| Credential theft from disk | CredMan / vault; `.env` gitignored; logs redacted |
| MITM on remoting | TLS verification (`verifyTls: true`); valid certificates; SSH host key policy |
| Prompt injection via Cursor | No free-form PowerShell tool; parameter validation |
| Accidental mass reboot | `maxConcurrency`; sequential reboot default; `protectedRole` |
| Localhost reboot | Explicit block on MCP workstation hostname |
| Privilege escalation on target | Dedicated svc account; no interactive admin in MCP config |

## Allowlist enforcement

Source: `config/servers.json` (validated against JSON Schema).

Rejected requests include:

- Unknown `server` name
- IP/hostname not matching allowlisted `address`
- Attempts to alias or tunnel to non-allowlisted hosts via diagnostics

Configuration fields with security impact:

| Field | Default | Purpose |
| --- | --- | --- |
| `allowReboot` | `false` | Must be `true` for real `restart_server` |
| `protectedRole` | `true` for sensitive templates | Blocks reboot unless exception documented |
| `verifyTls` | `true` | Rejects invalid certificates |
| `maxConcurrency` | `2` | Limits parallel destructive ops |

## Credential loading

Three supported backends (see [OPERATIONS.md](./OPERATIONS.md#credential-loading)):

### Windows Credential Manager

- **Storage:** Generic credentials (`cmdkey /generic:...`)
- **Naming:** `{WIN_ADMIN_MCP_CRED_PREFIX}{SERVER-NAME}` e.g. `WinAdminMCP/SERVER-PILOT-01`
- **Pros:** Native, per-user on admin workstation
- **Cons:** Not centrally rotated without extra tooling

### PowerShell SecretManagement

- **Storage:** Registered vault (KeePass, Azure Key Vault module, corporate plugin)
- **Naming:** Same logical prefix inside vault
- **Pros:** Central rotation, team access policy
- **Cons:** Requires vault module deployment

### Environment variables

- **Storage:** Cursor MCP `env`, OS user/machine vars, CI secret store
- **Naming:** `WIN_ADMIN_MCP_USER_<NORMALIZED_NAME>`, `WIN_ADMIN_MCP_PASS_<NORMALIZED_NAME>`
- **Pros:** Simple for Cursor integration
- **Cons:** Visible to processes running as same user — use only with OS-level protection

### Prohibited

- Passwords in `servers.json`, `mcp.json` in git, README, tickets pasted into chat
- Shared “admin/admin” test accounts in production allowlist
- Logging credential values on connection failure

## Transport security

### Priority order

1. **PowerShell over SSH** (port 22) — key or password auth per org policy; restrict firewall source to jump host
2. **WinRM HTTPS** (port 5986) — machine cert from enterprise CA; Kerberos for domain members
3. Other mechanisms only if formally approved and auditable

### WinRM requirements

| Requirement | Status |
| --- | --- |
| HTTPS listener | Required |
| HTTP listener | **Forbidden** |
| Basic auth | **Forbidden** |
| `TrustedHosts *` | **Forbidden** |
| Valid certificate | Required when `verifyTls: true` |
| Firewall | Allow 5986 from admin sources only — do not disable firewall |

### SSH requirements

- OpenSSH Server with supported configuration
- Host key verification — document first-connect procedure
- Subsystem for PowerShell remoting enabled
- No weakening of `StrictHostKeyChecking` in production without compensating controls

## MCP tool authorization matrix

| Tool | Data exposure | Destructive | Extra guards |
| --- | --- | --- | --- |
| `list_managed_servers` | Low | No | — |
| `test_server_connection` | Low | No | — |
| `get_*` read tools | Medium | No | Timeouts |
| `run_approved_diagnostic` | Medium | No | Closed operation list |
| `restart_service` | Medium | Yes (service) | Service name validation |
| `restart_server` | High | **Yes (OS)** | Full reboot guard chain |
| `wait_for_server_online` | Low | No | — |

## Reboot protection (`restart_server`)

Automatic checks before any reboot:

1. Server in allowlist
2. `allowReboot === true`
3. Not MCP localhost / Cursor workstation
4. Not `protectedRole` unless config exception flag set
5. Mandatory `reason` string (audited)
6. `dryRun === false` for actual restart
7. Configurable timeout and post-recovery validation
8. No parallel reboots unless explicitly configured and approved

Default parameter: **`dryRun: true`**.

Domain controllers, cluster nodes, and similarly tagged servers should keep `protectedRole: true` and `allowReboot: false`.

## Audit logging

Location: `WIN_ADMIN_MCP_LOG_DIR` (default `./logs/`).

Each entry includes (when applicable):

- `timestampUtc`
- `correlationId`
- `tool`
- `server` / `address`
- `result` / `success`
- `durationMs`
- `reason` (administrative)
- `dryRun`
- `error` (sanitized)

### Never log

- Passwords or password hashes
- Kerberos tickets, OAuth tokens, API keys
- SecureString contents
- Private keys or certificate private material
- Authorization headers
- Full environment dumps
- User-provided `reason` text containing pasted secrets (truncate/redact if detected)

### Redaction

The server applies redaction filters to stderr, exception messages, and structured fields matching patterns such as `password`, `token`, `secret`, `Bearer `, and base64 credential blobs.

## Log rotation and retention

Operational recommendation (see [OPERATIONS.md](./OPERATIONS.md#log-rotation)):

- Daily JSONL files, 90-day local retention, archived compressed
- Restrict ACLs on `logs/` to administrators and audit role
- Forward to SIEM for tamper-evident storage

## Cursor MCP registration

- Use absolute paths to `node` and `dist/src/index.js`
- Place secrets in CredMan/vault; reference mode via env — not inline passwords in committed config
- Backup `mcp.json` before merge
- Do not remove unrelated MCP servers when adding `windows-admin`

## Hardening checklist

### Workstation running Cursor

- [ ] Dedicated admin account or approved jump workflow
- [ ] Disk encryption enabled
- [ ] MCP repo clone without secrets
- [ ] CredMan/vault populated per server
- [ ] Latest security patches

### Each allowlisted server

- [ ] Transport encrypted (SSH or WinRM HTTPS)
- [ ] Firewall scoped to admin sources
- [ ] Service account least privilege
- [ ] `allowReboot` false unless maintenance role
- [ ] Rollback script accessible

### Repository

- [ ] `.env` gitignored
- [ ] `config/servers.json` gitignored (only `servers.example.json` committed)
- [ ] `logs/` gitignored
- [ ] Pre-commit hooks exclude accidental secret commits (recommended)

## Incident response

If credentials may be exposed:

1. Revoke/rotate affected accounts immediately
2. Review audit logs for unexpected `restart_server` or `restart_service` entries
3. Temporarily disable MCP in Cursor config
4. Re-issue credentials via vault; run `test_server_connection` before re-enable

If unauthorized reboot occurred:

1. Preserve `logs/audit-*.jsonl`
2. Identify correlation ID and change record in `reason` field
3. Follow organizational IR procedure

## Compliance notes

- Align retention with local policy (GDPR, internal audit)
- Document data processed: hostnames, usernames, health metrics, event log excerpts
- Spanish-speaking operators may use Spanish text in `reason` fields; audit storage remains UTF-8 JSON

## Related documents

- [OPERATIONS.md](./OPERATIONS.md) — Procedures and examples
- [ROLLBACK.md](./ROLLBACK.md) — Revert SSH/WinRM preparation
- [README.md](./README.md) — Overview and tool list
