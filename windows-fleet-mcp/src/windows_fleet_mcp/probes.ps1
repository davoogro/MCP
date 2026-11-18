# Probes remotas: cada entrada recibe un objeto de argumentos (desde JSON)
$global:WinFleetProbes = @{}

function ConvertTo-WinFleetString {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [string]) { return $Value }
    return $Value.ToString()
}

function Get-WinFleetIgnoreAutoStopNames {
    @(
        'DoSvc', 'wuauserv', 'edgeupdate', 'edgeupdatem', 'RemoteRegistry', 'gupdate', 'gupdatem',
        'sppsvc', 'MapsBroker', 'CDPSvc', 'tiledatamodelsvc', 'WbioSrvc', 'ShellHWDetection'
    )
}

$script:WinFleetExpectedStoppedCache = @{}

function Test-WinFleetServiceExpectedStopped {
    param([string]$Name)
    if ($script:WinFleetExpectedStoppedCache.ContainsKey($Name)) {
        return $script:WinFleetExpectedStoppedCache[$Name]
    }
    $expected = $false
    if ((Get-WinFleetIgnoreAutoStopNames) -contains $Name) { $expected = $true }
    else {
        $regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$Name"
        try {
            $delayed = (Get-ItemProperty -Path $regPath -Name DelayedAutostart -ErrorAction SilentlyContinue).DelayedAutostart
            if ($delayed -eq 1) { $expected = $true }
        } catch {}
        if (-not $expected) {
            try {
                if (Test-Path -LiteralPath "$regPath\TriggerInfo") { $expected = $true }
            } catch {}
        }
    }
    $script:WinFleetExpectedStoppedCache[$Name] = $expected
    return $expected
}

function Get-ScheduledTaskResultText {
    param([uint32]$Code)
    switch ($Code) {
        0 { return 'Success' }
        0x41301 { return 'Task is running' }
        0x41303 { return 'Task has not yet run' }
        0x41304 { return 'No more runs scheduled' }
        0x41305 { return 'Task is disabled' }
        0x41306 { return 'Task has not been run' }
        0x41307 { return 'Task is queued' }
        0x41308 { return 'Task is ready' }
        0x41309 { return 'Task is running' }
        0x4130A { return 'Task is terminating' }
        0x41325 { return 'Task is queued' }
        0x800710E0 { return 'Rejected (already running or idle)' }
        0x8004131F { return 'Launch failure' }
        0x80070002 { return 'File not found' }
        0x80070005 { return 'Access denied' }
        default { return 'Failure or unknown' }
    }
}

function Test-ScheduledTaskResultIsFailure {
    param([uint32]$Code)
    $nonFailure = @(
        0, 0x41301, 0x41303, 0x41325, [uint32]0x800710E0
    )
    return ($nonFailure -notcontains $Code)
}

$global:WinFleetProbes['whoami'] = {
    param($a)
    $ident = whoami
    $v = $PSVersionTable.PSVersion.ToString()
    return @{ whoami = $ident; ps_version = $v }
}

