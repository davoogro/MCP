# Worker PowerShell persistente: JSON línea a línea en stdin/stdout
param(
    [string]$ProbesPath = (Join-Path $PSScriptRoot 'probes.ps1')
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$WarningPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

. $ProbesPath

$Script:ProbesSource = Get-Content -LiteralPath $ProbesPath -Raw -Encoding UTF8
$Script:ProbesContentHash = (
    [BitConverter]::ToString(
        [System.Security.Cryptography.SHA256]::Create().ComputeHash(
            [System.Text.Encoding]::UTF8.GetBytes($Script:ProbesSource)
        )
    ).Replace('-', '').ToLower()
)
$SessionProbeHash = @{}

$SessionCache = @{}
$DefaultPort = 5985
$DefaultUseSsl = $false
$DefaultOpenMs = 20000
$DefaultOpMs = 180000

function Write-Response($obj) {
    $json = $obj | ConvertTo-Json -Depth 12 -Compress
    [Console]::Out.WriteLine($json)
}

function Get-SessionKey($hostname, $port, $useSsl) {
    return "$hostname|$port|$useSsl"
}

function Remove-StaleSession($key) {
    if ($SessionCache.ContainsKey($key)) {
        $s = $SessionCache[$key]
        if ($s) { Remove-PSSession -Session $s -ErrorAction SilentlyContinue }
        $SessionCache.Remove($key)
        $SessionProbeHash.Remove($key)
    }
}

function Ensure-SessionProbesLoaded($session, $key) {
    $expected = $Script:ProbesContentHash
    if ($SessionProbeHash.ContainsKey($key) -and $SessionProbeHash[$key] -eq $expected) { return }
    $loadSb = {
        param($probesSource, $expectedHash)
        if ($global:WinFleetProbesLoadedHash -eq $expectedHash -and $global:WinFleetProbes) { return }
        $null = Invoke-Expression $probesSource
        $global:WinFleetProbesLoadedHash = $expectedHash
    }
    Invoke-Command -Session $session -ScriptBlock $loadSb -ArgumentList $Script:ProbesSource, $expected -ErrorAction Stop
    $SessionProbeHash[$key] = $expected
}

function Get-OrCreateSession($hostname, $port, $useSsl, $openMs, $opMs) {
    $key = Get-SessionKey $hostname $port $useSsl
    if ($SessionCache.ContainsKey($key)) {
        $existing = $SessionCache[$key]
        if ($existing -and $existing.State -eq 'Opened') {
            if (-not $SessionProbeHash.ContainsKey($key) -or $SessionProbeHash[$key] -ne $Script:ProbesContentHash) {
                Ensure-SessionProbesLoaded $existing $key
            }
            return $existing
        }
        Remove-StaleSession $key
    }
    $uri = if ($useSsl) { "https://${hostname}:${port}/wsman" } else { "http://${hostname}:${port}/wsman" }
    $opt = New-PSSessionOption -OpenTimeout $openMs -OperationTimeout $opMs -SkipCACheck -SkipCNCheck
    try {
        $sess = New-PSSession -ComputerName $hostname -UseSSL:$useSsl -SessionOption $opt -Authentication Default -ErrorAction Stop
        $SessionCache[$key] = $sess
        Ensure-SessionProbesLoaded $sess $key
        return $sess
    } catch {
        $msg = $_.Exception.Message
        $code = 'Unreachable'
        if ($msg -match 'access|denied|auth|logon|cred') { $code = 'AuthFailed' }
        throw @{ error = $msg; error_code = $code }
    }
}

function Invoke-ProbeRemote($sessions, $probeName, $argsJson, $retry) {
    if (-not $global:WinFleetProbes.ContainsKey($probeName)) {
        throw @{ error = "probe desconocida: $probeName"; error_code = 'InvalidArgument' }
    }
    foreach ($s in @($sessions)) {
        $sKey = $null
        foreach ($pair in $SessionCache.GetEnumerator()) {
            if ($pair.Value -eq $s) { $sKey = $pair.Key; break }
        }
        if ($sKey) { Ensure-SessionProbesLoaded $s $sKey }
    }
    $sb = {
        param($pName, $aJson)
        $a = $aJson | ConvertFrom-Json
        if (-not $global:WinFleetProbes.ContainsKey($pName)) { throw "probe remota desconocida: $pName" }
        $handler = $global:WinFleetProbes[$pName]
        $result = & $handler $a
        if ($null -eq $result) { return @{} }
        return $result
    }
    $attempt = 0
    while ($true) {
        $attempt++
        $errs = $null
        try {
            $raw = Invoke-Command -Session $sessions -ScriptBlock $sb -ArgumentList $probeName, $argsJson `
                -ThrottleLimit 16 -ErrorAction SilentlyContinue -ErrorVariable +errs
        } catch {
            if ($attempt -le $retry) {
                foreach ($s in $sessions) { Remove-PSSession $s -ErrorAction SilentlyContinue }
                continue
            }
            throw
        }
        $byHost = @{}
        if ($raw) {
            foreach ($item in @($raw)) {
                $h = $item.PSComputerName
                if (-not $byHost.ContainsKey($h)) { $byHost[$h] = @() }
                $byHost[$h] += $item
            }
        }
        $hostErrors = @{}
        if ($errs) {
            foreach ($e in @($errs)) {
                $h = $null
                if ($e.TargetObject) { $h = $e.TargetObject.ToString() }
                if (-not $h -and $e.OriginInfo) { $h = $e.OriginInfo.PSComputerName }
                if ($h) {
                    if (-not $hostErrors.ContainsKey($h)) { $hostErrors[$h] = @() }
                    $hostErrors[$h] += $e.Exception.Message
                }
            }
        }
        return @{ results = $byHost; host_errors = $hostErrors }
    }
}

function Handle-Invoke($req) {
    $id = $req.id
    $hosts = @($req.hosts)
    $probe = $req.probe
    $args = if ($req.args) { ($req.args | ConvertTo-Json -Compress -Depth 8) } else { '{}' }
    $timeout = if ($req.timeout) { [int]$req.timeout } else { 180 }
    $hostMeta = @{}
    if ($req.host_meta) {
        foreach ($p in $req.host_meta.PSObject.Properties) { $hostMeta[$p.Name] = $p.Value }
    }
    $sessions = @()
    $sessionMap = @{}
    $hostResults = @{}
    foreach ($h in $hosts) {
        $meta = $hostMeta[$h]
        if (-not $meta) { $meta = @{ hostname = $h; port = $DefaultPort; use_ssl = $DefaultUseSsl; open_timeout_ms = $DefaultOpenMs; operation_timeout_ms = ($timeout * 1000) } }
        $hostname = $meta.hostname
        $port = [int]$meta.port
        $useSsl = [bool]$meta.use_ssl
        $openMs = [int]$meta.open_timeout_ms
        $opMs = [int]$meta.operation_timeout_ms
        try {
            $sess = Get-OrCreateSession $hostname $port $useSsl $openMs $opMs
            $sessions += $sess
            $sessionMap[$sess.Id] = $h
        } catch {
            $err = $_
            if ($err -is [hashtable]) {
                $hostResults[$h] = @{ ok = $false; error = $err.error; error_code = $err.error_code }
            } else {
                $hostResults[$h] = @{ ok = $false; error = $err.Exception.Message; error_code = 'Unreachable' }
            }
        }
    }
    if ($sessions.Count -gt 0) {
        try {
            $inv = Invoke-ProbeRemote $sessions $probe $args 1
            foreach ($pair in $inv.results.GetEnumerator()) {
                $psName = $pair.Key
                $sid = $null
                foreach ($s in $sessions) {
                    if ($s.ComputerName -eq $psName -or $s.ComputerName -like "*$psName*") { $sid = $sessionMap[$s.Id]; break }
                }
                if (-not $sid) {
                    foreach ($h in $hosts) {
                        if ($hostMeta[$h].hostname -eq $psName) { $sid = $h; break }
                    }
                }
                if (-not $sid) { $sid = $psName }
                $data = $pair.Value
                if ($data.Count -eq 1) { $data = $data[0] }
                $hostResults[$sid] = @{ ok = $true; data = $data }
            }
            foreach ($pair in $inv.host_errors.GetEnumerator()) {
                $psName = $pair.Key
                $sid = $psName
                foreach ($h in $hosts) {
                    if ($hostMeta[$h].hostname -eq $psName) { $sid = $h; break }
                }
                $msg = ($pair.Value -join '; ')
                $hostResults[$sid] = @{ ok = $false; error = $msg; error_code = 'RemoteError' }
            }
        } catch {
            $hostResults['_error'] = @{ ok = $false; error = $_.Exception.Message; error_code = 'RemoteError' }
        }
    }
    $out = @{ id = $id; ok = $true; results = $hostResults; sessions = ($SessionCache.Keys | ForEach-Object { $_ }) }
    Write-Response $out
}

function Handle-InvokeScript($req) {
    $id = $req.id
    $hosts = @($req.hosts)
    $script = [string]$req.script
    $timeout = if ($req.timeout) { [int]$req.timeout } else { 180 }
    $hostMeta = @{}
    if ($req.host_meta) {
        foreach ($p in $req.host_meta.PSObject.Properties) { $hostMeta[$p.Name] = $p.Value }
    }
    $sessions = @()
    $sessionMap = @{}
    $hostResults = @{}
    foreach ($h in $hosts) {
        $meta = $hostMeta[$h]
        if (-not $meta) { $meta = @{ hostname = $h; port = $DefaultPort; use_ssl = $false; open_timeout_ms = $DefaultOpenMs; operation_timeout_ms = ($timeout * 1000) } }
        try {
            $sess = Get-OrCreateSession $meta.hostname ([int]$meta.port) ([bool]$meta.use_ssl) ([int]$meta.open_timeout_ms) ([int]$meta.operation_timeout_ms)
            $sessions += $sess
            $sessionMap[$sess.ComputerName] = $h
        } catch {
            $err = $_
            if ($err -is [hashtable]) {
                $hostResults[$h] = @{ ok = $false; error = $err.error; error_code = $err.error_code }
            } else {
                $hostResults[$h] = @{ ok = $false; error = $err.Exception.Message; error_code = 'Unreachable' }
            }
        }
    }
    if ($sessions.Count -gt 0) {
        $sb = [scriptblock]::Create($script)
        $raw = Invoke-Command -Session $sessions -ScriptBlock $sb -ThrottleLimit 16 -ErrorAction SilentlyContinue -ErrorVariable errs
        $byHost = @{}
        if ($raw) {
            foreach ($item in @($raw)) {
                $hn = $item.PSComputerName
                if (-not $byHost.ContainsKey($hn)) { $byHost[$hn] = @() }
                $byHost[$hn] += $item
            }
        }
        foreach ($pair in $byHost.GetEnumerator()) {
            $sid = $sessionMap[$pair.Key]
            if (-not $sid) { $sid = $pair.Key }
            $hostResults[$sid] = @{ ok = $true; data = $pair.Value }
        }
        if ($errs) {
            foreach ($e in @($errs)) {
                $hn = $e.OriginInfo.PSComputerName
                $sid = if ($sessionMap[$hn]) { $sessionMap[$hn] } else { $hn }
                $hostResults[$sid] = @{ ok = $false; error = $e.Exception.Message; error_code = 'RemoteError' }
            }
        }
    }
    Write-Response @{ id = $id; ok = $true; results = $hostResults }
}

function Handle-InvokeLocal($req) {
    $id = $req.id
    $probe = $req.probe
    $args = if ($req.args) { $req.args } else { @{} }
    if (-not $global:WinFleetProbes.ContainsKey($probe)) {
        Write-Response @{ id = $id; ok = $false; error = "probe desconocida"; error_code = 'InvalidArgument' }
        return
    }
    $handler = $global:WinFleetProbes[$probe]
    $data = & $handler $args
    Write-Response @{ id = $id; ok = $true; data = $data }
}

function Handle-Copy($req) {
    $id = $req.id
    $op = $req.op
    $hostId = $req.host
    $meta = $req.host_meta
    $sess = Get-OrCreateSession $meta.hostname ([int]$meta.port) ([bool]$meta.use_ssl) ([int]$meta.open_timeout_ms) ([int]$meta.operation_timeout_ms)
    if ($op -eq 'copy_to') {
        Copy-Item -Path $req.local_path -Destination $req.remote_path -ToSession $sess -Force -Recurse:( [bool]$req.recurse )
    } else {
        Copy-Item -Path $req.remote_path -Destination $req.local_path -FromSession $sess -Force -Recurse:( [bool]$req.recurse )
    }
    Write-Response @{ id = $id; ok = $true }
}

function Handle-CloseSessions($req) {
    $hosts = @($req.hosts)
    if ($hosts.Count -eq 0) {
        foreach ($k in @($SessionCache.Keys)) { Remove-StaleSession $k }
    } else {
        foreach ($h in $hosts) {
            foreach ($k in @($SessionCache.Keys)) {
                if ($k -like "$h|*") { Remove-StaleSession $k }
            }
        }
    }
    Write-Response @{ id = $req.id; ok = $true; closed = $true }
}

function Handle-Ping($req) {
    Write-Response @{ id = $req.id; ok = $true; pong = $true; pid = $PID }
}

function Handle-ListSessions($req) {
    $open = @()
    foreach ($k in $SessionCache.Keys) {
        $s = $SessionCache[$k]
        if ($s -and $s.State -eq 'Opened') { $open += $k }
    }
    Write-Response @{ id = $req.id; ok = $true; sessions = $open }
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }
    try {
        $req = $line | ConvertFrom-Json
    } catch {
        Write-Response @{ id = $null; ok = $false; error = 'JSON inválido'; error_code = 'InvalidArgument' }
        continue
    }
    try {
        switch ($req.op) {
            'ping' { Handle-Ping $req }
            'invoke' { Handle-Invoke $req }
            'invoke_script' { Handle-InvokeScript $req }
            'invoke_local' { Handle-InvokeLocal $req }
            'copy_to' { Handle-Copy $req }
            'copy_from' { Handle-Copy $req }
            'close_sessions' { Handle-CloseSessions $req }
            'list_sessions' { Handle-ListSessions $req }
            default {
                Write-Response @{ id = $req.id; ok = $false; error = "op desconocida: $($req.op)"; error_code = 'InvalidArgument' }
            }
        }
    } catch {
        Write-Response @{ id = $req.id; ok = $false; error = $_.Exception.Message; error_code = 'RemoteError' }
    }
}

foreach ($k in @($SessionCache.Keys)) { Remove-StaleSession $k }
