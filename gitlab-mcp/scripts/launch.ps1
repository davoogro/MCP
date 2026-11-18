# Launch @zereight/mcp-gitlab for Cursor (stdio MCP)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root '.env'

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Error "Missing .env at $Path. Copy .env.example to .env and configure GitLab auth."
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq '' -or $line.StartsWith('#')) { return }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { return }
        $name = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

Import-DotEnv -Path $EnvFile

if (-not $env:GITLAB_API_URL) {
    $env:GITLAB_API_URL = 'https://gitlab.example.com/api/v4'
}

$useOAuth = ($env:GITLAB_USE_OAUTH -eq 'true')
$hasPat = -not [string]::IsNullOrWhiteSpace($env:GITLAB_PERSONAL_ACCESS_TOKEN)

if (-not $useOAuth -and -not $hasPat) {
    Write-Error @"
GitLab auth not configured.

SAML/SSO only applies to the browser login. The MCP talks to the GitLab REST API and needs either:
  A) GITLAB_PERSONAL_ACCESS_TOKEN in .env  (simplest)
  B) GITLAB_USE_OAUTH=true + run scripts\auth-oauth.ps1 once  (good for SAML)

See README.md in $Root
"@
}

$npxCandidates = @(
    'C:\Program Files\nodejs\npx.cmd',
    (Join-Path $env:APPDATA 'npm\npx.cmd')
)
$npx = $npxCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $npx) {
    $cmd = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if ($cmd) { $npx = $cmd.Source }
}

$localServer = Join-Path $Root 'node_modules\@zereight\mcp-gitlab\build\index.js'
$node = 'C:\Program Files\nodejs\node.exe'
if (-not (Test-Path $node)) {
    $nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($nodeCmd) { $node = $nodeCmd.Source }
}

if (Test-Path $localServer) {
    if (-not $node) { Write-Error 'Node.js not found. Install Node 18+ from https://nodejs.org.' }
    & $node $localServer
} elseif ($npx) {
    & $npx -y @zereight/mcp-gitlab@latest
} else {
    Write-Error 'Neither local @zereight/mcp-gitlab nor npx found. Run npm install in C:\DVB\mcp\gitlab-mcp.'
}
