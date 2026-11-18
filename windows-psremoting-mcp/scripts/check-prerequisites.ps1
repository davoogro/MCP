#Requires -Version 5.1
<#
.SYNOPSIS
    Validates local prerequisites for windows-psremoting-mcp.
#>
[CmdletBinding()]
param()

$checks = @()

function Add-Check {
    param([string]$Name, [bool]$Success, [string]$Detail)
    $checks += [pscustomobject]@{ name = $Name; success = $Success; detail = $Detail }
}

$node = Get-Command node -ErrorAction SilentlyContinue
Add-Check 'node_installed' ($null -ne $node) ($(if ($node) { (& node --version) } else { 'node not found' }))

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
Add-Check 'pwsh_installed' ($null -ne $pwsh) ($(if ($pwsh) { $pwsh.Source } else { 'pwsh not found' }))

$trustedHosts = (Get-Item WSMan:\localhost\Client\TrustedHosts -ErrorAction SilentlyContinue).Value
$trustedOk = [string]::IsNullOrWhiteSpace($trustedHosts) -or ($trustedHosts -ne '*')
Add-Check 'trusted_hosts_not_wildcard' $trustedOk "TrustedHosts='$trustedHosts'"

$config = $env:WINMCP_CONFIG
Add-Check 'winmcp_config_set' (-not [string]::IsNullOrWhiteSpace($config)) $(if ($config) { $config } else { 'WINMCP_CONFIG not set' })
if ($config -and (Test-Path $config)) {
    Add-Check 'winmcp_config_exists' $true $config
} elseif ($config) {
    Add-Check 'winmcp_config_exists' $false "Missing file: $config"
}

$checks | ConvertTo-Json -Depth 4
if ($checks.success -contains $false) { exit 1 }
exit 0
