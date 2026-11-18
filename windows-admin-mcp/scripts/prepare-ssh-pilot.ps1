#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Prepare a pilot Windows Server for PowerShell remoting over OpenSSH.
.NOTES
    Run on ONE pilot server. Review with -WhatIf before applying.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ServiceAccountSam,
    [Parameter(Mandatory = $true)][string]$ClientSourceCidr,
    [Parameter(Mandatory = $true)][string]$AuthorizedKeyPath,
    [string]$BackupRoot = 'C:\ProgramData\McpRemoting\rollback'
)

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path $BackupRoot $timestamp
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

if ($PSCmdlet.ShouldProcess('OpenSSH Server', 'Install and configure')) {
    if (Test-Path 'C:\ProgramData\ssh\sshd_config') {
        Copy-Item 'C:\ProgramData\ssh\sshd_config' (Join-Path $backupDir 'sshd_config.bak')
    }

    $capability = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
    if ($capability.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name $capability.Name
    }

    $content = Get-Content 'C:\ProgramData\ssh\sshd_config' -Raw
    $replacements = @{
        '(?m)^#?\s*PasswordAuthentication\s+.*$' = 'PasswordAuthentication no'
        '(?m)^#?\s*PubkeyAuthentication\s+.*$' = 'PubkeyAuthentication yes'
    }
    foreach ($pattern in $replacements.Keys) {
        if ($content -match $pattern) {
            $content = [regex]::Replace($content, $pattern, $replacements[$pattern])
        } else {
            $content += "`n$($replacements[$pattern])"
        }
    }
    Set-Content -Path 'C:\ProgramData\ssh\sshd_config' -Value $content -Encoding ascii

    if (-not (Get-NetFirewallRule -DisplayName 'MCP-SSH-Inbound' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'MCP-SSH-Inbound' -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort 22 -RemoteAddress $ClientSourceCidr | Out-Null
    }

    Set-Service sshd -StartupType Automatic
    Restart-Service sshd

    Write-Output @{ success = $true; backupDir = $backupDir; message = 'SSH pilot prep completed' } | ConvertTo-Json
}
