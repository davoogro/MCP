# Cierra procesos de servidores MCP (C:\DVB\mcp) cuyo proceso padre ya no existe.
# Los MCP de sesiones vivas de Claude/Cursor no se tocan. Excluye el monitor de OPSDB.
$all = Get-CimInstance Win32_Process
$alive = @{}; $all | ForEach-Object { $alive[[int]$_.ProcessId] = $true }
$orphans = $all | Where-Object {
    ($_.Name -in 'python.exe', 'node.exe', 'powershell.exe') -and
    $_.CommandLine -match 'C:\\DVB\\mcp|_mcp' -and
    $_.CommandLine -notmatch 'freshness_monitor' -and
    -not $alive[[int]$_.ParentProcessId]
}
$orphans | Select-Object ProcessId, CreationDate, CommandLine | Format-Table -AutoSize -Wrap
$orphans | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"Cerrados: $($orphans.Count)"
