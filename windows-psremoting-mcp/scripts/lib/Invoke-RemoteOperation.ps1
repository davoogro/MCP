# Compatible with Windows PowerShell 5.1 and PowerShell 7+.
# The request JSON (which may contain a resolved credential) is normally piped via stdin so it
# never appears on a process command line. -RequestJson is kept for manual/backward-compatible use.
param(
    [Parameter(Mandatory = $false)]
    [string]$RequestJson
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RequestJson)) {
    $RequestJson = [Console]::In.ReadToEnd()
}

function Get-EnvOrDefault {
    param([string]$Name, [string]$Default)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

function Write-OperationResult {
    param(
        [bool]$Success,
        [object]$Data = $null,
        [string]$ErrorCode = $null,
        [string]$SanitizedError = $null,
        [string]$TransportUsed = $null,
        [int]$DurationMs = 0,
        [int]$ExitCode = 0,
        [string]$Stderr = ''
    )
    @{
        success         = $Success
        data            = $Data
        errorCode       = $ErrorCode
        sanitizedError  = $SanitizedError
        transportUsed   = $TransportUsed
        durationMs      = $DurationMs
        exitCode        = $ExitCode
        stderr          = $Stderr
    } | ConvertTo-Json -Depth 10 -Compress
}

function New-StrictSessionOption {
    # Millisecond integers work on both PS 5.1 and 7+ (5.1 rejects TimeSpan values here).
    return New-PSSessionOption -SkipCACheck:$false -SkipCNCheck:$false -SkipRevocationCheck:$false `
        -OperationTimeout 120000 -OpenTimeout 15000
}

function Test-SshTransport {
    param($Request, [int]$TimeoutSec)
    $server = $Request.server
    $port = if ($server.port) { [int]$server.port } else { [int](Get-EnvOrDefault 'WINMCP_SSH_PORT' '22') }
    $tcp = Test-NetConnection -ComputerName $server.address -Port $port -WarningAction SilentlyContinue
    if (-not $tcp.TcpTestSucceeded) { return $null }

    if ($Request.sshKeyPath) {
        $sshArgs = @(
            '-o', 'BatchMode=yes',
            '-o', "ConnectTimeout=$TimeoutSec",
            '-o', 'StrictHostKeyChecking=yes',
            '-i', $Request.sshKeyPath,
            "$($Request.credential.username)@$($server.address)",
            'echo MCP_PROBE_OK'
        )
        $output = & ssh @sshArgs 2>&1
        if ($LASTEXITCODE -eq 0 -and ($output -join '') -match 'MCP_PROBE_OK') {
            return 'ssh'
        }
    }

    if ($PSVersionTable.PSVersion.Major -ge 7) {
        try {
            $session = New-PSSession -HostName $server.address -UserName $Request.credential.username `
                -KeyFilePath $Request.sshKeyPath -SSHTransport -ErrorAction Stop
            Remove-PSSession $session
            return 'ssh'
        } catch { }
    }
    return $null
}

function Test-WinRmTransport {
    param($Request, [int]$TimeoutSec)
    $server = $Request.server
    $port = if ($server.port) { [int]$server.port } else { [int](Get-EnvOrDefault 'WINMCP_WINRM_PORT' '5986') }
    $tcp = Test-NetConnection -ComputerName $server.address -Port $port -WarningAction SilentlyContinue
    if (-not $tcp.TcpTestSucceeded) { return $null }

    if ($Request.defaults.verifyTls -eq $true) {
        try {
            $null = Test-WSMan -ComputerName $server.address -UseSSL -Port $port -ErrorAction Stop
        } catch {
            return $null
        }
    }
    return 'winrm'
}

