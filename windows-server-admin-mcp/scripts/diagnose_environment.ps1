#Requires -Version 5.1
<#
.SYNOPSIS
    Non-destructive local and remote environment diagnosis for windows-server-admin-mcp.
.DESCRIPTION
    Checks the local workstation and a remote integration server without modifying
    WinRM, firewall, TrustedHosts, or installing software. Uses integrated auth only.
#>
[CmdletBinding()]
param(
    [string]$RemoteServer = 'integration-server-01',
    [int]$PortTimeoutMs = 3000,
    [string]$ProjectRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot '..')).Path
}

. (Join-Path $ScriptRoot 'lib\McpConfig.ps1')

function New-DiagResult {
    param(
        [string]$Name,
        [ValidateSet('PASS', 'FAIL', 'WARN', 'INFO', 'SKIP')]
        [string]$Status,
        [string]$Detail = '',
        $Data = $null
    )
    return [ordered]@{
        name   = $Name
        status = $Status
        detail = $Detail
        data   = $Data
    }
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory)][string]$ComputerName,
        [Parameter(Mandatory)][int]$Port,
        [int]$TimeoutMs = 3000
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($ComputerName, $Port, $null, $null)
        $completed = $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if (-not $completed) {
            return [ordered]@{ open = $false; error = 'timeout' }
        }
        $client.EndConnect($async)
        return [ordered]@{ open = $true; error = $null }
    } catch {
        return [ordered]@{ open = $false; error = $_.Exception.Message }
    } finally {
        if ($client.Connected) { $client.Close() }
        $client.Dispose()
    }
}

function Get-ToolVersion {
    param(
        [string]$CommandPath,
        [string[]]$VersionArgs = @('--version')
    )

    if (-not $CommandPath) {
        return [ordered]@{ found = $false; version = $null; path = $null }
    }

    try {
        $output = & $CommandPath @VersionArgs 2>&1 | Out-String
        return [ordered]@{
            found   = $true
            version = ($output.Trim())
            path    = $CommandPath
        }
    } catch {
        return [ordered]@{
            found   = $false
            version = $_.Exception.Message
            path    = $CommandPath
        }
    }
}

function Find-ReferenceFiles {
    param([string[]]$Names, [string[]]$SearchRoots)

    $results = [ordered]@{}
    foreach ($name in $Names) {
        $hits = @()
        foreach ($root in $SearchRoots) {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            try {
                $items = Get-ChildItem -LiteralPath $root -Filter $name -Recurse -ErrorAction SilentlyContinue |
                    Where-Object { $_.FullName -notmatch '\\node_modules\\|\\\.venv\\|\\\.git\\' } |
                    Select-Object -First 5 -ExpandProperty FullName
                if ($items) { $hits += $items }
            } catch { }
        }
        $results[$name] = @($hits | Select-Object -Unique)
    }
    return $results
}

function Add-SummaryLine {
    param(
        [System.Collections.ArrayList]$Lines,
        [string]$Name,
        [string]$Status,
        [string]$Detail
    )
    $marker = switch ($Status) {
        'PASS' { '[PASS]' }
        'FAIL' { '[FAIL]' }
        'WARN' { '[WARN]' }
        default { '[INFO]' }
    }
    [void]$Lines.Add(("| {0} | {1} | {2} |" -f $Name, $marker, ($Detail -replace '\|', '/')))
}

$startedAt = (Get-Date).ToUniversalTime()
$results = [System.Collections.ArrayList]@()
$referenceNames = @('deploy_and_run.ps1', 'runremote.ps1', 'remotest.py', 'server_info.py', 'file2df.py')
$searchRoots = @('C:\DVB', (Join-Path $env:USERPROFILE 'Downloads\py'))

# --- Local checks ---
try {
    $os = Get-CimInstance Win32_OperatingSystem
    [void]$results.Add((New-DiagResult -Name 'Local Windows' -Status 'INFO' -Detail $os.Caption -Data @{
            caption      = $os.Caption
            version      = $os.Version
            build_number = $os.BuildNumber
            arch         = $os.OSArchitecture
        }))
} catch {
    [void]$results.Add((New-DiagResult -Name 'Local Windows' -Status 'FAIL' -Detail $_.Exception.Message))
}

[void]$results.Add((New-DiagResult -Name 'Local User' -Status 'INFO' -Detail "$env:USERDOMAIN\$env:USERNAME" -Data @{
        user   = $env:USERNAME
        domain = $env:USERDOMAIN
        upn    = "$env:USERDOMAIN\$env:USERNAME"
    }))

