#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$BackupDir
)

if ($PSCmdlet.ShouldProcess('OpenSSH Server', 'Rollback')) {
    if (Test-Path (Join-Path $BackupDir 'sshd_config.bak')) {
        Copy-Item (Join-Path $BackupDir 'sshd_config.bak') 'C:\ProgramData\ssh\sshd_config' -Force
    }
    Stop-Service sshd -Force -ErrorAction SilentlyContinue
    Set-Service sshd -StartupType Disabled -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName 'MCP-SSH-Inbound' -ErrorAction SilentlyContinue
    @{ success = $true; rolledBack = 'ssh' } | ConvertTo-Json
}
