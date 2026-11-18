# Cursor Installation

Install windows-server-admin-mcp into global Cursor MCP configuration.

## Prerequisites

- Python 3.10+ at `C:\DVB\python\python.exe` (or `uv` in PATH)
- PowerShell 5.1+
- Project cloned to e.g. `C:\DVB\mcp\windows-server-admin-mcp`
- Local server inventory: `config/servers.local.yaml`

## Install command

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\DVB\mcp\windows-server-admin-mcp\scripts\install.ps1
```

### What install.ps1 does

1. Detects Cursor executable
2. Locates `%USERPROFILE%\.cursor\mcp.json`
3. Creates backup: `backups/cursor-mcp/mcp.json.bak-YYYYMMDD-HHmmss`
4. Installs Python package (`uv sync` or `pip install -e`)
5. Writes `scripts/launch.ps1` (machine-specific, gitignored)
6. Registers MCP entry **`windows-server-admin`**
7. Runs `validate_mcp.ps1`

## Resulting mcp.json entry

```json
{
  "mcpServers": {
    "windows-server-admin": {
      "command": "powershell.exe",
      "args": [
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "C:\\DVB\\mcp\\windows-server-admin-mcp\\scripts\\launch.ps1"
      ],
      "env": {
        "WSA_MCP_CONFIG_DIR": "C:\\DVB\\mcp\\windows-server-admin-mcp\\config"
      }
    }
  }
}
```

Uses **absolute paths** and **STDIO** transport (PowerShell launcher → Python MCP).

## Preserved entries

Install merges without removing existing servers. Verified preserved:

- `fabric-sql`
- `fabric-sql-2`
- `wsl`
- `windows-admin` (legacy TypeScript MCP)
- `multi-postgres`

## Validate

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\validate_mcp.ps1
```

Expected output:

```
[PASS] Runtime: C:\DVB\python\python.exe
[PASS] MCP handshake: server module loaded
[PASS] Tool enumeration: 17 tools
[PASS] list_servers: [...]
```

Evidence recorded in `reports/mcp_validation.md`.

## After install

1. Restart Cursor or reload MCP servers
2. Confirm `windows-server-admin` appears in MCP tools list
3. Run `list_servers` from agent chat

## Uninstall

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

## Manual registration (alternative)

If install script fails, add entry manually to `%USERPROFILE%\.cursor\mcp.json` using the JSON block above. Set `WSA_MCP_CONFIG_DIR` to your config directory.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Hyphenated MCP name error | Fixed in McpConfig.ps1 hashtable merge |
| uv not found | pip fallback to `C:\DVB\python\python.exe` |
| Tools not appearing | Restart Cursor; check launcher path |
| Empty inventory | Create `servers.local.yaml` |

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
