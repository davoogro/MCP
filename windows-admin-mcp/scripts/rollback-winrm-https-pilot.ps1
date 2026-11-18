#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$BackupDir
)

if ($PSCmdlet.ShouldProcess('WinRM HTTPS listener', 'Rollback')) {
    $configBackup = Join-Path $BackupDir 'winrm-config.txt'
    if (Test-Path $configBackup) {
        Write-Warning "Manual review required: restore WinRM config from $configBackup"
    }
    Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Transport -eq 'HTTPS' -and $_.Port -eq 5986 } | Remove-Item -Force
    Remove-NetFirewallRule -DisplayName 'MCP-WinRM-HTTPS-Inbound' -ErrorAction SilentlyContinue
    @{ success = $true; rolledBack = 'winrm_https'; note = 'Review winrm-config.txt backup for full restore' } | ConvertTo-Json
}
