param(
    [hashtable]$Parameters = @{}
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$driveLetters = @()
if ($Parameters.ContainsKey('drive_letters') -and $Parameters['drive_letters']) {
    $driveLetters = [string[]]($Parameters['drive_letters'] -split ',')
}

& "$PSScriptRoot\Get-McpDisks.ps1" -DriveLetters $driveLetters