$global:WinFleetProbes['server_health'] = {
    param($a)
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    $uptime = (Get-Date) - $os.LastBootUpTime
    $pending = @{
        RebootPending = $false
        CBSRebootPending = $false
        WURebootRequired = $false
        PendingFileRename = $false
        SCCMRebootPending = $false
    }
    try {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
        if (Test-Path $key) { $pending.CBSRebootPending = $true; $pending.RebootPending = $true }
    } catch {}
    try {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
        if (Test-Path $key) { $pending.WURebootRequired = $true; $pending.RebootPending = $true }
    } catch {}
    try {
        $pfr = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
        if ($pfr.PendingFileRenameOperations) { $pending.PendingFileRename = $true; $pending.RebootPending = $true }
    } catch {}
    try {
        $sccm = Get-WmiObject -Namespace root\ccm\ClientSDK -Class CCM_ClientUtilities -ErrorAction SilentlyContinue
        if ($sccm -and ($sccm | Invoke-WmiMethod -Name DetermineIfRebootPending).RebootPending) {
            $pending.SCCMRebootPending = $true; $pending.RebootPending = $true
        }
    } catch {}
    $hf = Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 1
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $totalMem = [int64]$cs.TotalPhysicalMemory
    $freeMem = [int64]$os.FreePhysicalMemory * 1024
    $usedPct = if ($totalMem -gt 0) { [math]::Round(100 * (1 - $freeMem / $totalMem), 1) } else { 0 }
    return @{
        os = $os.Caption
        version = $os.Version
        build = $os.BuildNumber
        domain = $cs.Domain
        uptime_seconds = [int]$uptime.TotalSeconds
        uptime = $uptime.ToString()
        cpu_load_pct = [double]$cpu
        memory_total_bytes = $totalMem
        memory_free_bytes = $freeMem
        memory_used_pct = $usedPct
        reboot_pending = $pending
        last_hotfix = if ($hf) { @{ id = $hf.HotFixID; installed_on = ($hf.InstalledOn).ToString('o') } } else { $null }
    }
}

$global:WinFleetProbes['fleet_overview'] = {
    param($a)
    $diskWarn = if ($a.disk_free_pct_warn) { [double]$a.disk_free_pct_warn } else { 10 }
    $memWarn = if ($a.mem_used_pct_warn) { [double]$a.mem_used_pct_warn } else { 90 }
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    $uptime = (Get-Date) - $os.LastBootUpTime
    $pending = @{
        RebootPending = $false
        CBSRebootPending = $false
        WURebootRequired = $false
        PendingFileRename = $false
        SCCMRebootPending = $false
    }
    try {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
        if (Test-Path $key) { $pending.CBSRebootPending = $true; $pending.RebootPending = $true }
    } catch {}
    try {
        $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
        if (Test-Path $key) { $pending.WURebootRequired = $true; $pending.RebootPending = $true }
    } catch {}
    try {
        $pfr = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
        if ($pfr.PendingFileRenameOperations) { $pending.PendingFileRename = $true; $pending.RebootPending = $true }
    } catch {}
    $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $totalMem = [int64]$cs.TotalPhysicalMemory
    $freeMem = [int64]$os.FreePhysicalMemory * 1024
    $usedPct = if ($totalMem -gt 0) { [math]::Round(100 * (1 - $freeMem / $totalMem), 1) } else { 0 }
    $health = @{
        os = $os.Caption
        version = $os.Version
        build = $os.BuildNumber
        domain = $cs.Domain
        uptime_seconds = [int]$uptime.TotalSeconds
        uptime = $uptime.ToString()
        cpu_load_pct = [double]$cpu
        memory_total_bytes = $totalMem
        memory_free_bytes = $freeMem
        memory_used_pct = $usedPct
        reboot_pending = $pending
        last_hotfix = $null
    }
    $disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
        $freePct = if ($_.Size -gt 0) { [math]::Round(100 * $_.FreeSpace / $_.Size, 1) } else { 0 }
        @{ device = $_.DeviceID; free_pct = $freePct; free_bytes = [int64]$_.FreeSpace; size_bytes = [int64]$_.Size }
    }
    $stoppedAuto = @()
    $stoppedCandidates = Get-CimInstance Win32_Service -Filter "StartMode='Auto' AND State<>'Running'" -ErrorAction SilentlyContinue |
        Select-Object -First 60
    foreach ($svc in $stoppedCandidates) {
        if (Test-WinFleetServiceExpectedStopped $svc.Name) { continue }
        $stoppedAuto += @{
            name = $svc.Name
            state = (ConvertTo-WinFleetString $svc.State)
            display_name = $svc.DisplayName
        }
        if ($stoppedAuto.Count -ge 20) { break }
    }
    $since = (Get-Date).AddHours(-24)
    $errCount = 0
    try {
        $errCount = @(
            Get-WinEvent -FilterHashtable @{
                LogName = 'System', 'Application'
                Level = 1, 2
                StartTime = $since
            } -MaxEvents 500 -ErrorAction SilentlyContinue
        ).Count
    } catch {}
    $alerts = @()
    foreach ($d in $disks) {
        if ($d.free_pct -lt $diskWarn) {
            $alerts += @{ severity = 'warning'; message = "Disco $($d.device) libre $($d.free_pct)%" }
        }
    }
    if ($usedPct -ge $memWarn) {
        $alerts += @{ severity = 'warning'; message = "Memoria usada $usedPct%" }
    }
    if ($pending.RebootPending) {
        $alerts += @{ severity = 'major'; message = 'Reinicio pendiente' }
    }
    return @{
        health = $health
        disks = @($disks)
        stopped_auto_services = @($stoppedAuto)
        event_errors_24h = $errCount
        alerts = @($alerts)
    }
}

