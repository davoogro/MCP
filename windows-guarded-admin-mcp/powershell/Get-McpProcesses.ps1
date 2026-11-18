param(
    [int]$Top = 10,
    [string]$NameFilter = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$procs = Get-Process | Sort-Object WorkingSet64 -Descending
if ($NameFilter) {
    $procs = $procs | Where-Object { $_.ProcessName -like "*$NameFilter*" }
}

$processes = $procs | Select-Object -First $Top | ForEach-Object {
    [pscustomobject]@{
        name         = $_.ProcessName
        id           = $_.Id
        cpuSeconds   = [math]::Round($_.CPU, 2)
        workingSetMb = [math]::Round($_.WorkingSet64 / 1MB, 2)
    }
}

@{ success = $true; data = @{ processes = $processes } } | ConvertTo-Json -Depth 6 -Compress
