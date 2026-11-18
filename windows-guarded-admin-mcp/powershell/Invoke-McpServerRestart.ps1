param(
    [Parameter(Mandatory = $true)]
    [string]$Hostname,
    [Parameter(Mandatory = $true)]
    [string]$Reason,
    [int]$DelaySeconds = 300,
    [switch]$Force,
    [switch]$Cancel
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Cancel) {
    $proc = Start-Process -FilePath 'shutdown.exe' -ArgumentList @('/a', '/m', "\\$Hostname") -Wait -PassThru -NoNewWindow
    @{
        success = $true
        data    = @{
            cancelled = $true
            hostname  = $Hostname
            exitCode  = $proc.ExitCode
        }
    } | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

$args = @('/r', '/m', "\\$Hostname", '/t', [string]$DelaySeconds, '/c', $Reason)
if ($Force) { $args += '/f' }

$proc = Start-Process -FilePath 'shutdown.exe' -ArgumentList $args -Wait -PassThru -NoNewWindow
if ($proc.ExitCode -ne 0) { throw "shutdown.exe failed with exit code $($proc.ExitCode)" }

@{
    success = $true
    data    = @{
        initiated         = $true
        rebootCommandSent = $true
        hostname          = $Hostname
        delaySeconds      = $DelaySeconds
        force             = [bool]$Force
        method            = 'shutdown.exe /r /m'
    }
} | ConvertTo-Json -Depth 6 -Compress
