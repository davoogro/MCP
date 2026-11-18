param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$pending = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'

@{
    success = $true
    data    = [pscustomobject]@{
        computerName    = $env:COMPUTERNAME
        osName          = $os.Caption
        osVersion       = $os.Version
        lastBootTimeUtc = $os.LastBootUpTime.ToUniversalTime().ToString('o')
        totalMemoryGb   = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
        freeMemoryGb    = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
        cpuLoadPercent  = $cpu.LoadPercentage
        pendingReboot   = [bool]$pending
    }
} | ConvertTo-Json -Depth 6 -Compress
