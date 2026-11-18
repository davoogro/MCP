param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem

@{
    success = $true
    data    = [pscustomobject]@{
        computerName = $env:COMPUTERNAME
        domain       = $cs.Domain
        osName       = $os.Caption
        osVersion    = $os.Version
        collectedUtc = (Get-Date).ToUniversalTime().ToString('o')
    }
} | ConvertTo-Json -Depth 6 -Compress
