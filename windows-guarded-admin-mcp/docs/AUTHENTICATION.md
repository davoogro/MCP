# Authentication

## Supported method (v0.1.0)

**Integrated Windows authentication only** — `connection.authentication: current_windows_identity`

The MCP server runs on the operator's Windows workstation under the logged-on user's security context. PowerShell remoting uses that token via:

- Kerberos (preferred in domain)
- NTLM (fallback)

## Explicitly not supported

| Method | Status |
|--------|--------|
| Username/password in YAML | **Rejected** at config parse |
| `-Credential` objects in Python | Not implemented |
| `-AsPlainText` SecureString | Blocked by security tests |
| API keys / tokens | Not applicable |

If `credential:` appears in a server entry, config load raises `ValidationError`.

## Configuration example

```yaml
servers:
  integration-server-01:
    hostname: integration-server-01.example.local
    connection:
      transport: winrm
      authentication: current_windows_identity
      port: 5985
      use_ssl: false
      use_pssession: true
```

Real hostname goes in gitignored `servers.local.yaml`.

## Requirements

1. Operator logged in as domain account with remote admin rights
2. Target resolves via DNS (or configured hostname)
3. No cross-forest issues without trust
4. UAC: remote admin token must be available (Remote UAC settings on target)

## Identity verification

MCP tool `get_server_identity` returns remote hostname, OS version, domain/workgroup.

Diagnostic script reports `whoami` from successful PSSession (logged in audit/diagnostic reports only — gitignored).

## Comparison to legacy approaches

| Approach | windows-psremoting-mcp (TS) | windows-guarded-admin-mcp (Python) |
|----------|------------------------|-----------------------------------|
| Credential Manager | Supported | Removed |
| `.env` passwords | Supported | Not used |
| Integrated auth | Partial | **Only method** |

Rationale: eliminate secret storage in MCP config; rely on enterprise SSO session.

## Future considerations (not in 0.1.0)

- gMSA / managed service identity for dedicated automation account
- WinRM HTTPS with certificate validation
- Just Enough Administration (JEA) constrained endpoints
