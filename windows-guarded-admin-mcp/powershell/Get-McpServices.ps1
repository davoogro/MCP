param(
    [string[]]$ServiceNames = @(),
    [string]$NameFilter = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($ServiceNames -and $ServiceNames.Count -gt 0) {
    $services = foreach ($name in $ServiceNames) {
        $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($svc) {
            [pscustomobject]@{ name = $svc.Name; status = $svc.Status.ToString(); displayName = $svc.DisplayName }
        } else {
            [pscustomobject]@{ name = $name; status = 'NotFound'; displayName = $null }
        }
    }
} else {
    $all = Get-Service
    if ($NameFilter) {
        $all = $all | Where-Object { $_.DisplayName -like "*$NameFilter*" -or $_.Name -like "*$NameFilter*" }
    }
    $services = $all | Select-Object -First 200 | ForEach-Object {
        [pscustomobject]@{ name = $_.Name; status = $_.Status.ToString(); displayName = $_.DisplayName }
    }
}

@{ success = $true; data = @{ services = $services } } | ConvertTo-Json -Depth 6 -Compress
