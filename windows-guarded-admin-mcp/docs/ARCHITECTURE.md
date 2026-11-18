# Architecture

## Overview

```
┌─────────────┐    STDIO     ┌──────────────────────┐    subprocess    ┌─────────────────┐
│ Cursor MCP  │◄────────────►│ windows-guarded-admin │─────────────────►│ powershell.exe  │
│   host      │   JSON-RPC   │ -mcp (FastMCP)       │   Invoke-Mcp*    │ (local runner)  │
└─────────────┘              └──────────┬───────────┘                  └────────┬────────┘
                                        │                                        │
                                        │ PolicyEngine + Services                │ WinRM/PSSession
                                        ▼                                        ▼
                             ┌──────────────────────┐                  ┌─────────────────┐
                             │ YAML config          │                  │ Remote Windows  │
                             │ servers/policies/    │                  │ Server          │
                             │ scripts              │                  └─────────────────┘
                             └──────────────────────┘
```

## Layers

| Layer | Path | Responsibility |
|-------|------|----------------|
| MCP surface | `server.py` | Tool registration, audit wrapper, JSON envelopes |
| Services | `services/` | Business logic per domain (health, scripts, restart) |
| Policies | `policies.py` | Allowlist, permissions, validation |
| Catalog | `catalog.py` | Script registry, path confinement, SHA256 |
| Confirmations | `confirmations.py` | One-time restart tokens |
| Execution | `execution/` | PowerShell runner, WinRM transport, PSSession/SMB transfer |
| Config | `config.py` | YAML load/merge, pydantic models |
| Audit | `audit.py` | JSONL structured logging |
| Dispatcher PS1 | `powershell/` | Remote operation implementations |

## Configuration merge order

1. `servers.example.yaml` or `servers.yaml` (if present)
2. **`servers.local.yaml`** overlay (gitignored)
3. `policies.example.yaml` or `policies.yaml`
4. `scripts.example.yaml` or `scripts.yaml`
5. **`scripts.local.yaml`** overlay — extends `allowed_servers` per script (gitignored)

Environment override: `WSA_MCP_CONFIG_DIR`.

## Remote execution path

1. MCP tool invoked → `PolicyEngine.get_server()` + permission check
2. Service builds request → `Invoke-McpRemoteOperation.ps1` or specialized script
3. `powershell_runner.run_powershell_file()` spawns local PowerShell
4. Local PS opens PSSession to `-ComputerName` using integrated auth
5. Script content transferred via PSSession file copy or SMB fallback (`C$` staging)
6. Result parsed as JSON → `OperationResult` envelope

## Operation tracking

Mutating tools create entries in `OperationRegistry` (`operations.py`):

- `run_authorized_script`
- `restart_service`
- `prepare_server_restart` / `confirm_server_restart` / `cancel_server_restart`

Query status via `get_operation_status`.

## Data models

`OperationResult` (in `models.py`) is the uniform MCP response:

- `success`, `data`, `error` (with `ErrorCode` enum)
- `server_id`, `operation_id`, `transport`, `duration_ms`, timestamps

## Package layout

```
windows-guarded-admin-mcp/
├── src/windows_guarded_admin_mcp/   # Python package
├── powershell/                     # Remote dispatcher scripts
├── authorized_scripts/             # Catalog-approved .ps1
├── config/                         # YAML (+ gitignored local overlays)
├── scripts/                        # Install, validate, diagnose
├── tests/                          # unit, security, integration
└── docs/                           # Detailed guides
```

## Dependencies

- `mcp~=1.28` — FastMCP STDIO server
- `pydantic>=2` — config/result models
- `pyyaml>=6` — configuration

No pywinrm Python library; remoting delegated entirely to PowerShell 5.1+.

## Extension points

- Add MCP tools in `server.py` + corresponding service
- Add remote capability in `powershell/*.ps1`
- Register scripts in `config/scripts.yaml`
- Extend policies in `config/policies.yaml`
