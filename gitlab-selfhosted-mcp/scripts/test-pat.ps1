# Smoke-test GitLab API using .env PAT. Never prints the token.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root '.env'

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq '' -or $line.StartsWith('#')) { return }
    $idx = $line.IndexOf('=')
    if ($idx -lt 1) { return }
    $name = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim()
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}

$api = $env:GITLAB_API_URL
$token = $env:GITLAB_PERSONAL_ACCESS_TOKEN
if ([string]::IsNullOrWhiteSpace($api)) { throw 'GITLAB_API_URL missing' }
if ([string]::IsNullOrWhiteSpace($token)) { throw 'GITLAB_PERSONAL_ACCESS_TOKEN missing' }

Write-Host "API=$api"
Write-Host "PAT_PRESENT=true"
$headers = @{ 'PRIVATE-TOKEN' = $token }

try {
    $user = Invoke-RestMethod -Uri "$api/user" -Headers $headers -Method Get
    Write-Host 'AUTH_OK'
    Write-Host ("user_id={0} username={1} name={2}" -f $user.id, $user.username, $user.name)
} catch {
    Write-Host 'AUTH_FAIL'
    Write-Host $_.Exception.Message
    if ($_.Exception.Response) {
        Write-Host ("http_status={0}" -f [int]$_.Exception.Response.StatusCode)
    }
    exit 1
}

try {
    $projects = Invoke-RestMethod -Uri "$api/projects?membership=true&per_page=5&simple=true" -Headers $headers -Method Get
    Write-Host "PROJECTS_OK count=$($projects.Count)"
    $projects | ForEach-Object {
        Write-Host ("- {0} (id={1})" -f $_.path_with_namespace, $_.id)
    }
} catch {
    Write-Host 'PROJECTS_FAIL'
    Write-Host $_.Exception.Message
    exit 1
}
