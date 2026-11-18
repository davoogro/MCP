param(
    [Parameter(Mandatory = $true)]
    [string]$ServiceName,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($DryRun) {
    @{
        success = $true
        data    = @{
            dryRun      = $true
            serviceName = $ServiceName
            action      = 'Would restart service'
        }
    } | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

$svc = Get-Service -Name $ServiceName -ErrorAction Stop
$before = $svc.Status.ToString()
Restart-Service -Name $ServiceName -Force -ErrorAction Stop
Start-Sleep -Seconds 2
$after = (Get-Service -Name $ServiceName).Status.ToString()

@{
    success = $true
    data    = [pscustomobject]@{
        serviceName  = $ServiceName
        statusBefore = $before
        statusAfter  = $after
    }
} | ConvertTo-Json -Depth 6 -Compress