$global:WinFleetProbes['disks'] = {
    param($a)
    $includeVol = [bool]$a.include_volumes
    $logical = Get-CimInstance Win32_LogicalDisk | ForEach-Object {
        $freePct = if ($_.Size -gt 0) { [math]::Round(100 * $_.FreeSpace / $_.Size, 1) } else { 0 }
        @{
            device = $_.DeviceID; label = $_.VolumeName; drive_type = $_.DriveType
            size_bytes = [int64]$_.Size; free_bytes = [int64]$_.FreeSpace; free_pct = $freePct
            filesystem = $_.FileSystem
        }
    }
    $vols = @()
    if ($includeVol) {
        $vols = Get-Volume -ErrorAction SilentlyContinue | ForEach-Object {
            @{ drive = $_.DriveLetter; file_system = $_.FileSystemType; size_bytes = [int64]$_.Size; free_bytes = [int64]$_.SizeRemaining }
        }
    }
    return @{ logical = @($logical); volumes = @($vols) }
}

$global:WinFleetProbes['services'] = {
    param($a)
    $name = $a.name
    $state = $a.state
    $startMode = $a.start_mode
    $problemsOnly = [bool]$a.problems_only
    $includeTrigger = $false
    if ($null -ne $a.include_trigger_start) { $includeTrigger = [bool]$a.include_trigger_start }
    $limit = if ($a.limit) { [int]$a.limit } else { 300 }
    if ($problemsOnly) {
        $svcs = Get-CimInstance Win32_Service -Filter "StartMode='Auto' AND State<>'Running'" -ErrorAction SilentlyContinue
        if (-not $includeTrigger) {
            $svcs = $svcs | Where-Object { -not (Test-WinFleetServiceExpectedStopped $_.Name) }
        }
    } else {
        $svcs = Get-CimInstance Win32_Service -ErrorAction SilentlyContinue
    }
    if ($name) {
        $svcs = $svcs | Where-Object { $_.Name -like $name -or $_.DisplayName -like $name }
    }
    if ($state) { $svcs = $svcs | Where-Object { $_.State -eq $state } }
    if ($startMode) { $svcs = $svcs | Where-Object { $_.StartMode -eq $startMode } }
    $out = $svcs | Select-Object -First $limit | ForEach-Object {
        @{
            name = $_.Name
            display_name = $_.DisplayName
            state = (ConvertTo-WinFleetString $_.State)
            start_mode = (ConvertTo-WinFleetString $_.StartMode)
            pid = $_.ProcessId
            account = $_.StartName
            path = $_.PathName
        }
    }
    return @{ services = @($out); count = @($out).Count }
}

