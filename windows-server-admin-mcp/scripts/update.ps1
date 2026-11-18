#Requires -Version 5.1
<#
.SYNOPSIS
    Updates windows-server-admin-mcp dependencies and refreshes Cursor MCP registration.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [switch]$SkipValidation
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
}

. (Join-Path $ScriptRoot 'lib\McpConfig.ps1')

Write-Host 'Updating windows-server-admin-mcp ...' -ForegroundColor Cyan

$uvPath = Resolve-UvPath
if ($uvPath -and (Test-Path -LiteralPath (Join-Path $ProjectRoot 'pyproject.toml'))) {
    Write-Host "Running uv sync --directory $ProjectRoot"
    & $uvPath sync --directory $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync failed with exit code $LASTEXITCODE"
    }
} else {
    Write-Warning 'uv unavailable; attempting pip install -e'
    $pythonPath = Resolve-ToolPath -Name 'python'
    if (-not $pythonPath) { throw 'No uv or python available for update.' }
    & $pythonPath -m pip install -e $ProjectRoot
}

& (Join-Path $ProjectRoot 'scripts\configure_cursor.ps1') -ProjectRoot $ProjectRoot

$validationScript = Join-Path $ProjectRoot 'scripts\validate_mcp.ps1'
if (-not $SkipValidation -and (Test-Path -LiteralPath $validationScript)) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $validationScript -ProjectRoot $ProjectRoot
}

Write-Host 'Update complete. Restart Cursor if MCP tools did not refresh automatically.' -ForegroundColor Green
