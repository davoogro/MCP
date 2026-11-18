# Adding Servers

## Quick steps

1. **Copy template**

   ```powershell
   Copy-Item config\servers.local.yaml.example config\servers.local.yaml
   ```

2. **Add server block** (use logical `server_id`, real `hostname`):

   ```yaml
   servers:
     integration-server-01:
       hostname: your-real-hostname.example.local
       environment: integration
       enabled: true
       connection:
         transport: winrm
         authentication: current_windows_identity
         powershell_executable: powershell.exe
         use_pssession: true
         allow_smb_fallback: true
         port: 5985
         use_ssl: false
       permissions:
         read_operations: true
         authorized_scripts: true
         restart_service: false
         restart_server: false
         cancel_restart: false
       limits:
         command_timeout_seconds: 120
         script_timeout_seconds: 600
         max_parallel_operations: 2
       tags:
         - windows
         - integration
   ```

3. **Authorize scripts** (if needed) — add to `config/scripts.local.yaml`:

   ```yaml
   scripts:
     smoke_test:
       allowed_servers:
         - integration-server-01
   ```

4. **Validate**

   ```powershell
   powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\validate_mcp.ps1
   # MCP: diagnose_server_access, test_remote_execution
   ```

## server_id vs hostname

| Field | Purpose | Example |
|-------|---------|---------|
| `server_id` | YAML key / MCP parameter | `integration-server-01` |
| `hostname` | DNS name for WinRM | `srv01.corp.example.local` |

Use sanitized IDs in tracked docs; real hostnames only in gitignored `servers.local.yaml`.

## Permission profiles

### Read-only integration

```yaml
permissions:
  read_operations: true
  authorized_scripts: true
  restart_service: false
  restart_server: false
  cancel_restart: false
```

### Lab with service restart

```yaml
permissions:
  read_operations: true
  authorized_scripts: true
  restart_service: true
  restart_server: false
  cancel_restart: false
```

### Maintenance window (use with extreme care)

```yaml
permissions:
  restart_server: true
  cancel_restart: true
```

Requires change control; never enable on first pilot.

## Merge behavior

- `servers.example.yaml` provides template entries (`integration-server-01`, `lab-server-02`)
- `servers.local.yaml` **overrides/adds** servers without modifying tracked files
- Disabled servers: `enabled: false` (still in inventory but operations may reject)

## Network checklist

- [ ] DNS resolves hostname from operator workstation
- [ ] TCP 5985 reachable (or 5986 with SSL)
- [ ] Operator has remote admin rights
- [ ] WinRM enabled on target
- [ ] `diagnose_environment.ps1` passes PSSession test

## Integration tests

```powershell
$env:WSA_MCP_INTEGRATION='1'
$env:WSA_MCP_INTEGRATION_SERVER='integration-server-01'  # optional override
C:\DVB\python\python.exe -m pytest tests/integration -v
```

## Do not

- Put passwords or `credential:` blocks in YAML
- Commit `servers.local.yaml`
- Use IP addresses unless `allow_ip_address: true`
- Enable `restart_server` on integration validation hosts