$global:WinFleetProbes['processes'] = {
    param($a)
    $name = $a.name
    $sortBy = if ($a.sort_by) { $a.sort_by } else { 'cpu' }
    $top = if ($a.top) { [int]$a.top } else { 25 }
    $procs = Get-Process -ErrorAction SilentlyContinue
    if ($name) { $procs = $procs | Where-Object { $_.ProcessName -like $name } }
    if ($sortBy -eq 'memory') { $procs = $procs | Sort-Object WorkingSet64 -Descending }
    else { $procs = $procs | Sort-Object CPU -Descending }
    $out = $procs | Select-Object -First $top | ForEach-Object {
        $row = @{
            pid = $_.Id; name = $_.ProcessName; cpu = $_.CPU; memory_bytes = [int64]$_.WorkingSet64
        }
        if ($a.include_command_line) {
            try { $row.command_line = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine } catch {}
        }
        if ($a.include_owner) {
            try { $row.owner = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").GetOwner().User } catch {}
        }
        $row
    }
    return @{ processes = @($out) }
}

$global:WinFleetProbes['events'] = {
    param($a)
    $log = if ($a.log) { $a.log } else { 'System' }
    $sinceH = if ($a.since_hours) { [double]$a.since_hours } else { 24 }
    $max = if ($a.max_events) { [int]$a.max_events } else { 50 }
    $levels = @{ critical = 1; error = 2; warning = 3; info = 4 }
    $fh = @{ LogName = $log; StartTime = (Get-Date).AddHours(-$sinceH) }
    if ($a.level -and $levels.ContainsKey($a.level)) { $fh.Level = $levels[$a.level] }
    if ($a.provider) { $fh.ProviderName = $a.provider }
    if ($a.event_id) { $fh.Id = [int]$a.event_id }
    $events = Get-WinEvent -FilterHashtable $fh -MaxEvents $max -ErrorAction SilentlyContinue
    $out = @()
    foreach ($ev in @($events)) {
        $msg = $ev.Message
        if ($a.contains -and $msg -notlike "*$($a.contains)*") { continue }
        if ($msg.Length -gt 2000) { $msg = $msg.Substring(0, 2000) }
        $out += @{
            time = $ev.TimeCreated.ToString('o'); id = $ev.Id; level = $ev.LevelDisplayName
            provider = $ev.ProviderName; message = $msg
        }
    }
    return @{ events = @($out); count = @($out).Count }
}

$global:WinFleetProbes['event_logs_list'] = {
    param($a)
    $contains = $a.contains
    $logs = Get-WinEvent -ListLog * -ErrorAction SilentlyContinue
    if ($contains) { $logs = $logs | Where-Object { $_.LogName -like "*$contains*" } }
    $out = $logs | Select-Object -First 200 | ForEach-Object {
        @{ name = $_.LogName; record_count = [int64]$_.RecordCount; size_bytes = [int64]$_.FileSize; enabled = $_.IsEnabled }
    }
    return @{ logs = @($out) }
}

$global:WinFleetProbes['scheduled_tasks'] = {
    param($a)
    $path = if ($a.path) { $a.path } else { '\' }
    $name = $a.name
    $failedOnly = [bool]$a.failed_only
    $includeMs = [bool]$a.include_microsoft
    $tasks = Get-ScheduledTask -TaskPath "$path*" -ErrorAction SilentlyContinue
    if (-not $includeMs) { $tasks = $tasks | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } }
    if ($name) { $tasks = $tasks | Where-Object { $_.TaskName -like $name } }
    $out = @()
    foreach ($t in $tasks) {
        $info = Get-ScheduledTaskInfo $t -ErrorAction SilentlyContinue
        $resultCode = [uint32]$info.LastTaskResult
        if ($failedOnly -and -not (Test-ScheduledTaskResultIsFailure $resultCode)) { continue }
        $firstAction = $null
        if ($t.Actions -and $t.Actions.Count -gt 0) {
            $act = $t.Actions[0]
            $firstAction = @{ execute = $act.Execute; arguments = $act.Arguments }
        }
        $out += @{
            path = $t.TaskPath
            name = $t.TaskName
            state = (ConvertTo-WinFleetString $t.State)
            last_run = if ($info.LastRunTime) { $info.LastRunTime.ToString('o') } else { $null }
            last_result_hex = ('0x{0:X8}' -f $resultCode)
            last_result_text = (Get-ScheduledTaskResultText $resultCode)
            next_run = if ($info.NextRunTime) { $info.NextRunTime.ToString('o') } else { $null }
            action = $firstAction
        }
    }
    return @{ tasks = @($out) }
}

$global:WinFleetProbes['hotfixes'] = {
    param($a)
    $last = if ($a.last) { [int]$a.last } else { 20 }
    $hfs = Get-HotFix | Sort-Object { $_.InstalledOn } -Descending | Select-Object -First $last
    $items = $hfs | ForEach-Object {
        @{ id = $_.HotFixID; description = $_.Description; installed_by = $_.InstalledBy; installed_on = ($_.InstalledOn).ToString('o') }
    }
    $latest = ($hfs | Select-Object -First 1)
    return @{
        hotfixes = @($items)
        last_installed_on = if ($latest) { ($latest.InstalledOn).ToString('o') } else { $null }
    }
}

$global:WinFleetProbes['software'] = {
    param($a)
    $name = $a.name
    $paths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $items = @()
    foreach ($p in $paths) {
        Get-ItemProperty $p -ErrorAction SilentlyContinue | ForEach-Object {
            if (-not $_.DisplayName) { return }
            if ($name -and $_.DisplayName -notlike "*$name*") { return }
            $items += @{
                name = $_.DisplayName; version = $_.DisplayVersion; publisher = $_.Publisher
                install_date = $_.InstallDate
            }
        }
    }
    return @{ software = @($items); count = @($items).Count }
}

$global:WinFleetProbes['network'] = {
    param($a)
    $adapters = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {
        @{ ip = $_.IPAddress; prefix = $_.PrefixLength; interface = $_.InterfaceAlias }
    }
    $gw = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object -First 3 | ForEach-Object {
        @{ next_hop = $_.NextHop; interface = $_.InterfaceAlias; metric = $_.RouteMetric }
    }
    $dns = Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {
        @{ interface = $_.InterfaceAlias; servers = @($_.ServerAddresses) }
    }
    $listen = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Select-Object -First 100 | ForEach-Object {
        $proc = $null
        try { $proc = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } catch {}
        @{ local = "$($_.LocalAddress):$($_.LocalPort)"; pid = $_.OwningProcess; process = $proc }
    }
    $est = Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Group-Object RemoteAddress |
        Select-Object -First 30 | ForEach-Object { @{ remote = $_.Name; count = $_.Count } }
    return @{ ipv4 = @($adapters); gateways = @($gw); dns = @($dns); listening = @($listen); established_by_remote = @($est) }
}

$global:WinFleetProbes['test_network'] = {
    param($a)
    $target = $a.target
    $port = $a.port
    $ping = Test-Connection -ComputerName $target -Count 2 -Quiet
    $dns = [System.Net.Dns]::GetHostAddresses($target) | ForEach-Object { $_.ToString() }
    $tcp = $null
    if ($port) {
        $tcp = Test-NetConnection -ComputerName $target -Port $port -WarningAction SilentlyContinue |
            Select-Object TcpTestSucceeded, PingSucceeded
    }
    return @{ ping_ok = [bool]$ping; resolved = @($dns); tcp = $tcp }
}

$global:WinFleetProbes['sessions'] = {
    param($a)
    $q = quser 2>$null
    $lines = @()
    if ($LASTEXITCODE -eq 0 -and $q) {
        $lines = $q | Select-Object -Skip 1 | ForEach-Object {
            $parts = ($_ -replace '\s{2,}', '|').Split('|')
            if ($parts.Count -ge 4) {
                @{ user = $parts[0].Trim(); session = $parts[1].Trim(); state = $parts[3].Trim() }
            }
        }
    }
    return @{ sessions = @($lines) }
}

$global:WinFleetProbes['certificates'] = {
    param($a)
    $store = if ($a.store) { $a.store } else { 'My' }
    $days = $a.expiring_days
    $certs = Get-ChildItem "Cert:\LocalMachine\$store" -ErrorAction SilentlyContinue
    if ($days) {
        $limit = (Get-Date).AddDays([int]$days)
        $certs = $certs | Where-Object { $_.NotAfter -le $limit }
    }
    $out = $certs | Select-Object -First 200 | ForEach-Object {
        $d = ($_.NotAfter - (Get-Date)).Days
        @{ subject = $_.Subject; thumbprint = $_.Thumbprint; not_after = $_.NotAfter.ToString('o'); days_remaining = $d }
    }
    return @{ certificates = @($out) }
}

$global:WinFleetProbes['local_admins'] = {
    param($a)
    $members = Get-LocalGroupMember -Group Administrators -ErrorAction SilentlyContinue | ForEach-Object {
        @{ name = $_.Name; object_class = $_.ObjectClass; principal_source = $_.PrincipalSource }
    }
    return @{ members = @($members) }
}

$global:WinFleetProbes['shares'] = {
    param($a)
    $sh = Get-SmbShare -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike '*$' } | ForEach-Object {
        @{ name = $_.Name; path = $_.Path; description = $_.Description }
    }
    return @{ shares = @($sh) }
}

