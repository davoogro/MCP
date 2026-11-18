param(
    [string[]]$DriveLetters = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$volumes = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'
if ($DriveLetters -and $DriveLetters.Count -gt 0) {
    $volumes = $volumes | Where-Object { $DriveLetters -contains $_.DeviceID }
}

$disks = $volumes | ForEach-Object {
    [pscustomobject]@{
        driveLetter = $_.DeviceID
        sizeGb      = [math]::Round($_.Size / 1GB, 2)
        freeGb      = [math]::Round($_.FreeSpace / 1GB, 2)
        usedPercent = if ($_.Size -gt 0) { [math]::Round((($_.Size - $_.FreeSpace) / $_.Size) * 100, 2) } else { 0 }
    }
}

@{ success = $true; data = @{ disks = $disks } } | ConvertTo-Json -Depth 6 -Compress
