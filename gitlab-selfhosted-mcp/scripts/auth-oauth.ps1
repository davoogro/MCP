# One-time OAuth device-flow auth for GitLab (works well with SAML/SSO in browser)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root '.env'

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Error "Missing .env at $Path. Copy .env.example to .env first."
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

if (-not $env:GITLAB_OAUTH_CLIENT_ID) {
    Write-Error 'Set GITLAB_OAUTH_CLIENT_ID in .env (GitLab → Preferences → Applications → Application ID).'
}

if (-not $env:GITLAB_API_URL) {
    $env:GITLAB_API_URL = 'https://gitlab.example.com/api/v4'
}

$tokenPath = if ($env:GITLAB_OAUTH_TOKEN_PATH) { $env:GITLAB_OAUTH_TOKEN_PATH } else { Join-Path $env:USERPROFILE '.gitlab-mcp-token.json' }

Write-Host ''
Write-Host 'GitLab OAuth device flow' -ForegroundColor Cyan
Write-Host '1. A URL and user code will appear below.'
Write-Host '2. Open the URL in a browser where you are already logged in via SAML.'
Write-Host '3. Authorize the application.'
Write-Host "4. Token will be saved to: $tokenPath"
Write-Host ''

$node = Join-Path $env:LOCALAPPDATA 'Programs\cursor\resources\app\resources\helpers\node.exe'
$npx = Join-Path (Split-Path $node) 'npx.cmd'

$authArgs = @(
    $npx, '-y', '@zereight/mcp-gitlab@latest', 'auth',
    '--client-id', $env:GITLAB_OAUTH_CLIENT_ID,
    '--api-url', $env:GITLAB_API_URL,
    '--token-path', $tokenPath
)

if ($env:GITLAB_OAUTH_CLIENT_SECRET) {
    $authArgs += @('--client-secret', $env:GITLAB_OAUTH_CLIENT_SECRET)
}

& $node @authArgs

Write-Host ''
Write-Host 'Next: ensure .env contains GITLAB_USE_OAUTH=true and restart the GitLab MCP in Cursor.' -ForegroundColor Green
