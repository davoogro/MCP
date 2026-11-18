# Script Catalog

## Purpose

Remote PowerShell execution is limited to **pre-registered scripts** in `config/scripts.yaml`. There is no MCP tool for arbitrary command execution.

## Catalog file

```yaml
scripts:
  smoke_test:
    description: PowerShell smoke test for remote execution verification
    path: authorized_scripts/smoke_test.ps1
    sha256: auto
    timeout_seconds: 60
    allowed_servers:
      - integration-server-01
      - lab-server-02
    parameters:
      message:
        type: string
        required: false
        description: Optional message echoed in the response
```

Fallback: if `scripts.yaml` missing, `scripts.example.yaml` is loaded.

## Local overlay

`config/scripts.local.yaml` (gitignored) extends `allowed_servers` without putting real hostnames in tracked files:

```yaml
scripts:
  smoke_test:
    allowed_servers:
      - your-real-server-id
```

Merge logic in `config.py` concatenates allowlists.

## SHA256 modes

| Value | Behavior |
|-------|----------|
| `auto` or `AUTO` | Compute hash at runtime; always accept if file unchanged since load |
| 64-char hex | Pin exact content; mismatch → `ScriptHashMismatchError` |

Use `auto` during development; pin hashes for production catalog entries.

## Path rules

- Relative paths resolve from project root
- Must end in `.ps1`
- Must stay under `authorized_scripts/` or `powershell/`
- Path traversal (`../`) rejected

## Script IDs

Current catalog:

| ID | File | Remote |
|----|------|--------|
| `smoke_test` | `authorized_scripts/smoke_test.ps1` | Yes |

Local-only (not in remote catalog):

| File | Purpose |
|------|---------|
| `authorized_scripts/smoke_test.py` | Local package/config smoke test |

## Adding a script

1. Write `.ps1` with structured JSON output (see `smoke_test.ps1`)
2. Add entry to `scripts.yaml`
3. Set `allowed_servers` explicitly (empty list = deny all)
4. Run unit tests: `tests/unit/test_catalog.py`
5. Test remotely: `run_authorized_script` MCP tool

## MCP invocation

```json
{
  "tool": "run_authorized_script",
  "arguments": {
    "server_id": "integration-server-01",
    "script_id": "smoke_test",
    "parameters": { "message": "catalog smoke OK" }
  }
}
```

Requires `permissions.authorized_scripts: true` on target server.

## Security tests

- Hash mismatch blocked (`tests/security/test_hash_mismatch.py`)
- Path traversal blocked (`tests/unit/test_catalog.py`)
- Non-`.ps1` extensions rejected