try {
    $cs = Get-CimInstance Win32_ComputerSystem
    $join = if ($cs.PartOfDomain) { "Domain: $($cs.Domain)" } else { "Workgroup: $($cs.Workgroup)" }
    [void]$results.Add((New-DiagResult -Name 'Domain/Workgroup' -Status 'INFO' -Detail $join -Data @{
            part_of_domain = [bool]$cs.PartOfDomain
            domain         = $cs.Domain
            workgroup      = $cs.Workgroup
        }))
} catch {
    [void]$results.Add((New-DiagResult -Name 'Domain/Workgroup' -Status 'WARN' -Detail $_.Exception.Message))
}

$ps51 = (powershell.exe -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' 2>&1 | Out-String).Trim()
[void]$results.Add((New-DiagResult -Name 'Windows PowerShell 5.1' -Status $(if ($ps51) { 'PASS' } else { 'FAIL' }) -Detail $ps51))

$pwshPath = Resolve-ToolPath -Name 'pwsh'
$pwshInfo = Get-ToolVersion -CommandPath $pwshPath
[void]$results.Add((New-DiagResult -Name 'PowerShell 7' -Status $(if ($pwshInfo.found) { 'PASS' } else { 'INFO' }) -Detail $(if ($pwshInfo.found) { $pwshInfo.version } else { 'not installed' }) -Data $pwshInfo))

$pythonPath = Resolve-ToolPath -Name 'python'
$pythonInfo = Get-ToolVersion -CommandPath $pythonPath
[void]$results.Add((New-DiagResult -Name 'Python' -Status $(if ($pythonInfo.found) { 'PASS' } else { 'FAIL' }) -Detail $(if ($pythonInfo.found) { $pythonInfo.version } else { 'not found' }) -Data $pythonInfo))

$uvPath = Resolve-UvPath
$uvInfo = Get-ToolVersion -CommandPath $uvPath
[void]$results.Add((New-DiagResult -Name 'uv' -Status $(if ($uvInfo.found) { 'PASS' } else { 'WARN' }) -Detail $(if ($uvInfo.found) { $uvInfo.version } else { 'not found' }) -Data $uvInfo))

$gitPath = Resolve-ToolPath -Name 'git'
$gitInfo = Get-ToolVersion -CommandPath $gitPath
[void]$results.Add((New-DiagResult -Name 'Git' -Status $(if ($gitInfo.found) { 'PASS' } else { 'WARN' }) -Detail $(if ($gitInfo.found) { $gitInfo.version } else { 'not found' }) -Data $gitInfo))

$cursorInfo = Get-CursorInstallInfo
$cursorStatus = if ($cursorInfo.executables.Count -gt 0) { 'PASS' } else { 'WARN' }
[void]$results.Add((New-DiagResult -Name 'Cursor' -Status $cursorStatus -Detail ($cursorInfo.executables | ForEach-Object { $_.path }) -join '; ' -Data $cursorInfo))

$mcpCandidates = Get-CursorMcpConfigCandidates
$mcpDetected = @()
foreach ($candidate in $mcpCandidates) {
    if (Test-Path -LiteralPath $candidate) {
        $cfg = Read-McpConfigJson -Path $candidate
        $servers = @()
        if ($cfg.mcpServers) {
            $servers = @($cfg.mcpServers.PSObject.Properties.Name)
        }
        $mcpDetected += [ordered]@{
            path    = $candidate
            exists  = $true
            servers = $servers
        }
    } else {
        $mcpDetected += [ordered]@{
            path    = $candidate
            exists  = $false
            servers = @()
        }
    }
}
$activeMcp = Get-ActiveCursorMcpConfigPath
[void]$results.Add((New-DiagResult -Name 'Cursor MCP config' -Status $(if ($mcpDetected | Where-Object { $_.exists }) { 'PASS' } else { 'WARN' }) -Detail $activeMcp -Data $mcpDetected))

try {
    $policy = Get-ExecutionPolicy -Scope CurrentUser
    $policyStatus = if ($policy -in @('RemoteSigned', 'Unrestricted', 'Bypass')) { 'PASS' } else { 'WARN' }
    [void]$results.Add((New-DiagResult -Name 'Execution policy (CurrentUser)' -Status $policyStatus -Detail $policy))
} catch {
    [void]$results.Add((New-DiagResult -Name 'Execution policy (CurrentUser)' -Status 'WARN' -Detail $_.Exception.Message))
}

try {
    if ($pythonPath) {
        $proc = Start-Process -FilePath $pythonPath -ArgumentList @('-c', "print('subprocess-ok')") -NoNewWindow -PassThru -Wait
        $subprocessOk = ($proc.ExitCode -eq 0)
    } else {
        $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', 'echo subprocess-ok') -NoNewWindow -PassThru -Wait
        $subprocessOk = ($proc.ExitCode -eq 0)
    }
    [void]$results.Add((New-DiagResult -Name 'Subprocess capability' -Status $(if ($subprocessOk) { 'PASS' } else { 'FAIL' }) -Detail $(if ($subprocessOk) { 'ok' } else { "exit $($proc.ExitCode)" })))
} catch {
    [void]$results.Add((New-DiagResult -Name 'Subprocess capability' -Status 'FAIL' -Detail $_.Exception.Message))
}

$referenceFiles = Find-ReferenceFiles -Names $referenceNames -SearchRoots $searchRoots
$refFoundCount = ($referenceFiles.GetEnumerator() | Where-Object { $_.Value.Count -gt 0 }).Count
[void]$results.Add((New-DiagResult -Name 'Reference files' -Status $(if ($refFoundCount -gt 0) { 'PASS' } else { 'WARN' }) -Detail ("found {0}/{1}" -f $refFoundCount, $referenceNames.Count) -Data $referenceFiles))

# --- Remote checks ---
$remoteSummary = [ordered]@{}
$integratedAuthPass = $false
$winrmPass = $false
$psRemotingPass = $false
$pssessionPass = $false
$smbPass = $false

$dnsResult = [ordered]@{ hostname = $RemoteServer; addresses = @(); error = $null }
try {
    $resolved = @([System.Net.Dns]::GetHostAddresses($RemoteServer) | ForEach-Object { $_.ToString() })
    $dnsResult.addresses = $resolved
    $dnsStatus = if ($resolved.Length -gt 0) { 'PASS' } else { 'FAIL' }
    [void]$results.Add((New-DiagResult -Name "Remote DNS ($RemoteServer)" -Status $dnsStatus -Detail ($resolved -join ', ') -Data $dnsResult))
} catch {
    $dnsResult.error = $_.Exception.Message
    [void]$results.Add((New-DiagResult -Name "Remote DNS ($RemoteServer)" -Status 'FAIL' -Detail $_.Exception.Message -Data $dnsResult))
}

$portChecks = [ordered]@{}
foreach ($port in @(5985, 5986, 445, 135)) {
    $target = if ($dnsResult.addresses.Length -gt 0) { $dnsResult.addresses[0] } else { $RemoteServer }
    $check = Test-TcpPort -ComputerName $target -Port $port -TimeoutMs $PortTimeoutMs
    $portChecks["$port"] = $check
    $portStatus = if ($check.open) { 'PASS' } else { 'FAIL' }
    [void]$results.Add((New-DiagResult -Name "Remote port $port ($RemoteServer)" -Status $portStatus -Detail $(if ($check.open) { 'open' } else { $check.error }) -Data $check))
}

try {
    $wsman = Test-WSMan -ComputerName $RemoteServer -ErrorAction Stop
    $winrmPass = $true
    [void]$results.Add((New-DiagResult -Name "Test-WSMan ($RemoteServer)" -Status 'PASS' -Detail ($wsman | Select-Object ProductVersion, ProtocolVersion | ConvertTo-Json -Compress) -Data $wsman))
} catch {
    [void]$results.Add((New-DiagResult -Name "Test-WSMan ($RemoteServer)" -Status 'FAIL' -Detail $_.Exception.Message))
}

$remoteIdentity = [ordered]@{}
$remotePython = [ordered]@{ found = $false; paths = @(); version = $null }
$session = $null
try {
    $session = New-PSSession -ComputerName $RemoteServer -ErrorAction Stop
    $pssessionPass = $true
    $remoteData = Invoke-Command -Session $session -ScriptBlock {
        [ordered]@{
            hostname   = hostname
            whoami     = whoami
            ps_version = $PSVersionTable.PSVersion.ToString()
        }
    }
    $remoteIdentity = $remoteData
    $integratedAuthPass = $true
    $psRemotingPass = $true
    $winrmPass = $true
    [void]$results.Add((New-DiagResult -Name "PSSession ($RemoteServer)" -Status 'PASS' -Detail ($remoteData | ConvertTo-Json -Compress) -Data $remoteData))

    try {
        $pyInfo = Invoke-Command -Session $session -ScriptBlock {
            $paths = @()
            foreach ($pattern in @('C:\Python*\python.exe', 'C:\Program Files\Python*\python.exe', 'C:\Program Files (x86)\Python*\python.exe')) {
                $paths += Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
            }
            $cmd = Get-Command python -ErrorAction SilentlyContinue
            if ($cmd) { $paths += $cmd.Source }
            $paths = @($paths | Select-Object -Unique)
            $version = $null
            if ($paths.Count -gt 0) {
                $version = (& $paths[0] --version 2>&1 | Out-String).Trim()
            }
            [ordered]@{ paths = $paths; version = $version }
        }
        $remotePython.paths = @($pyInfo.paths)
        $remotePython.version = $pyInfo.version
        $remotePython.found = ($pyInfo.paths.Count -gt 0)
        [void]$results.Add((New-DiagResult -Name "Remote Python ($RemoteServer)" -Status 'INFO' -Detail $(if ($remotePython.found) { $remotePython.version } else { 'not detected' }) -Data $remotePython))
    } catch {
        [void]$results.Add((New-DiagResult -Name "Remote Python ($RemoteServer)" -Status 'INFO' -Detail $_.Exception.Message -Data $remotePython))
    }
} catch {
    [void]$results.Add((New-DiagResult -Name "PSSession ($RemoteServer)" -Status 'FAIL' -Detail $_.Exception.Message))
} finally {
    if ($session) {
        Remove-PSSession -Session $session -ErrorAction SilentlyContinue
    }
}

if (-not $pssessionPass) {
    try {
        $icData = Invoke-Command -ComputerName $RemoteServer -ScriptBlock {
            [ordered]@{
                hostname   = hostname
                whoami     = whoami
                ps_version = $PSVersionTable.PSVersion.ToString()
            }
        } -ErrorAction Stop
        $remoteIdentity = $icData
        $integratedAuthPass = $true
        $psRemotingPass = $true
        $winrmPass = $true
        [void]$results.Add((New-DiagResult -Name "Invoke-Command ($RemoteServer)" -Status 'PASS' -Detail ($icData | ConvertTo-Json -Compress) -Data $icData))
    } catch {
        [void]$results.Add((New-DiagResult -Name "Invoke-Command ($RemoteServer)" -Status 'FAIL' -Detail $_.Exception.Message))
    }
}

$uncRoot = "\\$RemoteServer\C$"
$diagGuid = [guid]::NewGuid().ToString('N')
$remoteTempRel = "ProgramData\WindowsServerAdminMCP\diag-$diagGuid"
$remoteTempUnc = Join-Path $uncRoot $remoteTempRel
$smbDetail = [ordered]@{}
try {
    if (-not (Test-Path -LiteralPath $uncRoot)) {
        throw "UNC path not accessible: $uncRoot"
    }
    New-Item -ItemType Directory -Path $remoteTempUnc -Force | Out-Null
    $testFile = Join-Path $remoteTempUnc 'write-test.txt'
    $payload = "diag-$diagGuid"
    Set-Content -LiteralPath $testFile -Value $payload -Encoding UTF8
    $readBack = Get-Content -LiteralPath $testFile -Raw
    if ($readBack.Trim() -ne $payload) {
        throw 'SMB read/write mismatch'
    }
    Remove-Item -LiteralPath $remoteTempUnc -Recurse -Force
    $smbPass = $true
    $smbDetail = [ordered]@{ unc = $uncRoot; temp = $remoteTempUnc; cleaned = $true }
    [void]$results.Add((New-DiagResult -Name "SMB C$ ($RemoteServer)" -Status 'PASS' -Detail 'read/write/delete ok' -Data $smbDetail))
} catch {
    $smbDetail = [ordered]@{ unc = $uncRoot; error = $_.Exception.Message }
    [void]$results.Add((New-DiagResult -Name "SMB C$ ($RemoteServer)" -Status 'FAIL' -Detail $_.Exception.Message -Data $smbDetail))
    try {
        if (Test-Path -LiteralPath $remoteTempUnc) {
            Remove-Item -LiteralPath $remoteTempUnc -Recurse -Force -ErrorAction SilentlyContinue
        }
    } catch { }
}

if (-not $pssessionPass -and $psRemotingPass) {
    try {
        $pyInfo = Invoke-Command -ComputerName $RemoteServer -ScriptBlock {
            $paths = @()
            foreach ($pattern in @('C:\Python*\python.exe', 'C:\Program Files\Python*\python.exe')) {
                $paths += Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
            }
            $cmd = Get-Command python -ErrorAction SilentlyContinue
            if ($cmd) { $paths += $cmd.Source }
            $paths = @($paths | Select-Object -Unique)
            $version = $null
            if ($paths.Count -gt 0) {
                $version = (& $paths[0] --version 2>&1 | Out-String).Trim()
            }
            [ordered]@{ paths = $paths; version = $version }
        } -ErrorAction Stop
        $remotePython.paths = @($pyInfo.paths)
        $remotePython.version = $pyInfo.version
        $remotePython.found = ($pyInfo.paths.Count -gt 0)
        [void]$results.Add((New-DiagResult -Name "Remote Python ($RemoteServer)" -Status 'INFO' -Detail $(if ($remotePython.found) { $remotePython.version } else { 'not detected' }) -Data $remotePython))
    } catch {
        [void]$results.Add((New-DiagResult -Name "Remote Python ($RemoteServer)" -Status 'INFO' -Detail $_.Exception.Message -Data $remotePython))
    }
}

$summaryMarkers = [ordered]@{
    'Integrated Windows authentication' = if ($integratedAuthPass) { 'PASS' } else { 'FAIL' }
    'WinRM'                             = if ($winrmPass) { 'PASS' } else { 'FAIL' }
    'PowerShell Remoting'               = if ($psRemotingPass) { 'PASS' } else { 'FAIL' }
    'PSSession'                         = if ($pssessionPass) { 'PASS' } else { 'FAIL' }
    'SMB C$'                            = if ($smbPass) { 'PASS' } else { 'FAIL' }
}

$completedAt = (Get-Date).ToUniversalTime()
$report = [ordered]@{
    schema_version = '1.0'
    project_root   = $ProjectRoot
    started_at_utc = $startedAt.ToString('o')
    completed_at_utc = $completedAt.ToString('o')
    duration_ms    = [int](($completedAt - $startedAt).TotalMilliseconds)
    local_computer = $env:COMPUTERNAME
    local_user     = "$env:USERDOMAIN\$env:USERNAME"
    remote_server  = $RemoteServer
    summary        = $summaryMarkers
    checks         = @($results)
    reference_files = $referenceFiles
    remote_ports   = $portChecks
    remote_identity = $remoteIdentity
}

$reportsDir = Join-Path $ProjectRoot 'reports'
if (-not (Test-Path -LiteralPath $reportsDir)) {
    New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null
}

$jsonPath = Join-Path $reportsDir 'environment_diagnostic.json'
$mdPath = Join-Path $reportsDir 'environment_diagnostic.md'

$report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $jsonPath -Encoding UTF8

$mdLines = [System.Collections.ArrayList]@()
[void]$mdLines.Add('# Environment Diagnostic Report')
[void]$mdLines.Add('')
[void]$mdLines.Add(("Generated (UTC): {0}" -f $completedAt.ToString('o')))
[void]$mdLines.Add(("Local: {0} as {1}" -f $env:COMPUTERNAME, "$env:USERDOMAIN\$env:USERNAME"))
[void]$mdLines.Add(("Remote server: {0}" -f $RemoteServer))
[void]$mdLines.Add('')
[void]$mdLines.Add('## Summary')
[void]$mdLines.Add('')
[void]$mdLines.Add('| Check | Status |')
[void]$mdLines.Add('| --- | --- |')
foreach ($entry in $summaryMarkers.GetEnumerator()) {
    [void]$mdLines.Add(("| {0} | **{1}** |" -f $entry.Key, $entry.Value))
}
[void]$mdLines.Add('')
[void]$mdLines.Add('## All Checks')
[void]$mdLines.Add('')
[void]$mdLines.Add('| Check | Status | Detail |')
[void]$mdLines.Add('| --- | --- | --- |')
foreach ($check in $results) {
    Add-SummaryLine -Lines $mdLines -Name $check.name -Status $check.status -Detail $check.detail
}
[void]$mdLines.Add('')
[void]$mdLines.Add('## Reference Files')
[void]$mdLines.Add('')
foreach ($ref in $referenceFiles.GetEnumerator()) {
    [void]$mdLines.Add(("### {0}" -f $ref.Key))
    if ($ref.Value.Count -eq 0) {
        [void]$mdLines.Add('- not found')
    } else {
        foreach ($path in $ref.Value) {
            [void]$mdLines.Add("- $path")
        }
    }
    [void]$mdLines.Add('')
}

$mdLines -join "`n" | Set-Content -LiteralPath $mdPath -Encoding UTF8

Write-Host ''
Write-Host '=== Environment Diagnostic Summary ===' -ForegroundColor Cyan
foreach ($entry in $summaryMarkers.GetEnumerator()) {
    $color = if ($entry.Value -eq 'PASS') { 'Green' } else { 'Red' }
    Write-Host ("{0}: {1}" -f $entry.Key, $entry.Value) -ForegroundColor $color
}
Write-Host ''
Write-Host "JSON: $jsonPath"
Write-Host "Markdown: $mdPath"

if ($summaryMarkers.Values -contains 'FAIL') {
    exit 2
}
exit 0
