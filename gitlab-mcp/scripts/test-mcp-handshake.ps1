# MCP stdio handshake smoke: initialize + tools/list. No token printed.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Get-Content (Join-Path $Root '.env') | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq '' -or $line.StartsWith('#')) { return }
    $idx = $line.IndexOf('=')
    if ($idx -lt 1) { return }
    [Environment]::SetEnvironmentVariable($line.Substring(0, $idx).Trim(), $line.Substring($idx + 1).Trim(), 'Process')
}

$server = Join-Path $Root 'node_modules\@zereight\mcp-gitlab\build\index.js'
$node = 'C:\Program Files\nodejs\node.exe'
$init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"gitlab-mcp-smoke","version":"0.0.1"}}}'
$inited = '{"jsonrpc":"2.0","method":"notifications/initialized"}'
$list = '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $node
$psi.Arguments = "`"$server`""
$psi.WorkingDirectory = $Root
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
foreach ($key in @('GITLAB_API_URL','GITLAB_PERSONAL_ACCESS_TOKEN','GITLAB_PERMISSION_MODE')) {
    if ($env:($key)) { $psi.Environment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }
}

$p = New-Object System.Diagnostics.Process
$p.StartInfo = $psi
[void]$p.Start()
$p.StandardInput.WriteLine($init)
$p.StandardInput.WriteLine($inited)
$p.StandardInput.WriteLine($list)
$p.StandardInput.Close()

$out = New-Object System.Text.StringBuilder
$err = New-Object System.Text.StringBuilder
$deadline = [DateTime]::UtcNow.AddSeconds(20)
while (-not $p.HasExited -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 200
}
if (-not $p.HasExited) { $p.Kill() }

$stdout = $p.StandardOutput.ReadToEnd()
$stderr = $p.StandardError.ReadToEnd()
Write-Host '--- STDERR ---'
Write-Host ($stderr.Substring(0, [Math]::Min(1500, $stderr.Length)))
Write-Host '--- STDOUT lines ---'
$stdout -split "`n" | Select-Object -First 8 | ForEach-Object {
    if ($_ -match '"result"') { Write-Host 'GOT_JSONRPC_RESULT' }
    elseif ($_ -match 'error') { Write-Host $_ }
    else { Write-Host $_.Substring(0, [Math]::Min(200, $_.Length)) }
}
