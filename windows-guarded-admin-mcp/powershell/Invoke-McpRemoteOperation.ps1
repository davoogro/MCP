param(
    [Parameter(Mandatory = $true)]
    [string]$RequestJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-McpResult {
    param(
        [bool]$Success,
        [object]$Data = $null,
        [string]$ErrorCode = $null,
        [string]$SanitizedError = $null,
        [string]$TransportUsed = 'winrm',
        [int]$DurationMs = 0,
        [string[]]$Warnings = @()
    )
    @{
        success        = $Success
        data           = $Data
        errorCode      = $ErrorCode
        sanitizedError = $SanitizedError
        transportUsed  = $TransportUsed
        durationMs     = $DurationMs
        warnings       = @($Warnings)
    } | ConvertTo-Json -Depth 12 -Compress
}

function Get-McpServerConfig {
    param($Request)
    $server = $Request.server
    $port = if ($null -ne $server.port) { [int]$server.port } else { 5985 }
    $useSsl = [bool]$server.useSsl
    $auth = if ($server.authentication) { [string]$server.authentication } else { 'current_windows_identity' }
    if ($auth -ne 'current_windows_identity') {
        throw 'AUTH_FAILED: only current_windows_identity is supported'
    }
    return [pscustomobject]@{
        Hostname           = [string]$server.hostname
        Port               = $port
        UseSsl             = $useSsl
        Authentication     = $auth
        UsePssession       = if ($null -ne $server.usePssession) { [bool]$server.usePssession } else { $true }
        AllowSmbFallback   = if ($null -ne $server.allowSmbFallback) { [bool]$server.allowSmbFallback } else { $true }
        StagingRoot        = 'C:\ProgramData\WindowsServerAdminMCP\staging'
    }
}

function New-McpSessionOption {
    param([int]$OpenTimeoutSeconds = 15, [int]$OperationTimeoutSeconds = 120)
    # PS 5.1 expects millisecond integers, not TimeSpan (PS 7+ accepts both).
    return New-PSSessionOption -SkipCACheck:$false -SkipCNCheck:$false -SkipRevocationCheck:$false `
        -OperationTimeout ($OperationTimeoutSeconds * 1000) `
        -OpenTimeout ($OpenTimeoutSeconds * 1000)
}

function Test-McpTcpPort {
    param([string]$Hostname, [int]$Port)
    try {
        $tcp = Test-NetConnection -ComputerName $Hostname -Port $Port -WarningAction SilentlyContinue
        return [bool]$tcp.TcpTestSucceeded
    } catch {
        return $false
    }
}

function Test-McpWinRm {
    param($Cfg)
    $wsmanParams = @{
        ComputerName = $Cfg.Hostname
        Port         = $Cfg.Port
        ErrorAction  = 'Stop'
    }
    if ($Cfg.UseSsl) {
        $wsmanParams['UseSSL'] = $true
    }
    try {
        $null = Test-WSMan @wsmanParams
        return $true
    } catch {
        return $false
    }
}

function New-McpRemoteSession {
    param($Cfg, [int]$OpenTimeoutSeconds = 15, [int]$OperationTimeoutSeconds = 120)
    $option = New-McpSessionOption -OpenTimeoutSeconds $OpenTimeoutSeconds -OperationTimeoutSeconds $OperationTimeoutSeconds
    $sessionParams = @{
        ComputerName  = $Cfg.Hostname
        Port          = $Cfg.Port
        SessionOption = $option
        ErrorAction   = 'Stop'
    }
    if ($Cfg.UseSsl) {
        $sessionParams['UseSSL'] = $true
    }
    return New-PSSession @sessionParams
}

function Invoke-McpRemote {
    param(
        $Cfg,
        [scriptblock]$ScriptBlock,
        [object[]]$ArgumentList = @(),
        [int]$OpenTimeoutSeconds = 15,
        [int]$OperationTimeoutSeconds = 120
    )
    $option = New-McpSessionOption -OpenTimeoutSeconds $OpenTimeoutSeconds -OperationTimeoutSeconds $OperationTimeoutSeconds
    $invokeParams = @{
        ComputerName  = $Cfg.Hostname
        Port          = $Cfg.Port
        SessionOption = $option
        ScriptBlock   = $ScriptBlock
        ArgumentList  = $ArgumentList
        ErrorAction   = 'Stop'
    }
    if ($Cfg.UseSsl) {
        $invokeParams['UseSSL'] = $true
    }
    $output = Invoke-Command @invokeParams
    if ($null -eq $output) { return $null }
    # Drop the remoting bookkeeping properties Invoke-Command adds to every object (noise in tool output).
    return @($output) | ForEach-Object { Remove-McpRemotingNoise -Item $_ }
}

function Remove-McpRemotingNoise {
    param($Item)
    if ($null -ne $Item) {
        foreach ($name in @('PSComputerName', 'RunspaceId', 'PSShowComputerName')) {
            if ($null -ne $Item.PSObject.Properties[$name]) {
                [void]$Item.PSObject.Properties.Remove($name)
            }
        }
    }
    , $Item
}

function Copy-McpFileToRemote {
    param(
        $Cfg,
        $Session,
        [string]$LocalPath,
        [string]$RemotePath,
        [string]$OperationId
    )
    try {
        Copy-Item -Path $LocalPath -Destination $RemotePath -ToSession $Session -Force -ErrorAction Stop
        return @{ success = $true; method = 'pssession'; remotePath = $RemotePath }
    } catch {
        if (-not $Cfg.AllowSmbFallback) {
            throw "PSSESSION_FAILED: $($_.Exception.Message)"
        }
        $stagingDir = Join-Path $Cfg.StagingRoot $OperationId
        $fileName = Split-Path $LocalPath -Leaf
        $smbDir = "\\$($Cfg.Hostname)\C$\ProgramData\WindowsServerAdminMCP\staging\$OperationId"
        $smbPath = Join-Path $smbDir $fileName
        try {
            if (-not (Test-Path -LiteralPath $smbDir)) {
                New-Item -ItemType Directory -Path $smbDir -Force | Out-Null
            }
            Copy-Item -Path $LocalPath -Destination $smbPath -Force -ErrorAction Stop
            return @{ success = $true; method = 'smb'; remotePath = (Join-Path $stagingDir $fileName) }
        } catch {
            throw "SMB_UNAVAILABLE: $($_.Exception.Message)"
        }
    }
}

function Invoke-McpDiagnoseAccess {
    param(
        $Cfg,
        [bool]$IncludeSmbTest = $true
    )
    $result = [ordered]@{
        hostname        = $Cfg.Hostname
        authentication  = $Cfg.Authentication
        port            = $Cfg.Port
        useSsl          = $Cfg.UseSsl
        dns             = @{ resolved = $false; addresses = @() }
        ports           = @{}
        winrm           = @{ available = $false }
        pssession       = @{ available = $false }
        identity        = $null
        powershell      = $null
        smb             = @{ available = $null }
        remotePython    = @{ available = $null }
        reachable       = $false
        transportUsed   = 'winrm'
    }

    try {
        $dns = Resolve-DnsName -Name $Cfg.Hostname -ErrorAction Stop
        $result.dns.resolved = $true
        $result.dns.addresses = @($dns | Where-Object { $_.IPAddress } | ForEach-Object { $_.IPAddress })
    } catch {
        $result.dns.error = 'DNS_FAILED'
    }

    foreach ($p in @(5985, 5986, 445, 135)) {
        $result.ports[[string]$p] = Test-McpTcpPort -Hostname $Cfg.Hostname -Port $p
    }

    $result.winrm.available = Test-McpWinRm -Cfg $Cfg
    if ($result.winrm.available) {
        try {
            $session = New-McpRemoteSession -Cfg $Cfg
            try {
                $probe = Invoke-Command -Session $session -ScriptBlock {
                    [pscustomobject]@{
                        whoami     = (whoami)
                        psVersion  = $PSVersionTable.PSVersion.ToString()
                        computer   = $env:COMPUTERNAME
                    }
                }
                $result.pssession.available = $true
                $result.identity = $probe.whoami
                $result.powershell = $probe.psVersion
            } finally {
                Remove-PSSession $session -ErrorAction SilentlyContinue
            }
        } catch {
            $result.pssession.error = 'PSSESSION_FAILED'
            $result.pssession.message = $_.Exception.Message
        }
    } else {
        $result.winrm.error = 'WINRM_UNAVAILABLE'
    }

    if (-not $IncludeSmbTest) {
        $result.smb = @{ skipped = $true }
    } elseif ($Cfg.AllowSmbFallback) {
        $smbProbe = "\\$($Cfg.Hostname)\C$\ProgramData"
        $result.smb.available = Test-Path -LiteralPath $smbProbe -ErrorAction SilentlyContinue
        if (-not $result.smb.available) {
            $result.smb.error = 'SMB_UNAVAILABLE'
        }
    }

    try {
        $pyProbe = Invoke-McpRemote -Cfg $Cfg -ScriptBlock {
            $py = Get-Command python -ErrorAction SilentlyContinue
            if ($py) { return $py.Source }
            $null
        }
        $result.remotePython.available = [bool]$pyProbe
        $result.remotePython.path = $pyProbe
    } catch {
        $result.remotePython.available = $false
        $result.remotePython.error = 'REMOTE_PYTHON_UNAVAILABLE'
    }

    $result.reachable = $result.winrm.available -and $result.pssession.available
    return $result
}

function ConvertTo-McpList {
    # Always serialize list results as JSON arrays (a single object would otherwise become a bare object).
    param($Items)
    , @(@($Items) | Where-Object { $null -ne $_ })
}

function Get-McpParam {
    param($Parameters, [string]$Name, $Default = $null)
    if ($null -eq $Parameters) { return $Default }
    if ($Parameters -is [hashtable]) {
        if ($Parameters.ContainsKey($Name)) { return $Parameters[$Name] }
        return $Default
    }
    $prop = $Parameters.PSObject.Properties[$Name]
    if ($null -ne $prop) { return $prop.Value }
    return $Default
}

$started = Get-Date
try {
    $request = $RequestJson | ConvertFrom-Json
    $operation = [string]$request.operation
    $cfg = Get-McpServerConfig -Request $request
    $parameters = @{}
    if ($request.parameters) {
        $request.parameters.psobject.Properties | ForEach-Object { $parameters[$_.Name] = $_.Value }
    }
    $opId = if ($request.operationId) { [string]$request.operationId } else { [guid]::NewGuid().ToString() }

    switch ($operation) {
        'diagnose_access' {
            $includeSmbTest = [bool](Get-McpParam -Parameters $parameters -Name 'includeSmbTest' -Default $true)
            $data = Invoke-McpDiagnoseAccess -Cfg $cfg -IncludeSmbTest $includeSmbTest
            $warnings = @()
            if (-not $data.reachable) {
                $warnings += 'Target not fully reachable; see data.reachable and component probes'
            }
            Write-McpResult -Success $true -Data $data -Warnings $warnings `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_identity' {
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                $cs = Get-CimInstance Win32_ComputerSystem
                $os = Get-CimInstance Win32_OperatingSystem
                [pscustomobject]@{
                    computerName = $env:COMPUTERNAME
                    whoami       = (whoami)
                    domain       = $cs.Domain
                    osName       = $os.Caption
                    osVersion    = $os.Version
                }
            }
            Write-McpResult -Success $true -Data $data -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'test_execution' {
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                [pscustomobject]@{
                    echo      = 'MCP_OK'
                    whoami    = (whoami)
                    timestamp = (Get-Date).ToUniversalTime().ToString('o')
                }
            }
            Write-McpResult -Success $true -Data $data -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_health' {
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                $os = Get-CimInstance Win32_OperatingSystem
                $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
                $pending = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
                [pscustomobject]@{
                    computerName    = $env:COMPUTERNAME
                    osName          = $os.Caption
                    osVersion       = $os.Version
                    lastBootTimeUtc = $os.LastBootUpTime.ToUniversalTime().ToString('o')
                    totalMemoryGb   = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
                    freeMemoryGb    = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
                    cpuLoadPercent  = $cpu.LoadPercentage
                    pendingReboot   = $pending
                }
            }
            Write-McpResult -Success $true -Data $data -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_disks' {
            $letters = @()
            $driveLetters = Get-McpParam -Parameters $parameters -Name 'driveLetters'
            if ($driveLetters) { $letters = @($driveLetters) }
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                param($DriveLetters)
                $volumes = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'
                if ($DriveLetters -and $DriveLetters.Count -gt 0) {
                    $volumes = $volumes | Where-Object { $DriveLetters -contains $_.DeviceID }
                }
                $volumes | ForEach-Object {
                    [pscustomobject]@{
                        driveLetter = $_.DeviceID
                        sizeGb      = [math]::Round($_.Size / 1GB, 2)
                        freeGb      = [math]::Round($_.FreeSpace / 1GB, 2)
                        usedPercent = if ($_.Size -gt 0) { [math]::Round((($_.Size - $_.FreeSpace) / $_.Size) * 100, 2) } else { 0 }
                    }
                }
            } -ArgumentList @(,$letters)
            Write-McpResult -Success $true -Data @{ disks = (ConvertTo-McpList $data) } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_services' {
            # Note: '-ArgumentList @(,$names, $filter)' would wrap the names array twice (the remote
            # side then saw one element = the whole array). Pass the two arguments explicitly.
            $svcNames = @(Get-McpParam -Parameters $parameters -Name 'serviceNames' | Where-Object { $_ })
            $svcFilter = Get-McpParam -Parameters $parameters -Name 'nameFilter'
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                param($ServiceNames, $NameFilter)
                if ($ServiceNames -and $ServiceNames.Count -gt 0) {
                    foreach ($name in $ServiceNames) {
                        $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
                        if ($svc) {
                            [pscustomobject]@{ name = $svc.Name; status = $svc.Status.ToString(); displayName = $svc.DisplayName }
                        } else {
                            [pscustomobject]@{ name = $name; status = 'NotFound'; displayName = $null }
                        }
                    }
                } else {
                    $all = Get-Service
                    if ($NameFilter) {
                        $all = $all | Where-Object { $_.DisplayName -like "*$NameFilter*" -or $_.Name -like "*$NameFilter*" }
                    }
                    $all | Select-Object -First 200 | ForEach-Object {
                        [pscustomobject]@{ name = $_.Name; status = $_.Status.ToString(); displayName = $_.DisplayName }
                    }
                }
            } -ArgumentList $svcNames, $svcFilter
            Write-McpResult -Success $true -Data @{ services = (ConvertTo-McpList $data) } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_processes' {
            $top = [int](Get-McpParam -Parameters $parameters -Name 'top' -Default 10)
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                param($Top, $NameFilter)
                $procs = Get-Process | Sort-Object WorkingSet64 -Descending
                if ($NameFilter) { $procs = $procs | Where-Object { $_.ProcessName -like "*$NameFilter*" } }
                $procs | Select-Object -First $Top | ForEach-Object {
                    [pscustomobject]@{
                        name         = $_.ProcessName
                        id           = $_.Id
                        cpuSeconds   = [math]::Round($_.CPU, 2)
                        workingSetMb = [math]::Round($_.WorkingSet64 / 1MB, 2)
                    }
                }
            } -ArgumentList @($top, (Get-McpParam -Parameters $parameters -Name 'nameFilter'))
            Write-McpResult -Success $true -Data @{ processes = (ConvertTo-McpList $data) } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_events' {
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                param($LogName, $Hours, $Level, $MaxEvents)
                $since = (Get-Date).AddHours(-1 * [int]$Hours)
                $levelId = switch ($Level) {
                    'Critical' { 1 }
                    'Error' { 2 }
                    'Warning' { 3 }
                    default { 4 }
                }
                Get-WinEvent -FilterHashtable @{
                    LogName   = $LogName
                    StartTime = $since
                    Level     = $levelId
                } -MaxEvents $MaxEvents -ErrorAction SilentlyContinue |
                    ForEach-Object {
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
            } -ArgumentList @(
                (Get-McpParam -Parameters $parameters -Name 'logName'),
                (Get-McpParam -Parameters $parameters -Name 'hours'),
                (Get-McpParam -Parameters $parameters -Name 'level'),
                (Get-McpParam -Parameters $parameters -Name 'maxEvents')
            )
            Write-McpResult -Success $true -Data @{ events = (ConvertTo-McpList $data) } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'restart_service' {
            $serviceName = Get-McpParam -Parameters $parameters -Name 'serviceName'
            if ((Get-McpParam -Parameters $parameters -Name 'dryRun') -eq $true) {
                Write-McpResult -Success $true -Data @{
                    dryRun      = $true
                    serviceName = $serviceName
                    action      = 'Would restart service'
                } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
                exit 0
            }
            $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                param($ServiceName)
                $svc = Get-Service -Name $ServiceName -ErrorAction Stop
                $before = $svc.Status.ToString()
                Restart-Service -Name $ServiceName -Force -ErrorAction Stop
                Start-Sleep -Seconds 2
                $after = (Get-Service -Name $ServiceName).Status.ToString()
                [pscustomobject]@{ serviceName = $ServiceName; statusBefore = $before; statusAfter = $after }
            } -ArgumentList @($serviceName)
            Write-McpResult -Success $true -Data $data -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'wait_for_online' {
            $timeoutSec = [int](Get-McpParam -Parameters $parameters -Name 'timeoutSeconds' -Default 900)
            $pollSec = [int](Get-McpParam -Parameters $parameters -Name 'pollIntervalSeconds' -Default 10)
            $deadline = (Get-Date).AddSeconds($timeoutSec)
            $online = $false
            while ((Get-Date) -lt $deadline) {
                $diag = Invoke-McpDiagnoseAccess -Cfg $cfg
                if ($diag.reachable) { $online = $true; break }
                Start-Sleep -Seconds $pollSec
            }
            Write-McpResult -Success $online -Data @{ online = $online } `
                -ErrorCode $(if ($online) { $null } else { 'SERVER_NOT_RECOVERED' }) `
                -SanitizedError $(if ($online) { $null } else { 'Server did not recover within timeout' }) `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'run_authorized_script' {
            $scriptPath = [string](Get-McpParam -Parameters $parameters -Name 'scriptPath')
            if (-not (Test-Path -LiteralPath $scriptPath)) { throw "Script not found: $scriptPath" }
            # Re-verify the catalog hash right before staging (closes the gap between the Python check and the copy).
            $expectedHash = [string](Get-McpParam -Parameters $parameters -Name 'sha256')
            if ([string]::IsNullOrWhiteSpace($expectedHash)) { throw 'HASH_INVALID: missing expected sha256' }
            $actualHash = (Get-FileHash -LiteralPath $scriptPath -Algorithm SHA256).Hash
            if ($actualHash -ne $expectedHash) { throw 'HASH_INVALID: script changed after catalog verification' }
            $scriptParams = @{}
            $nestedParams = Get-McpParam -Parameters $parameters -Name 'parameters'
            if ($nestedParams) {
                $nestedParams.psobject.Properties | ForEach-Object { $scriptParams[$_.Name] = $_.Value }
            }
            $session = $null
            $warnings = @()
            try {
                $remoteStaging = Join-Path $cfg.StagingRoot $opId
                if ($cfg.UsePssession) {
                    $session = New-McpRemoteSession -Cfg $cfg
                    Invoke-Command -Session $session -ScriptBlock {
                        param($p)
                        if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
                    } -ArgumentList $remoteStaging
                    $remoteScript = Join-Path $remoteStaging (Split-Path $scriptPath -Leaf)
                    $copyResult = Copy-McpFileToRemote -Cfg $cfg -Session $session -LocalPath $scriptPath `
                        -RemotePath $remoteScript -OperationId $opId
                    if ($copyResult.method -eq 'smb') {
                        $warnings += 'Used SMB fallback for script staging'
                    }
                    $data = Invoke-Command -Session $session -ScriptBlock {
                        param($RemoteScript, $ParamsJson)
                        # ConvertFrom-Json -AsHashtable does not exist in Windows PowerShell 5.1 (typical
                        # remote target), so build the splat hashtable from the PSCustomObject manually.
                        $params = @{}
                        if ($ParamsJson) {
                            $parsed = $ParamsJson | ConvertFrom-Json
                            if ($null -ne $parsed) {
                                foreach ($prop in $parsed.PSObject.Properties) { $params[$prop.Name] = $prop.Value }
                            }
                        }
                        & $RemoteScript @params
                    } -ArgumentList @($copyResult.remotePath, ($scriptParams | ConvertTo-Json -Compress))
                } else {
                    throw 'PSSESSION_FAILED: use_pssession is disabled for this server'
                }
                Write-McpResult -Success $true -Data @{ output = $data; stagingMethod = $copyResult.method } `
                    -Warnings $warnings -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            } finally {
                if ($session) {
                    try {
                        Invoke-Command -Session $session -ScriptBlock {
                            param($p)
                            if (Test-Path $p) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }
                        } -ArgumentList $remoteStaging -ErrorAction SilentlyContinue
                    } catch {
                        $warnings += 'CLEANUP_FAILED: remote staging directory may remain'
                    }
                    Remove-PSSession $session -ErrorAction SilentlyContinue
                }
            }
            exit 0
        }
        'diagnostic' {
            $diag = [string](Get-McpParam -Parameters $parameters -Name 'diagnostic')
            $diagParams = @{}
            $nestedDiagParams = Get-McpParam -Parameters $parameters -Name 'parameters'
            if ($nestedDiagParams) {
                $nestedDiagParams.psobject.Properties | ForEach-Object { $diagParams[$_.Name] = $_.Value }
            }
            switch ($diag) {
                'ping' {
                    $count = if ($diagParams.count) { [int]$diagParams.count } else { 2 }
                    $data = Test-Connection -ComputerName $cfg.Hostname -Count $count -ErrorAction SilentlyContinue
                }
                'dns_resolve' {
                    $data = Resolve-DnsName -Name ([string]$diagParams.hostname) -ErrorAction Stop
                }
                'tcp_port_check' {
                    $port = [int]$diagParams.port
                    $data = Test-NetConnection -ComputerName $cfg.Hostname -Port $port -WarningAction SilentlyContinue
                }
                default {
                    $data = Invoke-McpRemote -Cfg $cfg -ScriptBlock {
                        param($Diagnostic)
                        switch ($Diagnostic) {
                            'winrm_listener' {
                                Get-ChildItem WSMan:\localhost\Listener | Select-Object Name, Transport, Port
                            }
                            'disk_smart_status' {
                                Get-CimInstance -Namespace root/wmi -Class MSStorageDriver_FailurePredictStatus -ErrorAction SilentlyContinue
                            }
                            default { throw "Unsupported remote diagnostic: $Diagnostic" }
                        }
                    } -ArgumentList @($diag)
                }
            }
            Write-McpResult -Success $true -Data @{ diagnostic = $diag; result = $data } `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        default {
            throw "Unknown operation: $operation"
        }
    }
}
catch {
    $msg = $_.Exception.Message
    if ($msg -match '(?i)(password|credential|token|securestring)') {
        $msg = '[REDACTED] authentication failure'
    }
    $code = switch -Regex ($msg) {
        'DNS_FAILED' { 'DNS_FAILED'; break }
        'WINRM_UNAVAILABLE' { 'WINRM_UNAVAILABLE'; break }
        'PSSESSION_FAILED' { 'PSSESSION_FAILED'; break }
        'SMB_UNAVAILABLE' { 'SMB_UNAVAILABLE'; break }
        'REMOTE_PYTHON_UNAVAILABLE' { 'REMOTE_PYTHON_UNAVAILABLE'; break }
        'AUTH_FAILED' { 'AUTH_FAILED'; break }
        'HASH_INVALID' { 'HASH_INVALID'; break }
        'CLEANUP_FAILED' { 'CLEANUP_FAILED'; break }
        'SERVER_NOT_RECOVERED' { 'SERVER_NOT_RECOVERED'; break }
        default { 'INTERNAL_ERROR' }
    }
    Write-McpResult -Success $false -ErrorCode $code -SanitizedError $msg `
        -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
    exit 0
}
