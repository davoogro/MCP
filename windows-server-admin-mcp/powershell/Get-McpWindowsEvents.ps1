param(
    [string]$LogName = 'System',
    [int]$Hours = 24,
    [string]$Level = 'Error',
    [int]$MaxEvents = 50
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$since = (Get-Date).AddHours(-1 * $Hours)
$levelId = switch ($Level) {
    'Critical' { 1 }
    'Error' { 2 }
    'Warning' { 3 }
    default { 4 }
}

$events = Get-WinEvent -FilterHashtable @{
    LogName   = $LogName
    StartTime = $since
    Level     = $levelId
} -MaxEvents $MaxEvents -ErrorAction SilentlyContinue | ForEach-Object {
    $msg = $_.Message
    if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
    [pscustomobject]@{
        timeCreated = $_.TimeCreated.ToUniversalTime().ToString('o')
        id          = $_.Id
        level       = $_.LevelDisplayName
        provider    = $_.ProviderName
        message     = $msg
    }
}

@{ success = $true; data = @{ events = $events } } | ConvertTo-Json -Depth 6 -Compress
