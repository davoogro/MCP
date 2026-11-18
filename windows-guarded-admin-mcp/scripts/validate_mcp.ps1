#Requires -Version 5.1
<#
.SYNOPSIS
    Validates windows-guarded-admin-mcp handshake and list_servers tool.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [int]$TimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
}

. (Join-Path $ScriptRoot 'lib\McpConfig.ps1')

$results = [System.Collections.ArrayList]@()
function Add-ValidationResult {
    param([string]$Name, [string]$Status, [string]$Detail)
    [void]$results.Add([ordered]@{ name = $Name; status = $Status; detail = $Detail })
    $color = switch ($Status) {
        'PASS' { 'Green' }
        'FAIL' { 'Red' }
        default { 'Yellow' }
    }
    Write-Host ("[{0}] {1}: {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
}

$uvPath = Resolve-UvPath
$pythonPath = Resolve-ToolPath -Name 'python'
$env:WSA_MCP_CONFIG_DIR = Join-Path $ProjectRoot 'config'

$runner = $null
$runnerArgs = @()

if ($uvPath -and (Test-Path -LiteralPath (Join-Path $ProjectRoot 'pyproject.toml'))) {
    $runner = $uvPath
    $runnerArgs = @('run', '--directory', $ProjectRoot, 'python')
} elseif ($pythonPath) {
    $runner = $pythonPath
    $runnerArgs = @()
} else {
    $fallbackPython = 'C:\DVB\python\python.exe'
    if (Test-Path -LiteralPath $fallbackPython) {
        $runner = $fallbackPython
        $runnerArgs = @()
    } else {
        Add-ValidationResult -Name 'Runtime' -Status 'FAIL' -Detail 'Neither uv nor python found'
        exit 2
    }
}

Add-ValidationResult -Name 'Runtime' -Status 'PASS' -Detail ($runner)

$mcpCli = $null
try {
    if ($uvPath) {
        $mcpCli = & $uvPath run --directory $ProjectRoot python -c "import shutil; print(shutil.which('mcp') or '')" 2>$null
    } elseif ($pythonPath) {
        $mcpCli = & $pythonPath -c "import shutil; print(shutil.which('mcp') or '')" 2>$null
    }
    $mcpCli = ($mcpCli | Out-String).Trim()
} catch {
    $mcpCli = ''
}

$validationPy = @'
import asyncio
import json
import os
import sys
from pathlib import Path

project_root = Path(os.environ.get("WSA_MCP_PROJECT_ROOT", ".")).resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root / "src"))

async def main():
    out = {"tools": [], "list_servers": None, "errors": []}
    try:
        try:
            from mcp.server import MCPServer  # mcp >= 2.0
        except ImportError:
            from mcp.server.fastmcp import FastMCP  # mcp 1.x
        out["mcp_import"] = "ok"
    except Exception as exc:
        out["errors"].append(f"mcp import: {exc}")
        print(json.dumps(out))
        return

    try:
        from windows_guarded_admin_mcp.server import mcp as server_mcp
        tools_fn = getattr(server_mcp, "list_tools", None)
        if callable(tools_fn):
            tools = await tools_fn()
            out["tools"] = [getattr(t, "name", str(t)) for t in tools]
        elif hasattr(server_mcp, "_tool_manager"):
            out["tools"] = list(server_mcp._tool_manager._tools.keys())
        else:
            out["errors"].append("Could not enumerate tools from server module")
    except Exception as exc:
        out["errors"].append(f"server import/tools: {exc}")

    try:
        from windows_guarded_admin_mcp.config import load_config, resolve_config_dir
        cfg = load_config(resolve_config_dir())
        servers = []
        for sid, entry in cfg.servers.items():
            servers.append({
                "id": sid,
                "hostname": entry.hostname,
                "environment": entry.environment,
                "enabled": entry.enabled,
            })
        out["list_servers"] = servers
    except Exception as exc:
        out["errors"].append(f"list_servers: {exc}")

    print(json.dumps(out))

asyncio.run(main())
'@

$tempPy = Join-Path $env:TEMP ("wsa_mcp_validate_{0}.py" -f ([guid]::NewGuid().ToString('N')))
Set-Content -LiteralPath $tempPy -Value $validationPy -Encoding UTF8

try {
    $env:WSA_MCP_PROJECT_ROOT = $ProjectRoot
    $invokeArgs = @()
    if ($runnerArgs.Count -gt 0) {
        $invokeArgs += $runnerArgs
    }
    $invokeArgs += @($tempPy)

    $raw = & $runner @invokeArgs 2>&1 | Out-String
    $payload = $null
    try {
        $payload = $raw | ConvertFrom-Json
    } catch {
        Add-ValidationResult -Name 'MCP handshake' -Status 'FAIL' -Detail ("Invalid JSON output: {0}" -f ($raw.Trim()))
        exit 2
    }

    if ($payload.errors -and $payload.errors.Count -gt 0) {
        Add-ValidationResult -Name 'MCP handshake' -Status 'FAIL' -Detail ($payload.errors -join '; ')
    } else {
        Add-ValidationResult -Name 'MCP handshake' -Status 'PASS' -Detail 'server module loaded'
    }

    if ($payload.tools -and $payload.tools.Count -gt 0) {
        Add-ValidationResult -Name 'Tool enumeration' -Status 'PASS' -Detail ("{0} tools" -f $payload.tools.Count)
    } else {
        Add-ValidationResult -Name 'Tool enumeration' -Status 'WARN' -Detail 'no tools reported yet'
    }

    if ($payload.list_servers) {
        Add-ValidationResult -Name 'list_servers' -Status 'PASS' -Detail (($payload.list_servers | ConvertTo-Json -Compress))
    } else {
        Add-ValidationResult -Name 'list_servers' -Status 'WARN' -Detail 'inventory empty or not loaded'
    }
} finally {
    Remove-Item -LiteralPath $tempPy -Force -ErrorAction SilentlyContinue
}

$failCount = @($results | Where-Object { $_.status -eq 'FAIL' }).Count
if ($failCount -gt 0) { exit 2 }
exit 0
