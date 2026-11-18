#Requires -Version 5.1
<#
.SYNOPSIS
    Removes only the windows-guarded-admin MCP entry from Cursor configuration.
#>
[CmdletBinding()]
param(
    [string]$McpConfigPath,
    [string]$EntryName = 'windows-guarded-admin',
    [switch]$RemoveLauncher
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path

. (Join-Path $ScriptRoot 'lib\McpConfig.ps1')

if (-not $McpConfigPath) {
    $McpConfigPath = Get-ActiveCursorMcpConfigPath
}

if (-not (Test-Path -LiteralPath $McpConfigPath)) {
    Write-Host "No MCP config found at $McpConfigPath" -ForegroundColor Yellow
    exit 0
}

$backupPath = Backup-McpConfig -Path $McpConfigPath
$config = Read-McpConfigJson -Path $McpConfigPath

$hadEntry = $false
if ($config.mcpServers -and ($config.mcpServers.PSObject.Properties.Name -contains $EntryName)) {
    $hadEntry = $true
}

$config = Remove-WindowsServerAdminMcpEntry -ConfigObject $config -EntryName $EntryName
Write-McpConfigJson -Path $McpConfigPath -ConfigObject $config

if ($RemoveLauncher) {
    foreach ($path in @(
            (Join-Path $ProjectRoot 'scripts\launch.ps1')
            (Join-Path $ProjectRoot 'scripts\launch.cmd')
        )) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
            Write-Host "Removed launcher: $path"
        }
    }
}

[ordered]@{
    success       = $true
    removed_entry = $hadEntry
    entry_name    = $EntryName
    mcp_config    = $McpConfigPath
    backup        = $backupPath
    cursor_restart_recommended = $true
} | ConvertTo-Json -Depth 5

if ($hadEntry) {
    Write-Host "Removed '$EntryName' from $McpConfigPath" -ForegroundColor Green
} else {
    Write-Host "'$EntryName' was not registered in $McpConfigPath" -ForegroundColor Yellow
}

Write-Host 'Restart Cursor to apply changes.' -ForegroundColor Yellow
