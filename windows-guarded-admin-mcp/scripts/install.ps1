#Requires -Version 5.1
<#
.SYNOPSIS
    Installs windows-guarded-admin-mcp into Cursor global MCP configuration.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$McpConfigPath,
    [switch]$SkipValidation,
    [switch]$SkipUvSync
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
}

. (Join-Path $ScriptRoot 'lib\McpConfig.ps1')

function Write-InstallLog {
    param([string]$Message)
    Write-Host $Message
}

if (-not $McpConfigPath) {
    $McpConfigPath = Get-ActiveCursorMcpConfigPath
}

Write-InstallLog "Project root: $ProjectRoot"
Write-InstallLog "MCP config:   $McpConfigPath"

$cursorInfo = Get-CursorInstallInfo
if ($cursorInfo.executables.Count -eq 0) {
    Write-Warning 'Cursor executable not found in common locations. Continuing with MCP config registration.'
} else {
    foreach ($exe in $cursorInfo.executables) {
        Write-InstallLog ("Cursor: {0} ({1})" -f $exe.path, $exe.version)
    }
}

$candidates = Get-CursorMcpConfigCandidates
Write-InstallLog 'Detected MCP config candidates:'
foreach ($candidate in $candidates) {
    $exists = Test-Path -LiteralPath $candidate
    Write-InstallLog ("  [{0}] {1}" -f $(if ($exists) { 'x' } else { ' ' }), $candidate)
}

$backupPath = Backup-McpConfig -Path $McpConfigPath
if ($backupPath) {
    Write-InstallLog "Backup created: $backupPath"
} else {
    Write-InstallLog 'No existing MCP config to backup (will create new file).'
}

$uvPath = Resolve-UvPath
if (-not $SkipUvSync) {
    if ($uvPath -and (Test-Path -LiteralPath (Join-Path $ProjectRoot 'pyproject.toml'))) {
        Write-InstallLog "Running uv sync via $uvPath"
        & $uvPath sync --directory $ProjectRoot
        if ($LASTEXITCODE -ne 0) {
            throw "uv sync failed with exit code $LASTEXITCODE"
        }
    } else {
        Write-Warning 'uv not found or pyproject.toml missing; skipping uv sync.'
        $pythonPath = Resolve-ToolPath -Name 'python'
        if (-not $pythonPath) {
            $pythonPath = 'C:\DVB\python\python.exe'
        }
        if ($pythonPath -and (Test-Path -LiteralPath (Join-Path $ProjectRoot 'pyproject.toml'))) {
            Write-InstallLog "Attempting pip editable install via $pythonPath"
            & $pythonPath -m pip install -e $ProjectRoot
        }
    }
}

$launcherPath = Join-Path $ProjectRoot 'scripts\launch.ps1'
$uvForLauncher = if ($uvPath) { $uvPath } else { 'uv' }
$launcherContent = @"
#Requires -Version 5.1
Set-StrictMode -Version Latest
`$ProjectRoot = '$ProjectRoot'
`$UvPath = '$uvForLauncher'
`$env:WSA_MCP_CONFIG_DIR = Join-Path `$ProjectRoot 'config'

if (Test-Path -LiteralPath `$UvPath) {
    & `$UvPath run --directory `$ProjectRoot windows-guarded-admin-mcp
    exit `$LASTEXITCODE
}

`$python = '$((Resolve-ToolPath -Name 'python'))'
if (-not `$python -or -not (Test-Path -LiteralPath `$python)) {
    `$python = 'C:\DVB\python\python.exe'
}
if (`$python -and (Test-Path -LiteralPath `$python)) {
    Set-Location `$ProjectRoot
    & `$python -m windows_guarded_admin_mcp.server
    exit `$LASTEXITCODE
}

Write-Error 'Neither uv nor python found for windows-guarded-admin-mcp launcher.'
exit 1
"@
Set-Content -LiteralPath $launcherPath -Value $launcherContent -Encoding UTF8
Write-InstallLog "Launcher written: $launcherPath"

$launcherCmd = Join-Path $ProjectRoot 'scripts\launch.cmd'
$cmdContent = "@echo off`r`npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$launcherPath`"`r`nexit /b %ERRORLEVEL%`r`n"
Set-Content -LiteralPath $launcherCmd -Value $cmdContent -Encoding ASCII
Write-InstallLog "Launcher cmd written: $launcherCmd"

$config = Read-McpConfigJson -Path $McpConfigPath
$config = Merge-WindowsServerAdminMcpEntry -ConfigObject $config -ProjectRoot $ProjectRoot -UvPath $uvPath -LauncherPath $launcherPath
Write-McpConfigJson -Path $McpConfigPath -ConfigObject $config
Write-InstallLog "Registered MCP entry 'windows-guarded-admin' in $McpConfigPath"

$validationScript = Join-Path $ProjectRoot 'scripts\validate_mcp.ps1'
if (-not $SkipValidation -and (Test-Path -LiteralPath $validationScript)) {
    Write-InstallLog 'Running validate_mcp.ps1 ...'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $validationScript -ProjectRoot $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "MCP validation reported issues (exit $LASTEXITCODE)."
    }
}

Write-Host ''
Write-Host 'Installation complete.' -ForegroundColor Green
Write-Host 'Restart Cursor (or reload MCP servers) to pick up the new configuration.' -ForegroundColor Yellow

[ordered]@{
    success      = $true
    project_root = $ProjectRoot
    mcp_config   = $McpConfigPath
    backup       = $backupPath
    launcher     = $launcherPath
    entry_name   = 'windows-guarded-admin'
    cursor_restart_recommended = $true
} | ConvertTo-Json -Depth 5
