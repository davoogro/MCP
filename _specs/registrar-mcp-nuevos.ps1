# Registra en Claude Code los MCP nuevos de esta ronda (ejecutar en una terminal normal).
# `claude` no está en el PATH: se usa el claude.exe más reciente que trae la app de escritorio.
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    $exe = Get-ChildItem "$env:LOCALAPPDATA\Claude-Data\*\claude-code\*\*\claude.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $exe) { throw 'No se encontró claude.exe en %LOCALAPPDATA%\Claude-Data' }
    Set-Alias claude $exe.FullName
    "Usando $($exe.FullName)"
}
# windows-winrm-fleet: sucesor de windows-guarded-admin (40 tools, sesiones persistentes).
claude mcp add windows-winrm-fleet --scope user `
  -e PYTHONPATH=C:\DVB\mcp\windows-winrm-fleet-mcp\src `
  -- C:\DVB\python\python.exe -m windows_winrm_fleet_mcp

# wsl: reemplaza el archivo suelto wsl_mcp_server.py por el paquete wsl-mcp (22 tools, jobs en segundo plano).
claude mcp remove wsl --scope user
claude mcp add wsl --scope user `
  -e PYTHONPATH=C:\DVB\mcp\wsl-mcp\src -e PYTHONIOENCODING=utf-8 -e PYTHONUNBUFFERED=1 `
  -- C:\DVB\python\python.exe -m wsl_mcp

# Opcional, cuando windows-winrm-fleet te convenza: quitar el viejo.
# claude mcp remove windows-guarded-admin --scope user

claude mcp list
