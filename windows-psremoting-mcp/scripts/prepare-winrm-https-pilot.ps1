#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Prepare a pilot Windows Server for WinRM over HTTPS (5986).
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ListenerFqdn,
    [Parameter(Mandatory = $true)][string]$ClientSourceCidr,
    [Parameter(Mandatory = $true)][string]$CertificateThumbprint,
    [string]$BackupRoot = 'C:\ProgramData\McpRemoting\rollback'
)

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDir = Join-Path $BackupRoot $timestamp
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

if ($PSCmdlet.ShouldProcess('WinRM HTTPS listener', 'Configure')) {
    winrm get winrm/config | Out-File (Join-Path $backupDir 'winrm-config.txt')

    winrm set winrm/config/service/auth '@{Basic="false";Kerberos="true";Negotiate="true"}'
    winrm set winrm/config/service '@{AllowUnencrypted="false"}'

    Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Transport -eq 'HTTP' } | Remove-Item -Force

    $existing = Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Transport -eq 'HTTPS' -and $_.Port -eq 5986 }
    if (-not $existing) {
        winrm create "winrm/config/Listener?Address=*+Transport=HTTPS" `
            "@{Hostname=`"$ListenerFqdn`";CertificateThumbprint=`"$CertificateThumbprint`";Port=`"5986`"}"
    }

    if (-not (Get-NetFirewallRule -DisplayName 'MCP-WinRM-HTTPS-Inbound' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'MCP-WinRM-HTTPS-Inbound' -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort 5986 -RemoteAddress $ClientSourceCidr | Out-Null
    }

    if (Get-NetFirewallRule -DisplayName 'WINRM-HTTP-In-TCP' -ErrorAction SilentlyContinue) {
        Disable-NetFirewallRule -DisplayName 'WINRM-HTTP-In-TCP'
    }

    Write-Output @{
        success = $true
        backupDir = $backupDir
        listeners = (Get-ChildItem WSMan:\localhost\Listener | Select-Object Name, Transport, Port)
    } | ConvertTo-Json -Depth 4
}
