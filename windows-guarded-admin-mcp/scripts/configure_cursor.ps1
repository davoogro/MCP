#Requires -Version 5.1
<#
.SYNOPSIS
    Merges the windows-guarded-admin MCP entry into Cursor mcp.json without touching other servers.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$McpConfigPath,
    [string]$EntryName = 'windows-guarded-admin'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
}

. (Join-Path $ScriptRoot 'lib\McpConfig.ps1')

if (-not $McpConfigPath) {
    $McpConfigPath = Get-ActiveCursorMcpConfigPath
}

$backupPath = Backup-McpConfig -Path $McpConfigPath
$config = Read-McpConfigJson -Path $McpConfigPath
$launcherPath = Join-Path $ProjectRoot 'scripts\launch.ps1'
$uvPath = Resolve-UvPath

$config = Merge-WindowsServerAdminMcpEntry -ConfigObject $config -ProjectRoot $ProjectRoot -UvPath $uvPath -LauncherPath $launcherPath -EntryName $EntryName
Write-McpConfigJson -Path $McpConfigPath -ConfigObject $config

[ordered]@{
    success    = $true
    entry_name = $EntryName
    mcp_config = $McpConfigPath
    backup     = $backupPath
    cursor_restart_recommended = $true
} | ConvertTo-Json -Depth 5

Write-Host "Merged '$EntryName' into $McpConfigPath" -ForegroundColor Green
Write-Host 'Restart Cursor to reload MCP servers.' -ForegroundColor Yellow