$global:WinFleetProbes['iis'] = {
    param($a)
    if (-not (Get-Module -ListAvailable -Name WebAdministration)) {
        return @{ installed = $false }
    }
    Import-Module WebAdministration -ErrorAction SilentlyContinue
    $sites = Get-Website -ErrorAction SilentlyContinue | ForEach-Object {
        @{ name = $_.Name; state = (ConvertTo-WinFleetString $_.State); bindings = @($_.Bindings.Collection | ForEach-Object { $_.bindingInformation }) }
    }
    $pools = Get-IISAppPool -ErrorAction SilentlyContinue | ForEach-Object {
        @{ name = $_.Name; state = (ConvertTo-WinFleetString $_.State) }
    }
    return @{ installed = $true; sites = @($sites); app_pools = @($pools) }
}

$global:WinFleetProbes['performance'] = {
    param($a)
    $samples = if ($a.samples) { [int]$a.samples } else { 3 }
    $interval = if ($a.interval_seconds) { [int]$a.interval_seconds } else { 1 }
    $counters = @(
        '\Processor(_Total)\% Processor Time',
        '\PhysicalDisk(_Total)\Avg. Disk Queue Length',
        '\Memory\Available MBytes',
        '\Network Interface(*)\Bytes Total/sec'
    )
    $data = Get-Counter -Counter $counters -SampleInterval $interval -MaxSamples $samples -ErrorAction SilentlyContinue
    $avg = @{}
    foreach ($path in $counters) {
        $samplesArr = @()
        foreach ($cs in $data.CounterSamples) {
            if ($cs.Path -like "*$($path.Replace('\','\\'))*") { $samplesArr += $cs.CookedValue }
        }
        if ($samplesArr.Count -gt 0) { $avg[$path] = [math]::Round(($samplesArr | Measure-Object -Average).Average, 2) }
    }
    return @{ averages = $avg; samples = $samples; interval_seconds = $interval }
}

