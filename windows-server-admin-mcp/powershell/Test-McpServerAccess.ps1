param(
    [string]$RequestJson = '{}'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$request = $RequestJson | ConvertFrom-Json
$server = if ($request.server) { $request.server.hostname } else { $env:COMPUTERNAME }
$port = if ($request.server -and $request.server.port) { [int]$request.server.port } else { 5986 }

$reachable = $false
$latencyMs = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $tcp = Test-NetConnection -ComputerName $server -Port $port -WarningAction SilentlyContinue
    $reachable = [bool]$tcp.TcpTestSucceeded
} catch {
    $reachable = $false
}
$sw.Stop()

@{
    success = $true
    data    = @{
        reachable     = $reachable
        hostname      = $server
        port          = $port
        latencyMs     = $sw.ElapsedMilliseconds
        transportUsed = 'winrm'
    }
} | ConvertTo-Json -Depth 6 -Compress
