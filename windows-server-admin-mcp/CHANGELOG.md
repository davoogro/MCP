# Changelog

All notable changes to windows-server-admin-mcp are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-07-23

### Added

- Python MCP server (`windows-server-admin-mcp`) with STDIO transport via FastMCP
- 17 MCP tools: inventory, diagnostics, health, events, authorized scripts, restart workflow, audit
- YAML configuration: servers, policies, scripts with `*.example.yaml` templates
- Local overlays: `servers.local.yaml`, `scripts.local.yaml` (gitignored)
- Integrated Windows authentication only (`current_windows_identity`); credential config rejected
- WinRM/PSSession execution layer with SMB fallback for script transfer
- Authorized script catalog with SHA256 verification (`auto` mode)
- Two-step server restart confirmation tokens with TTL and parameter binding
- Policy engine: server allowlist, service denylist, input validation, permission matrix
- PowerShell dispatcher scripts under `powershell/`
- Cursor install/validate/uninstall/update scripts
- Test suites: 82 unit/security tests; integration tests behind `WSA_MCP_INTEGRATION=1`
- Documentation: README, SECURITY, OPERATIONS, architecture and operational guides

### Security

- No hardcoded credentials in source (validated by security tests)
- Integration hosts configured with all `restart_*` permissions false
- Real server/service restarts not executed during initial validation

### Known limitations

- Overall test coverage ~54%; service/execution modules lower without live mocks
- `uv` optional; pip fallback to `C:\DVB\python\python.exe` when uv absent
- Only `.ps1` scripts in remote catalog (no arbitrary remote Python)
- WinRM HTTP (5985) used for integration; HTTPS (5986) optional per server config

### Repository

- Local git init only; not pushed to remote
- Real hostnames confined to gitignored local config and reports

[0.1.0]: https://local/windows-server-admin-mcp/v0.1.0
