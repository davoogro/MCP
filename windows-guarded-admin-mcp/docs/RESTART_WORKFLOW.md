# Restart Workflow

## Overview

Server restarts use native `shutdown.exe` with a **two-step confirmation** to prevent accidental LLM-triggered reboots.

```
prepare_server_restart → confirmation_token
        ↓
confirm_server_restart(token) → shutdown.exe /r /m \\HOST /t SECONDS /c "REASON"
        ↓
wait_for_server → poll until WinRM responds
```

Cancel path: `cancel_server_restart` → `shutdown.exe /a /m \\HOST`

## Permission gates

Each server requires explicit YAML flags:

```yaml
permissions:
  restart_server: false    # must be true to restart OS
  restart_service: false   # separate flag for services
  cancel_restart: false    # allow abort of pending shutdown
```

Integration servers MUST keep `restart_server: false`. Enforced by integration and security tests.

## prepare_server_restart

Parameters:

- `server_id` — inventoried server
- `reason_code` — from `restart_reason_codes` in policies (e.g. `PLANNED_MAINTENANCE`)
- `reason_detail` — 10–500 chars, no shell metacharacters
- `delay_seconds` — default 300; passed to shutdown `/t`

Returns one-time `confirmation_token` with TTL (policy-controlled, max clamped).

## confirm_server_restart

Must pass **identical** `reason_code`, `reason_detail`, `delay_seconds` as prepare step. Token consumed on success (single use).

Remote command (via `Invoke-McpServerRestart.ps1`):

```
shutdown.exe /r /m \\HOSTNAME /t DELAY /c "REASON"
```

No `/f` (force apps closed) by default.

## cancel_server_restart

Invalidates token and runs:

```
shutdown.exe /a /m \\HOSTNAME
```

Requires `cancel_restart: true` permission.

## Service restart

Separate tool: `restart_service` with `dry_run` default behavior controlled by service layer.

Global denylist blocks: `RpcSs`, `NTDS`, `MSEXCHANGE*`.

## Validation status (v0.1.0)

| Action | Executed on real server |
|--------|-------------------------|
| `prepare_server_restart` | Blocked by policy on integration host |
| `confirm_server_restart` | **NO** |
| `restart_service` | **NO** |
| Real OS reboot | **NO** |

Workflow implemented and unit-tested; live reboot requires explicit permission change outside integration validation.

## Audit

All restart-related MCP calls logged to JSONL with sanitized parameters (tokens never written to audit).
