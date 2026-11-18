# Registra ssh-linux en Claude Code (ejecutar en una terminal normal, no como admin).
# `claude` no está en el PATH: se usa el claude.exe más reciente que trae la app de escritorio.
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    $exe = Get-ChildItem "$env:LOCALAPPDATA\Claude-Data\*\claude-code\*\*\claude.exe" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $exe) { throw 'No se encontró claude.exe en %LOCALAPPDATA%\Claude-Data' }
    Set-Alias claude $exe.FullName
    "Usando $($exe.FullName)"
}
claude mcp add ssh-linux --scope user `
  -e PYTHONPATH=C:\DVB\mcp\ssh-linux-mcp\src -e PYTHONIOENCODING=utf-8 -e PYTHONUNBUFFERED=1 `
  -- C:\DVB\python\python.exe -m ssh_linux_mcp

claude mcp list