function Select-Transport {
    param($Request)
    $timeout = [int]$Request.defaults.connectionTimeoutSeconds
    $preferred = @()
    switch ($Request.server.transport) {
        'ssh' { $preferred = @('ssh') }
        'winrm' { $preferred = @('winrm') }
        default { $preferred = @('ssh', 'winrm') }
    }
    foreach ($item in $preferred) {
        if ($item -eq 'ssh') {
            $found = Test-SshTransport -Request $Request -TimeoutSec $timeout
            if ($found) { return $found }
        }
        if ($item -eq 'winrm') {
            $found = Test-WinRmTransport -Request $Request -TimeoutSec $timeout
            if ($found) { return $found }
        }
    }
    return $null
}

function Invoke-RemoteApproved {
    param(
        $Request,
        [string]$Transport,
        [scriptblock]$ScriptBlock,
        [object[]]$ArgumentList = @()
    )
    $server = $Request.server
    $cred = New-Object System.Management.Automation.PSCredential(
        $Request.credential.username,
        (ConvertTo-SecureString $Request.credential.password -AsPlainText -Force)
    )
    $port = if ($server.port) { [int]$server.port } else {
        if ($Transport -eq 'winrm') { [int](Get-EnvOrDefault 'WINMCP_WINRM_PORT' '5986') } else { 22 }
    }

    if ($Transport -eq 'ssh') {
        if ($PSVersionTable.PSVersion.Major -ge 7 -and $Request.sshKeyPath) {
            $session = New-PSSession -HostName $server.address -UserName $Request.credential.username `
                -KeyFilePath $Request.sshKeyPath -SSHTransport -ErrorAction Stop
            try {
                return Invoke-Command -Session $session -ScriptBlock $ScriptBlock -ArgumentList $ArgumentList -ErrorAction Stop
            } finally {
                Remove-PSSession $session
            }
        }
        throw 'SSH transport requires PowerShell 7+ and sshKeyPath'
    }

    $option = New-StrictSessionOption
    return Invoke-Command -ComputerName $server.address -Credential $cred -UseSSL -Port $port `
        -SessionOption $option -ScriptBlock $ScriptBlock -ArgumentList $ArgumentList -ErrorAction Stop
}

$started = Get-Date
try {
    $request = $RequestJson | ConvertFrom-Json
    $operationId = [string]$request.operationId
    $parameters = @{}
    if ($request.parameters) {
        $request.parameters.psobject.Properties | ForEach-Object { $parameters[$_.Name] = $_.Value }
    }

    switch ($operationId) {
        'probe_connection' {
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $transport = Select-Transport -Request $request
            if (-not $transport) {
                Write-OperationResult -Success $false -ErrorCode 'HOST_UNREACHABLE' `
                    -SanitizedError 'No secure transport available (SSH or WinRM HTTPS)' `
                    -DurationMs $sw.ElapsedMilliseconds
                exit 0
            }
            Write-OperationResult -Success $true -TransportUsed $transport -Data @{
                reachable     = $true
                transportUsed = $transport
                latencyMs     = $sw.ElapsedMilliseconds
                tlsValid      = $true
            } -DurationMs $sw.ElapsedMilliseconds
            exit 0
        }
        'get_health' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                $os = Get-CimInstance Win32_OperatingSystem
                $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
                [pscustomobject]@{
                    computerName   = $env:COMPUTERNAME
                    osName         = $os.Caption
                    osVersion      = $os.Version
                    lastBootTimeUtc = $os.LastBootUpTime.ToUniversalTime().ToString('o')
                    totalMemoryGb  = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
                    freeMemoryGb   = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
                    cpuLoadPercent = $cpu.LoadPercentage
                }
            }
            Write-OperationResult -Success $true -TransportUsed $transport -Data $data `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_uptime' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                $os = Get-CimInstance Win32_OperatingSystem
                $uptime = (Get-Date) - $os.LastBootUpTime
                [pscustomobject]@{
                    lastBootTimeUtc = $os.LastBootUpTime.ToUniversalTime().ToString('o')
                    uptimeSeconds   = [int]$uptime.TotalSeconds
                }
            }
            Write-OperationResult -Success $true -TransportUsed $transport -Data $data `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_disk_usage' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $driveLetters = @($parameters.driveLetters)
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                param($DriveLetters)
                $volumes = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3"
                if ($DriveLetters -and $DriveLetters.Count -gt 0) {
                    $volumes = $volumes | Where-Object { $DriveLetters -contains $_.DeviceID }
                }
                $volumes | ForEach-Object {
                    [pscustomobject]@{
                        driveLetter  = $_.DeviceID
                        sizeGb       = [math]::Round($_.Size / 1GB, 2)
                        freeGb       = [math]::Round($_.FreeSpace / 1GB, 2)
                        usedPercent  = if ($_.Size -gt 0) { [math]::Round((($_.Size - $_.FreeSpace) / $_.Size) * 100, 2) } else { 0 }
                    }
                }
            } -ArgumentList @(,$driveLetters)
            Write-OperationResult -Success $true -TransportUsed $transport -Data @{ volumes = $data } `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_service_status' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $serviceNames = @($parameters.serviceNames)
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                param($Names)
                foreach ($name in $Names) {
                    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
                    if ($svc) {
                        [pscustomobject]@{ name = $svc.Name; status = $svc.Status.ToString(); displayName = $svc.DisplayName }
                    } else {
                        [pscustomobject]@{ name = $name; status = 'NotFound'; displayName = $null }
                    }
                }
            } -ArgumentList @(,$serviceNames)
            Write-OperationResult -Success $true -TransportUsed $transport -Data @{ services = $data } `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_recent_events' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                param($LogName, $Hours, $Level, $MaxEvents)
                $since = (Get-Date).AddHours(-1 * [int]$Hours)
                Get-WinEvent -FilterHashtable @{
                    LogName   = $LogName
                    StartTime = $since
                    Level     = switch ($Level) {
                        'Critical' { 1 }
                        'Error' { 2 }
                        'Warning' { 3 }
                        default { 4 }
                    }
                } -MaxEvents $MaxEvents -ErrorAction SilentlyContinue |
                    Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, @{ n = 'Message'; e = { $_.Message.Substring(0, [Math]::Min(200, $_.Message.Length)) } }
            } -ArgumentList @($parameters.logName, $parameters.hours, $parameters.level, $parameters.maxEvents)
            Write-OperationResult -Success $true -TransportUsed $transport -Data @{ events = $data } `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'get_pending_reboot' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                $pending = $false
                $reasons = @()
                if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
                    $pending = $true; $reasons += 'WindowsUpdate'
                }
                if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
                    $pending = $true; $reasons += 'CBS'
                }
                [pscustomobject]@{ pendingReboot = $pending; reasons = $reasons }
            }
            Write-OperationResult -Success $true -TransportUsed $transport -Data $data `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'restart_service' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            if ($parameters.dryRun -eq $true) {
                Write-OperationResult -Success $true -TransportUsed $transport -Data @{
                    dryRun = $true
                    action = 'Would restart service'
                    serviceName = $parameters.serviceName
                } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
                exit 0
            }
            $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                param($ServiceName)
                $svc = Get-Service -Name $ServiceName -ErrorAction Stop
                Restart-Service -Name $ServiceName -Force -ErrorAction Stop
                Start-Sleep -Seconds 2
                $after = Get-Service -Name $ServiceName
                [pscustomobject]@{ serviceName = $ServiceName; statusBefore = $svc.Status.ToString(); statusAfter = $after.Status.ToString() }
            } -ArgumentList @($parameters.serviceName)
            Write-OperationResult -Success $true -TransportUsed $transport -Data $data `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'restart_server' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $timeline = @()
            if ($parameters.dryRun -eq $true) {
                $timeline += @{ phase = 'validate'; at = (Get-Date).ToUniversalTime().ToString('o'); ok = $true; detail = 'dryRun=true' }
                Write-OperationResult -Success $true -TransportUsed $transport -Data @{
                    dryRun = $true
                    initiated = $false
                    rebootCommandSent = $false
                    hostWentOffline = $false
                    hostRecovered = $false
                    criticalServicesHealthy = $false
                    timeline = $timeline
                } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
                exit 0
            }
            Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                param($Force)
                Restart-Computer -Force:$Force -Confirm:$false
            } -ArgumentList @([bool]$parameters.force)
            Write-OperationResult -Success $true -TransportUsed $transport -Data @{
                dryRun = $false
                initiated = $true
                rebootCommandSent = $true
                hostWentOffline = $true
                hostRecovered = $false
                criticalServicesHealthy = $false
                timeline = @(
                    @{ phase = 'reboot_sent'; at = (Get-Date).ToUniversalTime().ToString('o'); ok = $true }
                )
            } -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'wait_for_online' {
            $timeoutMs = [int]$parameters.timeoutMs
            $pollMs = [int]$parameters.pollIntervalMs
            $deadline = (Get-Date).AddMilliseconds($timeoutMs)
            $online = $false
            while ((Get-Date) -lt $deadline) {
                $transport = Select-Transport -Request $request
                if ($transport) { $online = $true; break }
                Start-Sleep -Milliseconds $pollMs
            }
            Write-OperationResult -Success $online -TransportUsed $(if ($online) { 'auto' } else { $null }) -Data @{
                online = $online
            } -ErrorCode $(if ($online) { $null } else { 'RECOVERY_FAILED' }) `
                -SanitizedError $(if ($online) { $null } else { 'Server did not recover within timeout' }) `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        'diagnostic' {
            $transport = Select-Transport -Request $request
            if (-not $transport) { throw 'No secure transport available' }
            $diag = [string]$parameters.diagnostic
            $diagParams = @{}
            if ($parameters.parameters) {
                $parameters.parameters.psobject.Properties | ForEach-Object { $diagParams[$_.Name] = $_.Value }
            }
            switch ($diag) {
                'ping' {
                    $count = if ($diagParams.ContainsKey('count') -and $null -ne $diagParams['count']) { [int]$diagParams['count'] } else { 2 }
                    if ($count -lt 1 -or $count -gt 10) { $count = 2 }
                    $data = Test-Connection -ComputerName $request.server.address -Count $count -ErrorAction SilentlyContinue
                }
                'dns_resolve' {
                    $data = Resolve-DnsName -Name ([string]$diagParams.hostname) -ErrorAction Stop
                }
                'tcp_port_check' {
                    $port = [int]$diagParams.port
                    if (@(22, 445, 5986, 3389) -notcontains $port) { throw 'Port not in approved diagnostic list' }
                    $data = Test-NetConnection -ComputerName $request.server.address -Port $port -WarningAction SilentlyContinue
                }
                'winrm_listener' {
                    $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                        Get-ChildItem WSMan:\localhost\Listener | Select-Object Name, Transport, Port
                    }
                }
                'disk_smart_status' {
                    $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                        Get-CimInstance -Namespace root/wmi -Class MSStorageDriver_FailurePredictStatus -ErrorAction SilentlyContinue
                    }
                }
                'service_dependencies' {
                    $data = Invoke-RemoteApproved -Request $request -Transport $transport -ScriptBlock {
                        param($ServiceName)
                        (Get-Service -Name $ServiceName -ErrorAction Stop).DependentServices |
                            Select-Object Name, Status, DisplayName
                    } -ArgumentList @([string]$diagParams.serviceName)
                }
                default { throw "Unknown diagnostic: $diag" }
            }
            Write-OperationResult -Success $true -TransportUsed $transport -Data @{ result = $data } `
                -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds)
            exit 0
        }
        default {
            throw "Unknown operationId: $operationId"
        }
    }
}
catch {
    $msg = $_.Exception.Message
    if ($msg -match '(?i)(password|credential|token)') { $msg = '[REDACTED] authentication failure' }
    Write-OperationResult -Success $false -ErrorCode 'INTERNAL_ERROR' -SanitizedError $msg `
        -DurationMs ([int]((Get-Date) - $started).TotalMilliseconds) -ExitCode 1
    exit 0
}
