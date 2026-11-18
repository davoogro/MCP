#Requires -Version 5.1
<#
.SYNOPSIS
    Registers windows-admin-mcp in Cursor mcp.json without removing existing servers.
#>
[CmdletBinding()]
param(
    [string]$CursorMcpPath = "$env:USERPROFILE\.cursor\mcp.json",
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$NodePath = "$env:LOCALAPPDATA\Programs\cursor\resources\app\resources\helpers\node.exe"
)

$entryName = 'windows-admin'
$distPath = Join-Path $ProjectRoot 'dist\src\index.js'
$configPath = Join-Path $ProjectRoot 'config\servers.json'
$auditDir = Join-Path $ProjectRoot 'logs\audit'

if (-not (Test-Path $distPath)) {
    throw "Build output not found: $distPath. Run 'npm run build' first."
}

if (-not (Test-Path $NodePath)) {
    $NodePath = 'node'
}

if (-not (Test-Path $CursorMcpPath)) {
    @{ mcpServers = @{} } | ConvertTo-Json -Depth 10 | Set-Content $CursorMcpPath -Encoding UTF8
}

$json = Get-Content $CursorMcpPath -Raw | ConvertFrom-Json
if (-not $json.mcpServers) { $json | Add-Member -NotePropertyName mcpServers -NotePropertyValue (@{}) }

$backupPath = "$CursorMcpPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Copy-Item $CursorMcpPath $backupPath -Force

$json.mcpServers.$entryName = @{
    command = $NodePath
    args    = @($distPath)
    env     = @{
        WINMCP_CONFIG    = $configPath
        WINMCP_AUDIT_DIR = $auditDir
    }
}

$json | ConvertTo-Json -Depth 10 | Set-Content $CursorMcpPath -Encoding UTF8
@{ success = $true; backup = $backupPath; registered = $entryName; config = $CursorMcpPath } | ConvertTo-Json