$global:WinFleetProbes['firewall'] = {
    param($a)
    $enabledOnly = if ($null -ne $a.enabled_only) { [bool]$a.enabled_only } else { $true }
    $rules = Get-NetFirewallRule -ErrorAction SilentlyContinue
    if ($enabledOnly) { $rules = $rules | Where-Object { $_.Enabled -eq 'True' } }
    if ($a.direction) { $rules = $rules | Where-Object { $_.Direction -eq $a.direction } }
    if ($a.name) { $rules = $rules | Where-Object { $_.DisplayName -like "*$($a.name)*" } }
    $limit = if ($a.limit) { [int]$a.limit } else { 200 }
    $out = $rules | Select-Object -First $limit | ForEach-Object {
        @{
            name = $_.DisplayName
            enabled = (ConvertTo-WinFleetString $_.Enabled)
            direction = (ConvertTo-WinFleetString $_.Direction)
            action = (ConvertTo-WinFleetString $_.Action)
            profile = (ConvertTo-WinFleetString $_.Profile)
        }
    }
    return @{ rules = @($out); count = @($out).Count }
}

$global:WinFleetProbes['list_dir'] = {
    param($a)
    $path = $a.path
    $pattern = if ($a.pattern) { $a.pattern } else { '*' }
    $recurse = [bool]$a.recurse
    $max = if ($a.max_entries) { [int]$a.max_entries } else { 500 }
    if (-not (Test-Path -LiteralPath $path)) { throw "ruta no encontrada: $path" }
    $items = if ($recurse) {
        Get-ChildItem -LiteralPath $path -Filter $pattern -Recurse -ErrorAction SilentlyContinue | Select-Object -First $max
    } else {
        Get-ChildItem -LiteralPath $path -Filter $pattern -ErrorAction SilentlyContinue | Select-Object -First $max
    }
    $out = $items | ForEach-Object {
        @{
            name = $_.Name; path = $_.FullName; is_directory = $_.PSIsContainer
            size_bytes = if (-not $_.PSIsContainer) { [int64]$_.Length } else { 0 }
            modified = $_.LastWriteTime.ToString('o')
        }
    }
    return @{ entries = @($out); count = @($out).Count; path = $path }
}

