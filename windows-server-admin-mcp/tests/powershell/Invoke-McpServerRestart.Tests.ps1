# Pester 3.x compatible tests for Invoke-McpServerRestart.ps1 shutdown argument construction.
# Run: Invoke-Pester -Path tests/powershell

$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$RestartScript = Join-Path $ProjectRoot 'powershell\Invoke-McpServerRestart.ps1'
$RemoteScript = Join-Path $ProjectRoot 'powershell\Invoke-McpRemoteOperation.ps1'

if (-not (Test-Path $RestartScript)) {
    throw "Restart script not found: $RestartScript"
}

$RestartSource = Get-Content -Path $RestartScript -Raw

Describe 'Invoke-McpServerRestart shutdown arguments' {
    It 'builds /r /m /t /c without /f by default' {
        $RestartSource | Should Match "\`$args = @\('/r', '/m'"
        $RestartSource | Should Match "if \(\`$Force\) \{ \`$args \+= '/f' \}"
    }

    It 'uses Start-Process with ArgumentList not shell invocation' {
        $RestartSource | Should Match "Start-Process -FilePath 'shutdown\.exe' -ArgumentList"
        $RestartSource | Should Not Match 'shell\s*=\s*\$true'
    }

    It 'supports cancel via /a' {
        $RestartSource | Should Match "'/a'"
    }

    It 'declares Force as optional switch' {
        $RestartSource | Should Match '\[switch\]\$Force'
    }
}

Describe 'Invoke-McpRemoteOperation integrated auth hygiene' {
    It 'does not contain hardcoded password literals' {
        if (Test-Path $RemoteScript) {
            $content = Get-Content -Path $RemoteScript -Raw
            $content | Should Not Match 'password\s*=\s*[''"].{8,}[''"]'
        }
    }

    It 'uses current_windows_identity without credential parameters' {
        if (Test-Path $RemoteScript) {
            $content = Get-Content -Path $RemoteScript -Raw
            $content | Should Match 'current_windows_identity'
            $content | Should Not Match 'Get-McpCredential'
            $content | Should Not Match 'ConvertTo-SecureString'
            $content | Should Not Match '\-Credential'
        }
    }
}
