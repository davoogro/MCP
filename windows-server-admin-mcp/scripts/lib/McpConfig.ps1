#Requires -Version 5.1
Set-StrictMode -Version Latest

function Get-ProjectRoot {
    param([string]$StartPath)
    if (-not $StartPath) {
        $StartPath = Split-Path -Parent $MyInvocation.PSCommandPath
    }
    $root = Resolve-Path (Join-Path $StartPath '..\..') -ErrorAction Stop
    return $root.Path
}

function Get-CursorMcpConfigCandidates {
    $userHome = $env:USERPROFILE
    $appData = $env:APPDATA
    $localAppData = $env:LOCALAPPDATA

    @(
        (Join-Path $userHome '.cursor\mcp.json')
        (Join-Path $appData 'Cursor\User\globalStorage\cursor.mcp\mcp.json')
        (Join-Path $localAppData 'Cursor\User\globalStorage\cursor.mcp\mcp.json')
        (Join-Path $appData 'Cursor\User\mcp.json')
        (Join-Path $localAppData 'Programs\cursor\resources\app\mcp.json')
    ) | Select-Object -Unique
}

function Get-ActiveCursorMcpConfigPath {
    foreach ($path in (Get-CursorMcpConfigCandidates)) {
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }
    return (Join-Path $env:USERPROFILE '.cursor\mcp.json')
}

function Get-CursorInstallInfo {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\cursor\Cursor.exe')
        (Join-Path $env:ProgramFiles 'Cursor\Cursor.exe')
        "${env:ProgramFiles(x86)}\Cursor\Cursor.exe"
    )

    $found = @()
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) {
            $version = $null
            try {
                $version = (Get-Item -LiteralPath $path).VersionInfo.FileVersion
            } catch {
                $version = 'unknown'
            }
            $found += [ordered]@{
                path    = $path
                version = $version
            }
        }
    }

    $where = @()
    try {
        $where = @(where.exe cursor 2>$null)
    } catch { }

    return [ordered]@{
        executables = $found
        where_paths = $where
    }
}

function Read-McpConfigJson {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ mcpServers = @{} }
    }

    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return [pscustomobject]@{ mcpServers = @{} }
    }

    return ($raw | ConvertFrom-Json)
}

function Backup-McpConfig {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$BackupRoot = (Join-Path (Get-ProjectRoot) 'backups\cursor-mcp')
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    if (-not (Test-Path -LiteralPath $BackupRoot)) {
        New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupPath = Join-Path $BackupRoot ("mcp.json.bak-{0}" -f $stamp)
    Copy-Item -LiteralPath $Path -Destination $backupPath -Force
    return $backupPath
}

function ConvertTo-McpServersHashtable {
    param($McpServers)

    $servers = [ordered]@{}
    if ($null -eq $McpServers) {
        return $servers
    }
    if ($McpServers -is [System.Collections.IDictionary]) {
        foreach ($key in $McpServers.Keys) {
            $servers[[string]$key] = $McpServers[$key]
        }
        return $servers
    }
    foreach ($prop in $McpServers.PSObject.Properties) {
        $servers[$prop.Name] = $prop.Value
    }
    return $servers
}

function Merge-WindowsServerAdminMcpEntry {
    param(
        [Parameter(Mandatory)]$ConfigObject,
        [Parameter(Mandatory)][string]$ProjectRoot,
        [string]$EntryName = 'windows-server-admin',
        [string]$UvPath,
        [string]$LauncherPath
    )

    $ConfigObject.mcpServers = ConvertTo-McpServersHashtable -McpServers $ConfigObject.mcpServers

    $configDir = Join-Path $ProjectRoot 'config'
    $envBlock = [ordered]@{
        WSA_MCP_CONFIG_DIR = $configDir
    }

    if ($LauncherPath -and (Test-Path -LiteralPath $LauncherPath)) {
        $entry = [ordered]@{
            command = 'powershell.exe'
            args    = @(
                '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', $LauncherPath
            )
            env     = $envBlock
        }
    }
    elseif ($UvPath -and (Test-Path -LiteralPath $UvPath)) {
        $entry = [ordered]@{
            command = $UvPath
            args    = @(
                'run', '--directory', $ProjectRoot, 'windows-server-admin-mcp'
            )
            env     = $envBlock
        }
    }
    else {
        $python = Resolve-ToolPath -Name 'python'
        if (-not $python) {
            $python = 'C:\DVB\python\python.exe'
        }
        $entry = [ordered]@{
            command = $python
            args    = @('-m', 'windows_server_admin_mcp.server')
            env     = $envBlock
        }
    }

    $ConfigObject.mcpServers[$EntryName] = $entry
    return $ConfigObject
}

function Remove-WindowsServerAdminMcpEntry {
    param(
        [Parameter(Mandatory)]$ConfigObject,
        [string]$EntryName = 'windows-server-admin'
    )

    $ConfigObject.mcpServers = ConvertTo-McpServersHashtable -McpServers $ConfigObject.mcpServers
    if ($ConfigObject.mcpServers.Contains($EntryName)) {
        $ConfigObject.mcpServers.Remove($EntryName)
    }

    return $ConfigObject
}

function Write-McpConfigJson {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]$ConfigObject
    )

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    if ($ConfigObject.mcpServers) {
        $ConfigObject.mcpServers = ConvertTo-McpServersHashtable -McpServers $ConfigObject.mcpServers
    }

    $payload = [ordered]@{
        mcpServers = $ConfigObject.mcpServers
    }
    $json = $payload | ConvertTo-Json -Depth 20
    Set-Content -LiteralPath $Path -Value $json -Encoding UTF8
}

function Resolve-ToolPath {
    param([Parameter(Mandatory)][string]$Name)

    $candidates = @(
        (Join-Path 'C:\DVB\python' "$Name.exe")
        (Join-Path $env:LOCALAPPDATA "Programs\$Name\$Name.exe")
        (Join-Path $env:ProgramFiles "Git\cmd\$Name.exe")
        (Join-Path $env:ProgramFiles "Git\bin\$Name.exe")
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\$Name.exe")
        (Join-Path $env:USERPROFILE ".local\bin\$Name.exe")
        (Join-Path $env:USERPROFILE ".cargo\bin\$Name.exe")
    )

    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }

    try {
        $cmd = Get-Command $Name -ErrorAction Stop
        return $cmd.Source
    } catch {
        return $null
    }
}

function Resolve-UvPath {
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe')
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
    )

    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) {
            return $path
        }
    }

    try {
        return (Get-Command uv -ErrorAction Stop).Source
    } catch {
        return $null
    }
}