$global:WinFleetProbes['read_file'] = {
    param($a)
    $path = $a.path
    $offset = if ($a.offset) { [int]$a.offset } else { 0 }
    $max = if ($a.max_bytes) { [int]$a.max_bytes } else { 1048576 }
    $enc = $a.encoding
    if (-not (Test-Path -LiteralPath $path)) { throw "archivo no encontrado" }
    $fs = [System.IO.File]::OpenRead($path)
    try {
        [void]$fs.Seek($offset, [System.IO.SeekOrigin]::Begin)
        $buf = New-Object byte[] $max
        $read = $fs.Read($buf, 0, $max)
        $bytes = $buf[0..($read - 1)]
    } finally { $fs.Close() }
    if ($bytes -contains 0) { throw 'contenido binario; usa copy_from_server' }
    $text = if ($enc -and $enc -ne 'auto') { [Text.Encoding]::GetEncoding($enc).GetString($bytes) }
    else { [Text.Encoding]::UTF8.GetString($bytes) }
    return @{ content = $text; bytes_read = $read; offset = $offset; path = $path }
}

$global:WinFleetProbes['tail_file'] = {
    param($a)
    $path = $a.path
    $lines = if ($a.lines) { [int]$a.lines } else { 200 }
    $contains = $a.contains
    $content = Get-Content -LiteralPath $path -Tail $lines -ErrorAction Stop
    if ($contains) { $content = $content | Where-Object { $_ -like "*$contains*" } }
    return @{ lines = @($content); count = @($content).Count }
}

$global:WinFleetProbes['search_files'] = {
    param($a)
    $path = $a.path
    $pattern = if ($a.name_pattern) { $a.name_pattern } else { '*' }
    $contains = $a.contains
    $max = if ($a.max_results) { [int]$a.max_results } else { 200 }
    $depth = if ($a.max_depth) { [int]$a.max_depth } else { 8 }
    $found = @()
    function Walk($dir, $d) {
        if ($found.Count -ge $max -or $d -gt $depth) { return }
        Get-ChildItem -LiteralPath $dir -ErrorAction SilentlyContinue | ForEach-Object {
            if ($found.Count -ge $max) { return }
            if ($_.Name -like $pattern) {
                if (-not $contains) { $found += $_.FullName }
                else {
                    if (-not $_.PSIsContainer) {
                        if (Select-String -LiteralPath $_.FullName -Pattern $contains -Quiet -ErrorAction SilentlyContinue) {
                            $found += $_.FullName
                        }
                    }
                }
            }
            if ($_.PSIsContainer) { Walk $_.FullName ($d + 1) }
        }
    }
    Walk $path 0
    return @{ matches = @($found); count = @($found).Count }
}

$global:WinFleetProbes['manage_service'] = {
    param($a)
    $name = $a.name
    $action = $a.action
    $dry = [bool]$a.dry_run
    $svc = Get-Service -Name $name -ErrorAction Stop
    $before = @{ name = $svc.Name; status = $svc.Status.ToString(); start_type = $svc.StartType.ToString() }
    if ($dry) { return @{ dry_run = $true; before = $before } }
    switch ($action) {
        'start' { Start-Service -Name $name -ErrorAction Stop }
        'stop' { Stop-Service -Name $name -Force:( [bool]$a.force ) -ErrorAction Stop }
        'restart' { Restart-Service -Name $name -Force:( [bool]$a.force ) -ErrorAction Stop }
    }
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $svc = Get-Service -Name $name
    } while ((Get-Date) -lt $deadline -and $svc.Status -eq 'StartPending')
    $after = @{ status = $svc.Status.ToString() }
    return @{ before = $before; after = $after }
}

$global:WinFleetProbes['set_service_startmode'] = {
    param($a)
    if ([bool]$a.dry_run) { return @{ dry_run = $true; name = $a.name; start_mode = $a.start_mode } }
    Set-Service -Name $a.name -StartupType $a.start_mode -ErrorAction Stop
    $svc = Get-Service -Name $a.name
    return @{ name = $svc.Name; start_mode = $svc.StartType.ToString() }
}

$global:WinFleetProbes['stop_process'] = {
    param($a)
    if ([bool]$a.dry_run) { return @{ dry_run = $true; pid = $a.pid; name = $a.name } }
    if ($a.pid) { Stop-Process -Id $a.pid -Force -ErrorAction Stop; return @{ stopped_pid = $a.pid } }
    Stop-Process -Name $a.name -Force -ErrorAction Stop
    return @{ stopped_name = $a.name }
}

$global:WinFleetProbes['manage_task'] = {
    param($a)
    $tp = $a.task_path; $tn = $a.task_name; $act = $a.action
    if ([bool]$a.dry_run) { return @{ dry_run = $true; task_path = $tp; task_name = $tn; action = $act } }
    switch ($act) {
        'run' { Start-ScheduledTask -TaskPath $tp -TaskName $tn }
        'stop' { Stop-ScheduledTask -TaskPath $tp -TaskName $tn }
        'enable' { Enable-ScheduledTask -TaskPath $tp -TaskName $tn }
        'disable' { Disable-ScheduledTask -TaskPath $tp -TaskName $tn }
    }
    return @{ task_path = $tp; task_name = $tn; action = $act; done = $true }
}

$global:WinFleetProbes['write_file'] = {
    param($a)
    $path = $a.path
    $content = $a.content
    $append = [bool]$a.append
    $createDirs = [bool]$a.create_dirs
    $backup = [bool]$a.backup
    if ([bool]$a.dry_run) { return @{ dry_run = $true; path = $path; bytes = $content.Length } }
    if ($createDirs) {
        $dir = Split-Path -Parent $path
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    }
    if ($backup -and (Test-Path -LiteralPath $path)) {
        $bak = "$path.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item -LiteralPath $path -Destination $bak -Force
    }
    if ($append) { Add-Content -LiteralPath $path -Value $content -Encoding UTF8 }
    else { Set-Content -LiteralPath $path -Value $content -Encoding UTF8 -NoNewline:$false }
    return @{ path = $path; bytes_written = [Text.Encoding]::UTF8.GetByteCount($content); append = $append }
}

$global:WinFleetProbes['restart_prepare'] = {
    param($a)
    $health = & $global:WinFleetProbes['server_health'] @{}
    $sess = & $global:WinFleetProbes['sessions'] @{}
    $keySvc = Get-Service WinRM, EventLog, LanmanServer -ErrorAction SilentlyContinue | ForEach-Object {
        @{ name = $_.Name; status = $_.Status.ToString() }
    }
    return @{ uptime_seconds = $health.uptime_seconds; sessions = $sess.sessions; key_services = @($keySvc) }
}
