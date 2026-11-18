"""MCP de administración (lectura + escritura) de Windows Server vía SSH + PowerShell.

Uso:
  python -m windows_ssh_admin_mcp               # MCP server (stdio)
  python -m windows_ssh_admin_mcp --test [host] # probar conexión
  python -m windows_ssh_admin_mcp --handshake   # listar tools vía cliente MCP in-process
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from windows_ssh_admin_mcp import __version__
from windows_ssh_admin_mcp.config import MAX_CHARS, STATE_DIR, TRANSFER_DIR, Host, Settings
from windows_ssh_admin_mcp.transport import PsSession, RemoteError, Result, scp

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("windows-ssh-admin-mcp")

SETTINGS = Settings.load()
SESSIONS: dict[str, PsSession] = {}
AUDIT_LOG = STATE_DIR / "audit.jsonl"
JOB_DIR = r"C:\ProgramData\windows-ssh-admin-mcp\jobs"

ANN = {
    "read": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    "write": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
}

mcp = MCPServer(
    "windows-ssh-admin",
    title="Windows Server vía SSH",
    version=__version__,
    instructions=(
        "Administración completa (lectura y escritura) de Windows Server por SSH + PowerShell 5.1, con "
        "una sesión persistente por host (primer comando ~4 s por el ProxyJump, luego ~0.1 s). Hosts: "
        + "; ".join(f"'{h.id}' = {h.description}" for h in SETTINGS.hosts.values())
        + f". Host por defecto: '{SETTINGS.default}' (parámetro opcional 'host'). El usuario SSH es "
        "administrador local: los cambios son reales e inmediatos. Flujo: test_connection -> get_system_info "
        "-> tool específica; usa run_powershell solo cuando no exista una tool (devuelve texto; con Out-Json "
        "<obj> devuelve JSON en 'data'; castea a [string] las líneas de Get-Content antes de Out-Json o se cuelga). Cada script corre en un scope nuevo: las variables no persisten entre "
        "llamadas. No ejecutes programas interactivos (leerían el stdin de la sesión); run_cmd ya redirige "
        "<NUL. Tareas largas (instalaciones, Windows Update, copias grandes): run_background (tarea programada "
        "como SYSTEM, sobrevive a la sesión) -> get_background_job. Archivos: write_file (texto, hace backup "
        ".bak-<fecha>), upload_file/download_file (scp, binarios). Las tools destructivas (borrar, detener "
        "procesos, desinstalar, reiniciar el servidor...) exigen confirm=true: pide confirmación explícita al "
        "usuario antes. Antes de tocar red/firewall/servicio sshd ten en cuenta que puedes cortar tu propio "
        "acceso. Todo queda en query_audit_log."
    ),
)


# ============================================================
# Infra
# ============================================================


def _session(h: Host) -> PsSession:
    if h.id not in SESSIONS:
        SESSIONS[h.id] = PsSession(h.ssh_alias)
    return SESSIONS[h.id]


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _ps(host: Optional[str], body: str, params: Optional[dict[str, Any]] = None, timeout: float = 120,
        write: bool = False, strict: bool = True) -> tuple[Host, Result]:
    h = SETTINGS.get(host)
    if write and not h.allow_write:
        raise PermissionError(f"El host '{h.id}' tiene allow_write=false en hosts.json")
    head = "$ErrorActionPreference='Stop'\n" if strict else "$ErrorActionPreference='Continue'\n"
    if params is not None:
        head += (f"$P = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(json.dumps(params))}'))"
                 " | ConvertFrom-Json\n")
    return h, _session(h).run(head + body, timeout=timeout)


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} caracteres omitidos] ...\n{text[-half:]}"


def _fit(data: Any) -> tuple[Any, bool]:
    """Recorta listas grandes para que la respuesta no exceda MAX_CHARS."""
    if isinstance(data, list) and len(json.dumps(data, default=str)) > MAX_CHARS:
        lo, hi = 0, len(data)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(data[:mid], default=str)) <= MAX_CHARS:
                lo = mid
            else:
                hi = mid - 1
        return data[:lo], True
    return data, False


def _data(host: Optional[str], body: str, params: Optional[dict[str, Any]] = None, timeout: float = 120,
          write: bool = False) -> dict[str, Any]:
    h, r = _ps(host, body, params, timeout, write)
    out: dict[str, Any] = {"ok": r.ok, "host": h.id}
    if not r.ok:
        out["exit_code"] = r.code
    if r.json is not None:
        data, truncated = _fit(r.json)
        if isinstance(data, list):
            out["count"] = len(data)
        out["data"] = data
        if truncated:
            out["truncated"] = True
            out["hint"] = "Respuesta recortada: filtra o baja 'limit'."
    if r.stdout.strip():
        out["stdout"] = _clip(r.stdout.rstrip())
    if r.stderr.strip():
        out["stderr" if r.ok else "error"] = _clip(r.stderr.rstrip(), 8000)
    return out


def _text(host: Optional[str], body: str, params: Optional[dict[str, Any]] = None, timeout: float = 120,
          write: bool = False, strict: bool = False) -> dict[str, Any]:
    h, r = _ps(host, body, params, timeout, write, strict)
    out = {"host": h.id, **r.as_dict()}
    if "stdout" in out:
        out["stdout"] = _clip(out["stdout"])
    if "stderr" in out:
        out["stderr"] = _clip(out["stderr"], 15000)
    if r.json is not None:
        out["data"] = _fit(r.json)[0]
    return out


def _need_confirm(confirm: bool, what: str) -> Optional[dict[str, Any]]:
    if confirm:
        return None
    return {"ok": False, "confirmation_required": True,
            "message": f"Operación destructiva: {what}. Confirma con el usuario y repite con confirm=true."}


def _audit(tool: str, kind: str, args: dict[str, Any], ok: bool, err: Optional[str], dur: float) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        safe = {}
        for k, v in args.items():
            s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
            if isinstance(s, str) and len(s) > 400:
                s = s[:400] + f"...(+{len(s) - 400})"
            safe[k] = s
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "tool": tool, "kind": kind, "args": safe,
               "ok": ok, "error": err, "duration_s": round(dur, 2)}
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("No se pudo escribir el audit log")


def tool(kind: Literal["read", "write", "destructive"]):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(**kwargs):
            t0 = time.monotonic()
            ok, err = True, None
            try:
                res = fn(**kwargs)
                if isinstance(res, dict) and res.get("ok") is False:
                    ok, err = False, str(res.get("error") or res.get("message") or "")[:300]
                return res
            except (RemoteError, TimeoutError, PermissionError, ValueError, OSError) as exc:
                ok, err = False, f"{type(exc).__name__}: {exc}"
                return {"ok": False, "error": err}
            finally:
                _audit(fn.__name__, kind, kwargs, ok, err, time.monotonic() - t0)
        return mcp.tool(annotations=ANN[kind])(wrapper)
    return deco


# ============================================================
# Sesión y ejecución
# ============================================================


@tool("read")
def list_hosts() -> dict[str, Any]:
    """Hosts configurados (config/hosts.json), alias SSH, si permiten escritura y si hay sesión abierta."""
    return {"default": SETTINGS.default, "hosts": [
        {"id": h.id, "ssh_alias": h.ssh_alias, "description": h.description, "allow_write": h.allow_write,
         "session_open": h.id in SESSIONS and SESSIONS[h.id].started_at is not None}
        for h in SETTINGS.hosts.values()]}


@tool("read")
def test_connection(host: Optional[str] = None, reconnect: bool = False) -> dict[str, Any]:
    """Abre (o reabre con reconnect=true) la sesión SSH y devuelve identidad, admin y versión de PowerShell."""
    h = SETTINGS.get(host)
    if reconnect:
        _session(h).close()
    t0 = time.monotonic()
    res = _data(host, r"""
$id=[Security.Principal.WindowsIdentity]::GetCurrent()
Out-Json ([ordered]@{computer=$env:COMPUTERNAME; user=$id.Name;
  is_admin=([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);
  ps_version=$PSVersionTable.PSVersion.ToString(); os=(Get-CimInstance Win32_OperatingSystem).Caption})
""")
    res["elapsed_s"] = round(time.monotonic() - t0, 2)
    return res


@tool("write")
def run_powershell(script: str, host: Optional[str] = None, timeout_s: int = 120,
                   stop_on_error: bool = False) -> dict[str, Any]:
    """
    Ejecuta un script PowerShell 5.1 arbitrario en el servidor (como admin). Devuelve stdout (formateado
    como en consola, ancho 300), stderr (errores/warnings) y exit_code. Usa `Out-Json <objeto>` para
    devolver datos estructurados en 'data'. stop_on_error=true pone $ErrorActionPreference='Stop'.
    timeout_s máx. 3600; para más usa run_background. No uses programas interactivos.
    """
    return _text(host, script, timeout=min(max(timeout_s, 5), 3600), write=True, strict=stop_on_error)


@tool("write")
def run_cmd(command: str, host: Optional[str] = None, timeout_s: int = 120) -> dict[str, Any]:
    """Ejecuta una línea de cmd.exe (stderr unido a stdout, stdin <NUL). Útil para sc, netsh, dism, reg, etc."""
    return _text(host, "cmd.exe /d /c ($P.cmd + ' 2>&1 <NUL')", {"cmd": command},
                 timeout=min(max(timeout_s, 5), 3600), write=True)


@tool("write")
def run_background(script: str, name: str = "job", timeout_hours: float = 4,
                   host: Optional[str] = None) -> dict[str, Any]:
    """
    Lanza un script PowerShell como tarea programada (SYSTEM, privilegios máximos) que sobrevive a la sesión
    SSH: instalaciones, Windows Update, copias o descargas largas. Salida en C:\\ProgramData\\windows-ssh-admin-mcp\\jobs.
    Ojo: SYSTEM no tiene credenciales de red del usuario (shares UNC autenticados fallarán).
    Devuelve job_id para get_background_job.
    """
    job_id = f"{''.join(c for c in name if c.isalnum() or c in '-_')[:30] or 'job'}-{datetime.now():%Y%m%d-%H%M%S}"
    return _data(host, r"""
$dir=$env:JOB_DIR_PLACEHOLDER
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$base=Join-Path $dir $P.id
[IO.File]::WriteAllText("$base.ps1", $P.script, (New-Object Text.UTF8Encoding($true)))
$wrapper = @'
$ErrorActionPreference = 'Continue'
$base = '__BASE__'
"[$(Get-Date -Format s)] inicio" | Out-File "$base.log" -Encoding utf8
$code = 0
try { & "$base.ps1" *>&1 | Out-File "$base.log" -Append -Encoding utf8 -Width 300; if (-not $?) { $code = 1 }; if ($LASTEXITCODE) { $code = $LASTEXITCODE } }
catch { $_ | Out-File "$base.log" -Append -Encoding utf8 -Width 300; $code = 1 }
"[$(Get-Date -Format s)] fin, exit $code" | Out-File "$base.log" -Append -Encoding utf8
Set-Content "$base.exit" $code
'@
[IO.File]::WriteAllText("$base.wrapper.ps1", $wrapper.Replace('__BASE__', $base), (New-Object Text.UTF8Encoding($true)))
$act=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$base.wrapper.ps1`""
$pr=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::FromHours([double]$P.hours)) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $P.id -TaskPath '\windows-ssh-admin-mcp\' -Action $act -Principal $pr -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName $P.id -TaskPath '\windows-ssh-admin-mcp\'
Out-Json ([ordered]@{job_id=$P.id; log="$base.log"; task="\windows-ssh-admin-mcp\$($P.id)"})
""".replace("$env:JOB_DIR_PLACEHOLDER", f"'{JOB_DIR}'"),
                 {"id": job_id, "script": script, "hours": timeout_hours}, write=True)


@tool("read")
def get_background_job(job_id: str, tail_lines: int = 80, host: Optional[str] = None) -> dict[str, Any]:
    """Estado (Running/Ready), exit_code (si terminó) y últimas líneas del log de un job de run_background."""
    return _data(host, rf"""
$base=Join-Path '{JOB_DIR}' $P.id
$t=Get-ScheduledTask -TaskName $P.id -TaskPath '\windows-ssh-admin-mcp\' -ErrorAction SilentlyContinue
$exit= if(Test-Path "$base.exit"){{ (Get-Content "$base.exit" -Raw).Trim() }} else {{ $null }}
$log= if(Test-Path "$base.log"){{ (Get-Content "$base.log" -Tail $P.tail) -join "`n" }} else {{ '' }}
Out-Json ([ordered]@{{job_id=$P.id; task_state= if($t){{[string]$t.State}}else{{'(no existe)'}}; finished=($null -ne $exit);
  exit_code=$exit; log_tail=$log}})
""", {"id": job_id, "tail": tail_lines})


@tool("read")
def list_background_jobs(host: Optional[str] = None) -> dict[str, Any]:
    """Jobs lanzados con run_background (tareas en \\windows-ssh-admin-mcp\\) con estado y exit code."""
    return _data(host, rf"""
Out-Json @(Get-ScheduledTask -TaskPath '\windows-ssh-admin-mcp\' -ErrorAction SilentlyContinue | ForEach-Object {{
  $i=$_ | Get-ScheduledTaskInfo; $e=Join-Path '{JOB_DIR}' "$($_.TaskName).exit"
  [ordered]@{{job_id=$_.TaskName; state=[string]$_.State; last_run=(Fmt-Date $i.LastRunTime);
    exit_code= if(Test-Path $e){{(Get-Content $e -Raw).Trim()}}else{{$null}}}} }})
""")


@tool("destructive")
def remove_background_job(job_id: str, host: Optional[str] = None) -> dict[str, Any]:
    """Detiene (si corre) y elimina la tarea de un job y sus archivos (script, log, exit)."""
    return _data(host, rf"""
Stop-ScheduledTask -TaskName $P.id -TaskPath '\windows-ssh-admin-mcp\' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $P.id -TaskPath '\windows-ssh-admin-mcp\' -Confirm:$false -ErrorAction SilentlyContinue
Get-ChildItem '{JOB_DIR}' -Filter "$($P.id).*" -ErrorAction SilentlyContinue | Remove-Item -Force
Out-Json @{{removed=$P.id}}
""", {"id": job_id}, write=True)


# ============================================================
# Sistema
# ============================================================


@tool("read")
def get_system_info(host: Optional[str] = None) -> dict[str, Any]:
    """Resumen del servidor: SO/build, dominio, hardware, RAM, uptime, IPs, zona horaria, reinicio pendiente, roles instalados."""
    return _data(host, r"""
$os=Get-CimInstance Win32_OperatingSystem; $cs=Get-CimInstance Win32_ComputerSystem
$cbs='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion'
$pending=(Test-Path "$cbs\Component Based Servicing\RebootPending") -or (Test-Path "$cbs\WindowsUpdate\Auto Update\RebootRequired") -or
  [bool](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue)
$roles=@(); try { $roles=@(Get-WindowsFeature | Where-Object { $_.Installed -and $_.FeatureType -eq 'Role' } | ForEach-Object Name) } catch {}
Out-Json ([ordered]@{hostname=$env:COMPUTERNAME; domain=$cs.Domain; part_of_domain=$cs.PartOfDomain;
  os=$os.Caption; version=$os.Version; build=$os.BuildNumber; install_date=(Fmt-Date $os.InstallDate);
  last_boot=(Fmt-Date $os.LastBootUpTime); uptime_h=[math]::Round(((Get-Date)-$os.LastBootUpTime).TotalHours,1);
  manufacturer=$cs.Manufacturer; model=$cs.Model; cpu=@(Get-CimInstance Win32_Processor | ForEach-Object Name | Select-Object -Unique);
  sockets=@(Get-CimInstance Win32_Processor).Count;
  logical_cpus=$cs.NumberOfLogicalProcessors; ram_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,1);
  free_ram_gb=[math]::Round($os.FreePhysicalMemory/1MB,1);
  ipv4=@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -ne '127.0.0.1' | ForEach-Object { "$($_.InterfaceAlias)=$($_.IPAddress)/$($_.PrefixLength)" });
  timezone=(Get-TimeZone).Id; local_time=(Get-Date -Format s); pending_reboot=$pending; roles=$roles;
  ps_version=$PSVersionTable.PSVersion.ToString(); dotnet_release=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction SilentlyContinue).Release})
""")


@tool("read")
def get_performance(top: int = 10, host: Optional[str] = None) -> dict[str, Any]:
    """Carga actual: % CPU total, memoria, paginación, top procesos por CPU y por memoria."""
    return _data(host, r"""
$os=Get-CimInstance Win32_OperatingSystem
$pp=Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object { $_.Name -notin '_Total','Idle' }
$cpu=(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
$pf=Get-CimInstance Win32_PageFileUsage
Out-Json ([ordered]@{cpu_pct=$cpu; mem_total_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,1);
  mem_free_gb=[math]::Round($os.FreePhysicalMemory/1MB,1);
  mem_used_pct=[math]::Round(100-100*$os.FreePhysicalMemory/$os.TotalVisibleMemorySize,1);
  pagefile=@($pf | ForEach-Object { [ordered]@{name=$_.Name; size_mb=$_.AllocatedBaseSize; used_mb=$_.CurrentUsage} });
  processes=(Get-Process).Count;
  top_cpu=@($pp | Sort-Object PercentProcessorTime -Descending | Select-Object -First $P.top | ForEach-Object { [ordered]@{name=$_.Name; pid=$_.IDProcess; cpu_pct=$_.PercentProcessorTime; ws_mb=[math]::Round($_.WorkingSetPrivate/1MB,1)} });
  top_mem=@($pp | Sort-Object WorkingSetPrivate -Descending | Select-Object -First $P.top | ForEach-Object { [ordered]@{name=$_.Name; pid=$_.IDProcess; ws_private_mb=[math]::Round($_.WorkingSetPrivate/1MB,1)} })})
""", {"top": top})


@tool("read")
def get_disks(host: Optional[str] = None) -> dict[str, Any]:
    """Volúmenes (tamaño/libre/%), discos físicos y particiones."""
    return _data(host, r"""
Out-Json ([ordered]@{
 volumes=@(Get-Volume | Where-Object { $_.DriveLetter -or $_.FileSystemLabel } | ForEach-Object { [ordered]@{drive=[string]$_.DriveLetter;
   label=$_.FileSystemLabel; fs=$_.FileSystem; size_gb=[math]::Round($_.Size/1GB,2); free_gb=[math]::Round($_.SizeRemaining/1GB,2);
   free_pct= if($_.Size){[math]::Round(100*$_.SizeRemaining/$_.Size,1)}else{$null}; health=[string]$_.HealthStatus} });
 disks=@(Get-Disk | ForEach-Object { [ordered]@{number=$_.Number; name=$_.FriendlyName; size_gb=[math]::Round($_.Size/1GB,2);
   style=[string]$_.PartitionStyle; status=[string]$_.OperationalStatus; online=-not $_.IsOffline; readonly=$_.IsReadOnly;
   unallocated_gb=[math]::Round(($_.Size-$_.AllocatedSize)/1GB,2)} });
 partitions=@(Get-Partition | ForEach-Object { [ordered]@{disk=$_.DiskNumber; number=$_.PartitionNumber; drive=[string]$_.DriveLetter;
   size_gb=[math]::Round($_.Size/1GB,2); type=[string]$_.Type} })})
""")


@tool("write")
def extend_volume(drive_letter: str, host: Optional[str] = None) -> dict[str, Any]:
    """Extiende un volumen al máximo espacio contiguo disponible (tras ampliar el disco en el hipervisor)."""
    return _data(host, r"""
Update-HostStorageCache
$s=Get-PartitionSupportedSize -DriveLetter $P.d
$before=(Get-Partition -DriveLetter $P.d).Size
if ($s.SizeMax -gt $before + 1MB) { Resize-Partition -DriveLetter $P.d -Size $s.SizeMax }
Out-Json ([ordered]@{drive=$P.d; before_gb=[math]::Round($before/1GB,2); after_gb=[math]::Round((Get-Partition -DriveLetter $P.d).Size/1GB,2)})
""", {"d": drive_letter.strip(":")}, timeout=300, write=True)


@tool("read")
def get_events(log: str = "System", level: Optional[Literal["critical", "error", "warning", "info"]] = None,
               hours: float = 24, event_id: Optional[int] = None, provider: Optional[str] = None,
               contains: Optional[str] = None, max_events: int = 100, host: Optional[str] = None) -> dict[str, Any]:
    """
    Eventos de un log (System, Application, Security, Setup o cualquiera de list_event_logs) en las últimas
    'hours'. level filtra ese nivel y más graves (error = critical+error). contains filtra el mensaje.
    """
    lv = {"critical": [1], "error": [1, 2], "warning": [1, 2, 3], "info": [1, 2, 3, 4, 0]}
    return _data(host, r"""
$f=@{LogName=$P.log; StartTime=(Get-Date).AddHours(-[double]$P.hours)}
if ($P.levels) { $f.Level=[int[]]$P.levels }
if ($P.id) { $f.Id=[int]$P.id }
if ($P.provider) { $f.ProviderName=$P.provider }
try { $ev=@(Get-WinEvent -FilterHashtable $f -MaxEvents ([int]$P.max * $(if($P.contains){5}else{1}))) }
catch { if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $ev=@() } else { throw } }
if ($P.contains) { $ev=@($ev | Where-Object { $_.Message -like "*$($P.contains)*" } | Select-Object -First $P.max) }
Out-Json @($ev | ForEach-Object { $m=[string]$_.Message; [ordered]@{time=(Fmt-Date $_.TimeCreated); level=$_.LevelDisplayName;
  id=$_.Id; provider=$_.ProviderName; message= if($m.Length -gt 800){$m.Substring(0,800)+'...'}else{$m}} })
""", {"log": log, "levels": lv[level] if level else None, "hours": hours, "id": event_id, "provider": provider,
      "contains": contains, "max": max_events}, timeout=180)


@tool("read")
def list_event_logs(name_filter: Optional[str] = None, host: Optional[str] = None) -> dict[str, Any]:
    """Logs de eventos con registros (nombre, nº de registros, tamaño, habilitado). name_filter admite comodines."""
    return _data(host, r"""
$l=Get-WinEvent -ListLog $(if($P.f){$P.f}else{'*'}) -ErrorAction SilentlyContinue | Where-Object RecordCount -gt 0
Out-Json @($l | Sort-Object RecordCount -Descending | ForEach-Object { [ordered]@{name=$_.LogName; records=$_.RecordCount;
  size_mb=[math]::Round($_.FileSize/1MB,1); max_mb=[math]::Round($_.MaximumSizeInBytes/1MB,1); enabled=$_.IsEnabled} })
""", {"f": name_filter})


@tool("destructive")
def restart_server(confirm: bool = False, delay_s: int = 60, reason: str = "Reinicio solicitado vía MCP",
                   host: Optional[str] = None) -> dict[str, Any]:
    """Programa el reinicio del servidor (shutdown /r) en delay_s segundos. Requiere confirm=true. Luego usa wait_for_server."""
    if (c := _need_confirm(confirm, f"reiniciar el servidor en {delay_s}s")):
        return c
    res = _text(host, "shutdown.exe /r /t $P.t /d p:4:1 /c $P.r", {"t": max(delay_s, 0), "r": reason[:500]}, write=True)
    _session(SETTINGS.get(host)).close()
    res["hint"] = "Puedes abortarlo con cancel_restart antes de que venza el plazo."
    return res


@tool("write")
def cancel_restart(host: Optional[str] = None) -> dict[str, Any]:
    """Aborta un reinicio/apagado pendiente (shutdown /a)."""
    return _text(host, "shutdown.exe /a", write=True)


@tool("read")
def wait_for_server(host: Optional[str] = None, timeout_s: int = 900, initial_wait_s: int = 30) -> dict[str, Any]:
    """Espera (sondeando cada 15 s) a que el servidor vuelva a aceptar SSH tras un reinicio; devuelve uptime."""
    h = SETTINGS.get(host)
    time.sleep(max(0, initial_wait_s))
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        _session(h).close()
        try:
            _, r = _ps(host, "Out-Json @{boot=(Fmt-Date (Get-CimInstance Win32_OperatingSystem).LastBootUpTime)}",
                       timeout=60)
            if r.ok:
                return {"ok": True, "host": h.id, "data": r.json}
        except (RemoteError, TimeoutError) as exc:
            last = str(exc)
        time.sleep(15)
    return {"ok": False, "host": h.id, "error": f"No volvió en {timeout_s}s. Último error: {last}"}


# ============================================================
# Servicios y procesos
# ============================================================


@tool("read")
def get_services(name: Optional[str] = None, state: Optional[Literal["Running", "Stopped"]] = None,
                 start_mode: Optional[Literal["Auto", "Manual", "Disabled"]] = None, include_path: bool = False,
                 host: Optional[str] = None) -> dict[str, Any]:
    """Servicios (nombre o display name con comodines, p.ej. '*sql*'), estado, inicio, cuenta, PID."""
    return _data(host, r"""
$s=Get-CimInstance Win32_Service
if ($P.name) { $s=$s | Where-Object { $_.Name -like $P.name -or $_.DisplayName -like $P.name } }
if ($P.state) { $s=$s | Where-Object State -eq $P.state }
if ($P.mode) { $s=$s | Where-Object StartMode -eq $P.mode }
Out-Json @($s | Sort-Object Name | ForEach-Object { $o=[ordered]@{name=$_.Name; display=$_.DisplayName; state=$_.State;
  start_mode=$_.StartMode; delayed=$_.DelayedAutoStart; account=$_.StartName; pid=$_.ProcessId}
  if ($P.path) { $o.path=$_.PathName; $o.description=$_.Description }; $o })
""", {"name": name, "state": state, "mode": start_mode, "path": include_path})


@tool("write")
def manage_service(name: str, action: Literal["start", "stop", "restart", "pause", "resume"],
                   force: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Arranca/detiene/reinicia un servicio. force=true detiene también servicios dependientes. No toques sshd sin pensarlo."""
    return _data(host, r"""
switch ($P.action) {
 'start' { Start-Service -Name $P.name }
 'stop' { Stop-Service -Name $P.name -Force:$P.force }
 'restart' { Restart-Service -Name $P.name -Force:$P.force }
 'pause' { Suspend-Service -Name $P.name }
 'resume' { Resume-Service -Name $P.name }
}
$s=Get-Service -Name $P.name
Out-Json ([ordered]@{name=$s.Name; status=[string]$s.Status; start_type=[string]$s.StartType})
""", {"name": name, "action": action, "force": force}, timeout=180, write=True)


@tool("write")
def configure_service(name: str, start_type: Optional[Literal["Automatic", "AutomaticDelayed", "Manual", "Disabled"]] = None,
                      display_name: Optional[str] = None, description: Optional[str] = None,
                      failure_restart: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Cambia tipo de inicio, display name, descripción y/o configura reinicio automático ante fallo (3x a 60 s)."""
    return _data(host, r"""
if ($P.st -eq 'AutomaticDelayed') { Set-Service -Name $P.name -StartupType Automatic; & sc.exe config $P.name start= delayed-auto | Out-Null }
elseif ($P.st) { Set-Service -Name $P.name -StartupType $P.st }
if ($P.dn) { Set-Service -Name $P.name -DisplayName $P.dn }
if ($P.desc) { Set-Service -Name $P.name -Description $P.desc }
if ($P.fr) { & sc.exe failure $P.name reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null }
$s=Get-CimInstance Win32_Service -Filter "Name='$($P.name.Replace("'","''"))'"
Out-Json ([ordered]@{name=$s.Name; display=$s.DisplayName; start_mode=$s.StartMode; delayed=$s.DelayedAutoStart; state=$s.State})
""", {"name": name, "st": start_type, "dn": display_name, "desc": description, "fr": failure_restart}, write=True)


@tool("write")
def create_service(name: str, binary_path: str, display_name: Optional[str] = None,
                   start_type: Literal["Automatic", "Manual", "Disabled"] = "Automatic",
                   description: Optional[str] = None, start_now: bool = False,
                   host: Optional[str] = None) -> dict[str, Any]:
    """Crea un servicio Windows (New-Service, cuenta LocalSystem). binary_path con argumentos y comillas si hay espacios."""
    return _data(host, r"""
$a=@{Name=$P.name; BinaryPathName=$P.bin; StartupType=$P.st; DisplayName=$(if($P.dn){$P.dn}else{$P.name})}
if ($P.desc) { $a.Description=$P.desc }
New-Service @a | Out-Null
if ($P.start) { Start-Service -Name $P.name }
$s=Get-Service -Name $P.name
Out-Json ([ordered]@{name=$s.Name; status=[string]$s.Status; start_type=[string]$s.StartType})
""", {"name": name, "bin": binary_path, "dn": display_name, "st": start_type, "desc": description, "start": start_now},
                 write=True)


@tool("destructive")
def delete_service(name: str, confirm: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Detiene y elimina un servicio (sc delete). Requiere confirm=true."""
    if (c := _need_confirm(confirm, f"eliminar el servicio '{name}'")):
        return c
    return _text(host, "Stop-Service -Name $P.n -Force -ErrorAction SilentlyContinue; sc.exe delete $P.n",
                 {"n": name}, write=True)


@tool("read")
def get_processes(name: Optional[str] = None, sort_by: Literal["cpu", "memory", "name", "start"] = "memory",
                  top: int = 50, include_path: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Procesos (filtro por nombre con comodines) con PID, CPU acumulada, memoria, usuario, inicio y opcionalmente ruta/cmdline."""
    return _data(host, r"""
$pr=Get-Process -IncludeUserName
if ($P.name) { $pr=$pr | Where-Object Name -like $P.name }
$key=@{cpu='CPU'; memory='WorkingSet64'; name='Name'; start='StartTime'}[$P.sort]
$pr=$pr | Sort-Object $key -Descending:($P.sort -ne 'name') | Select-Object -First $P.top
$cim=@{}; if ($P.path) { Get-CimInstance Win32_Process | ForEach-Object { $cim[[int]$_.ProcessId]=$_.CommandLine } }
Out-Json @($pr | ForEach-Object { $o=[ordered]@{name=$_.Name; pid=$_.Id; cpu_s=[math]::Round($_.CPU,1);
  ws_mb=[math]::Round($_.WorkingSet64/1MB,1); user=$_.UserName; start=(Fmt-Date $_.StartTime); session=$_.SessionId}
  if ($P.path) { $o.path=$_.Path; $o.cmdline=$cim[$_.Id] }; $o })
""", {"name": name, "sort": sort_by, "top": top, "path": include_path})


@tool("destructive")
def stop_process(pid: Optional[int] = None, name: Optional[str] = None, confirm: bool = False,
                 host: Optional[str] = None) -> dict[str, Any]:
    """Mata procesos por PID o nombre (Stop-Process -Force). Requiere confirm=true."""
    if pid is None and not name:
        return {"ok": False, "error": "Indica pid o name"}
    if (c := _need_confirm(confirm, f"matar proceso {pid or name}")):
        return c
    return _data(host, r"""
$t= if($P.pid){ @(Get-Process -Id $P.pid) } else { @(Get-Process -Name $P.name) }
$t | Stop-Process -Force
Out-Json @($t | ForEach-Object { [ordered]@{name=$_.Name; pid=$_.Id} })
""", {"pid": pid, "name": name}, write=True)


# ============================================================
# Archivos
# ============================================================


@tool("read")
def list_directory(path: str, pattern: str = "*", recurse: bool = False, depth: int = 2,
                   include_hidden: bool = False, limit: int = 500, host: Optional[str] = None) -> dict[str, Any]:
    """Lista una carpeta (o recursivo hasta 'depth'): nombre, tipo, tamaño, modificado. path admite variables %ENV% / $env."""
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$a=@{LiteralPath=$path; Filter=$P.pattern; Force=$P.hidden}
if ($P.recurse) { $a.Recurse=$true; $a.Depth=$P.depth }
$items=@(Get-ChildItem @a | Select-Object -First $P.limit)
Out-Json @($items | ForEach-Object { [ordered]@{name= if($P.recurse){$_.FullName}else{$_.Name};
  type= if($_.PSIsContainer){'dir'}else{'file'}; size= if($_.PSIsContainer){$null}else{$_.Length};
  modified=(Fmt-Date $_.LastWriteTime); attributes=[string]$_.Attributes} })
""", {"path": path, "pattern": pattern, "recurse": recurse, "depth": depth, "hidden": include_hidden, "limit": limit})


@tool("read")
def read_file(path: str, head_lines: Optional[int] = None, tail_lines: Optional[int] = None,
              max_bytes: int = 1_000_000, host: Optional[str] = None) -> dict[str, Any]:
    """Lee un archivo de texto completo (hasta max_bytes) o sus primeras/últimas N líneas. Detecta encoding por BOM."""
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$f=Get-Item -LiteralPath $path
if ($P.tail) { $c=(Get-Content -LiteralPath $path -Tail $P.tail) -join "`n" }
elseif ($P.head) { $c=(Get-Content -LiteralPath $path -TotalCount $P.head) -join "`n" }
elseif ($f.Length -gt $P.max) { throw "Archivo de $($f.Length) bytes > max_bytes=$($P.max): usa head_lines/tail_lines o download_file" }
else { $c=[IO.File]::ReadAllText($f.FullName) }
Out-Json ([ordered]@{path=$f.FullName; size=$f.Length; modified=(Fmt-Date $f.LastWriteTime); content=$c})
""", {"path": path, "head": head_lines, "tail": tail_lines, "max": max_bytes})


@tool("write")
def write_file(path: str, content: str, append: bool = False,
               encoding: Literal["utf8", "utf8bom", "ascii", "unicode", "default"] = "utf8",
               backup: bool = True, line_endings: Literal["crlf", "lf", "keep"] = "crlf",
               host: Optional[str] = None) -> dict[str, Any]:
    """
    Escribe (o añade) texto a un archivo, creando carpetas si faltan. Si existe y backup=true, guarda copia
    <archivo>.bak-<fecha>. encoding 'default' = ANSI del sistema. line_endings crlf convierte \\n a \\r\\n.
    """
    if line_endings == "crlf":
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    elif line_endings == "lf":
        content = content.replace("\r\n", "\n")
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$text=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($P.b64))
$enc=switch($P.enc){ 'utf8'{New-Object Text.UTF8Encoding($false)} 'utf8bom'{New-Object Text.UTF8Encoding($true)}
  'ascii'{[Text.Encoding]::ASCII} 'unicode'{[Text.Encoding]::Unicode} default{[Text.Encoding]::Default} }
$dir=Split-Path -Parent $path; if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$bak=$null
if ((Test-Path -LiteralPath $path) -and $P.backup) { $bak="$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"; Copy-Item -LiteralPath $path $bak }
if ($P.append) { [IO.File]::AppendAllText($path, $text, $enc) } else { [IO.File]::WriteAllText($path, $text, $enc) }
$f=Get-Item -LiteralPath $path
Out-Json ([ordered]@{path=$f.FullName; size=$f.Length; backup=$bak})
""", {"path": path, "b64": _b64(content), "enc": encoding, "append": append, "backup": backup},
                 timeout=300, write=True)


@tool("write")
def replace_in_file(path: str, old: str, new: str, regex: bool = False, count: int = 0, backup: bool = True,
                    host: Optional[str] = None) -> dict[str, Any]:
    """
    Reemplaza texto en un archivo conservando su encoding y su BOM (o la ausencia de BOM). regex=false = literal.
    count=0 reemplaza todas; si no hay coincidencias falla sin tocar el archivo. Ideal para editar configs (.ini, .config, .xml).
    Sin BOM: UTF-8 si los bytes son UTF-8 válido, si no ANSI del sistema.
    """
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
# StreamReader.CurrentEncoding devuelve UTF-8 *con* preámbulo aunque el archivo no tenga BOM: detectar por bytes.
$b=[IO.File]::ReadAllBytes($path)
if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) { $enc=New-Object Text.UTF8Encoding($true); $skip=3 }
elseif ($b.Length -ge 2 -and $b[0] -eq 0xFF -and $b[1] -eq 0xFE) { $enc=New-Object Text.UnicodeEncoding($false, $true); $skip=2 }
elseif ($b.Length -ge 2 -and $b[0] -eq 0xFE -and $b[1] -eq 0xFF) { $enc=New-Object Text.UnicodeEncoding($true, $true); $skip=2 }
else {
  $skip=0
  try { [void](New-Object Text.UTF8Encoding($false, $true)).GetString($b); $enc=New-Object Text.UTF8Encoding($false) }
  catch { $enc=[Text.Encoding]::Default }
}
$text=$enc.GetString($b, $skip, $b.Length - $skip)
$pat= if($P.regex){$P.old}else{[regex]::Escape($P.old)}
$rx=New-Object regex($pat)
$n=$rx.Matches($text).Count
if ($n -eq 0) { throw "Sin coincidencias de '$($P.old)' en $path" }
$repl= if($P.regex){$P.new}else{$P.new.Replace('$','$$')}
$max= if($P.count -gt 0){$P.count}else{-1}
$out=$rx.Replace($text, $repl, $max)
$bak=$null; if ($P.backup) { $bak="$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"; Copy-Item -LiteralPath $path $bak }
[IO.File]::WriteAllText($path, $out, $enc)
Out-Json ([ordered]@{path=$path; matches=$n; replaced= if($P.count -gt 0){[math]::Min($n,$P.count)}else{$n}; encoding=$enc.WebName; bom=($skip -gt 0); backup=$bak})
""", {"path": path, "old": old, "new": new, "regex": regex, "count": count, "backup": backup}, write=True)


@tool("destructive")
def file_operation(action: Literal["copy", "move", "rename", "mkdir", "delete"], path: str,
                   destination: Optional[str] = None, recurse: bool = False, overwrite: bool = False,
                   confirm: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """
    Operaciones de archivo/carpeta: copy/move (destination), rename (destination = nombre nuevo), mkdir,
    delete (recurse para carpetas; requiere confirm=true; borra de verdad, sin papelera).
    """
    if action == "delete" and (c := _need_confirm(confirm, f"borrar {path}")):
        return c
    if action in ("copy", "move", "rename") and not destination:
        return {"ok": False, "error": "destination es obligatorio para copy/move/rename"}
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$dst= if($P.dst){[Environment]::ExpandEnvironmentVariables($P.dst)}else{$null}
switch ($P.action) {
 'copy' { Copy-Item -LiteralPath $path -Destination $dst -Recurse:$P.recurse -Force:$P.overwrite }
 'move' { Move-Item -LiteralPath $path -Destination $dst -Force:$P.overwrite }
 'rename' { Rename-Item -LiteralPath $path -NewName $dst }
 'mkdir' { New-Item -ItemType Directory -Force -Path $path | Out-Null }
 'delete' { Remove-Item -LiteralPath $path -Recurse:$P.recurse -Force }
}
Out-Json ([ordered]@{action=$P.action; path=$path; destination=$dst; exists_after=(Test-Path -LiteralPath $path)})
""", {"action": action, "path": path, "dst": destination, "recurse": recurse, "overwrite": overwrite},
                 timeout=600, write=True)


@tool("read")
def search_files(path: str, name_pattern: str = "*", content_regex: Optional[str] = None,
                 recurse: bool = True, limit: int = 200, newer_than_hours: Optional[float] = None,
                 host: Optional[str] = None) -> dict[str, Any]:
    """Busca archivos por nombre (comodines) y opcionalmente por contenido (regex, devuelve línea y número)."""
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$f=Get-ChildItem -LiteralPath $path -Filter $P.pat -File -Recurse:$P.recurse -Force -ErrorAction SilentlyContinue
if ($P.newer) { $lim=(Get-Date).AddHours(-[double]$P.newer); $f=$f | Where-Object LastWriteTime -gt $lim }
if ($P.rx) {
 Out-Json @($f | Select-String -Pattern $P.rx -ErrorAction SilentlyContinue | Select-Object -First $P.limit | ForEach-Object {
   $l=$_.Line; [ordered]@{path=$_.Path; line=$_.LineNumber; text= if($l.Length -gt 300){$l.Substring(0,300)}else{$l}} })
} else {
 Out-Json @($f | Select-Object -First $P.limit | ForEach-Object { [ordered]@{path=$_.FullName; size=$_.Length; modified=(Fmt-Date $_.LastWriteTime)} })
}
""", {"path": path, "pat": name_pattern, "rx": content_regex, "recurse": recurse, "limit": limit,
      "newer": newer_than_hours}, timeout=300)


@tool("read")
def get_file_info(path: str, hash: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Metadatos de archivo/carpeta: tamaño (total si es carpeta), fechas, atributos, versión, propietario, ACL y SHA256 opcional."""
    return _data(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$i=Get-Item -LiteralPath $path -Force; $acl=Get-Acl -LiteralPath $path
$o=[ordered]@{path=$i.FullName; type= if($i.PSIsContainer){'dir'}else{'file'}; created=(Fmt-Date $i.CreationTime);
  modified=(Fmt-Date $i.LastWriteTime); attributes=[string]$i.Attributes; owner=$acl.Owner;
  acl=@($acl.Access | ForEach-Object { "$($_.IdentityReference): $($_.AccessControlType) $($_.FileSystemRights)$(if($_.IsInherited){' (heredado)'})" })}
if ($i.PSIsContainer) { $m=Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum; $o.files=$m.Count; $o.size=$m.Sum }
else { $o.size=$i.Length; $o.version=$i.VersionInfo.FileVersion; $o.product=$i.VersionInfo.ProductName
  if ($P.hash) { $o.sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash } }
Out-Json $o
""", {"path": path, "hash": hash}, timeout=300)


@tool("write")
def set_permissions(path: str, identity: str, rights: Literal["F", "M", "RX", "R", "W"] = "M",
                    action: Literal["grant", "deny", "remove"] = "grant", inherit: bool = True,
                    recurse: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """
    Permisos NTFS vía icacls. rights: F=total, M=modificar, RX=leer y ejecutar, R=leer, W=escribir.
    inherit=true aplica (OI)(CI) a subcarpetas/archivos; recurse=true añade /T. remove quita las entradas de la identidad.
    """
    return _text(host, r"""
$path=[Environment]::ExpandEnvironmentVariables($P.path)
$spec= if($P.inherit){"(OI)(CI)$($P.rights)"}else{$P.rights}
$a=@($path)
switch ($P.action) { 'grant' { $a+='/grant'; $a+="$($P.id):$spec" } 'deny' { $a+='/deny'; $a+="$($P.id):$spec" } 'remove' { $a+='/remove'; $a+=$P.id } }
if ($P.recurse) { $a+='/T' }
$a+='/C'
& icacls.exe @a
""", {"path": path, "id": identity, "rights": rights, "action": action, "inherit": inherit, "recurse": recurse},
                 timeout=600, write=True)


def _remote_scp_path(h: Host, path: str) -> str:
    p = path.replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p
    return f"{h.ssh_alias}:{p}"


@tool("write")
def upload_file(local_path: str, remote_path: str, recurse: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Sube un archivo (o carpeta con recurse=true) local a remote_path vía scp (binarios, instaladores, zips)."""
    h = SETTINGS.get(host)
    if not h.allow_write:
        raise PermissionError(f"El host '{h.id}' tiene allow_write=false")
    if not os.path.exists(local_path):
        return {"ok": False, "error": f"No existe localmente: {local_path}"}
    t0 = time.monotonic()
    _ps(host, "$d=Split-Path -Parent $P.p; if ($d -and -not (Test-Path -LiteralPath $d)) "
              "{ New-Item -ItemType Directory -Force -Path $d | Out-Null }", {"p": remote_path}, write=True)
    r = scp(local_path, _remote_scp_path(h, remote_path), recursive=recurse)
    out = {"ok": r.returncode == 0, "host": h.id, "local": local_path, "remote": remote_path,
           "bytes": os.path.getsize(local_path) if os.path.isfile(local_path) else None,
           "duration_s": round(time.monotonic() - t0, 1)}
    if r.returncode != 0:
        out["error"] = r.stderr.decode("utf-8", "replace").strip()
    return out


@tool("read")
def download_file(remote_path: str, local_path: Optional[str] = None, recurse: bool = False,
                  host: Optional[str] = None) -> dict[str, Any]:
    """Descarga un archivo/carpeta remota vía scp. Sin local_path se guarda en C:\\DVB\\exports\\windows-ssh-admin\\<host>\\."""
    h = SETTINGS.get(host)
    if not local_path:
        target = TRANSFER_DIR / h.id
        target.mkdir(parents=True, exist_ok=True)
        local_path = str(target / Path(remote_path.replace("\\", "/")).name)
    else:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    r = scp(_remote_scp_path(h, remote_path), local_path, recursive=recurse)
    out = {"ok": r.returncode == 0, "host": h.id, "remote": remote_path, "local": local_path,
           "duration_s": round(time.monotonic() - t0, 1)}
    if r.returncode != 0:
        out["error"] = r.stderr.decode("utf-8", "replace").strip()
    elif os.path.isfile(local_path):
        out["bytes"] = os.path.getsize(local_path)
    return out


# ============================================================
# Roles, software, actualizaciones
# ============================================================


@tool("read")
def get_windows_features(installed_only: bool = True, name: Optional[str] = None,
                         host: Optional[str] = None) -> dict[str, Any]:
    """Roles y características (Get-WindowsFeature). name admite comodines, p.ej. '*IIS*' o 'Web-*'."""
    return _data(host, r"""
$f=Get-WindowsFeature
if ($P.inst) { $f=$f | Where-Object Installed }
if ($P.name) { $f=$f | Where-Object { $_.Name -like $P.name -or $_.DisplayName -like $P.name } }
Out-Json @($f | ForEach-Object { [ordered]@{name=$_.Name; display=$_.DisplayName; installed=$_.Installed;
  state=[string]$_.InstallState; type=$_.FeatureType; parent=$_.Parent} })
""", {"inst": installed_only, "name": name}, timeout=180)


@tool("write")
def install_windows_feature(names: list[str], include_management_tools: bool = True, source: Optional[str] = None,
                            host: Optional[str] = None) -> dict[str, Any]:
    """Instala roles/características (Install-WindowsFeature). Nunca reinicia solo: revisa restart_needed."""
    return _data(host, r"""
$a=@{Name=[string[]]$P.names; IncludeManagementTools=$P.mgmt}
if ($P.src) { $a.Source=$P.src }
$r=Install-WindowsFeature @a
Out-Json ([ordered]@{success=$r.Success; restart_needed=[string]$r.RestartNeeded; exit_code=[string]$r.ExitCode;
  installed=@($r.FeatureResult | ForEach-Object DisplayName)})
""", {"names": names, "mgmt": include_management_tools, "src": source}, timeout=3600, write=True)


@tool("destructive")
def uninstall_windows_feature(names: list[str], confirm: bool = False, remove_payload: bool = False,
                              host: Optional[str] = None) -> dict[str, Any]:
    """Desinstala roles/características. Requiere confirm=true. No reinicia solo."""
    if (c := _need_confirm(confirm, f"desinstalar {', '.join(names)}")):
        return c
    return _data(host, r"""
$r=Uninstall-WindowsFeature -Name ([string[]]$P.names) -Remove:$P.rm
Out-Json ([ordered]@{success=$r.Success; restart_needed=[string]$r.RestartNeeded; exit_code=[string]$r.ExitCode;
  removed=@($r.FeatureResult | ForEach-Object DisplayName)})
""", {"names": names, "rm": remove_payload}, timeout=3600, write=True)


@tool("read")
def get_installed_software(name: Optional[str] = None, host: Optional[str] = None) -> dict[str, Any]:
    """Software instalado (registro Uninstall 64/32 bits): nombre, versión, editor, fecha, comando de desinstalación."""
    return _data(host, r"""
$k='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
$s=Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and -not $_.SystemComponent }
if ($P.name) { $s=$s | Where-Object DisplayName -like $P.name }
Out-Json @($s | Sort-Object DisplayName -Unique | ForEach-Object { [ordered]@{name=$_.DisplayName; version=$_.DisplayVersion;
  publisher=$_.Publisher; installed=$_.InstallDate; uninstall=$(if($_.QuietUninstallString){$_.QuietUninstallString}else{$_.UninstallString})} })
""", {"name": name})


@tool("read")
def get_updates(host: Optional[str] = None, limit: int = 30) -> dict[str, Any]:
    """Hotfixes/KB instalados (más recientes primero) y configuración de Windows Update (WSUS, AU)."""
    return _data(host, r"""
$wu=Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -ErrorAction SilentlyContinue
$au=Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction SilentlyContinue
Out-Json ([ordered]@{wsus_server=$wu.WUServer; au_options=$au.AUOptions; no_auto_update=$au.NoAutoUpdate;
  hotfixes=@(Get-HotFix | Sort-Object { try { [datetime]$_.InstalledOn } catch { [datetime]::MinValue } } -Descending |
    Select-Object -First $P.limit | ForEach-Object { [ordered]@{id=$_.HotFixID; description=$_.Description; installed_on=(Fmt-Date $_.InstalledOn); by=$_.InstalledBy} })})
""", {"limit": limit})


_WU_SCRIPT = r"""
$s=New-Object -ComObject Microsoft.Update.Session
$r=$s.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
"Pendientes: $($r.Updates.Count)"
foreach ($u in $r.Updates) { "- $($u.Title)" }
if ('__MODE__' -eq 'install' -and $r.Updates.Count -gt 0) {
  $c=New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $r.Updates) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$c.Add($u) }
  $d=$s.CreateUpdateDownloader(); $d.Updates=$c; "Descargando..."; $dr=$d.Download(); "Descarga: $($dr.ResultCode)"
  $i=$s.CreateUpdateInstaller(); $i.Updates=$c; "Instalando..."; $ir=$i.Install()
  "Instalación: ResultCode=$($ir.ResultCode) (2=OK, 3=OK con errores, 4=fallo) RebootRequired=$($ir.RebootRequired)"
  for ($n=0; $n -lt $c.Count; $n++) { "  [$($ir.GetUpdateResult($n).ResultCode)] $($c.Item($n).Title)" }
}
"""


@tool("write")
def windows_update(action: Literal["scan", "install"] = "scan", host: Optional[str] = None) -> dict[str, Any]:
    """
    Busca (scan) o descarga+instala (install) actualizaciones pendientes vía el agente de Windows Update.
    Corre siempre como job en segundo plano (SYSTEM); sigue el progreso con get_background_job. No reinicia.
    """
    return run_background(script=_WU_SCRIPT.replace("__MODE__", action), name=f"wu-{action}",
                          timeout_hours=6, host=host)


# ============================================================
# Red y firewall
# ============================================================


@tool("read")
def get_network(include_routes: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Adaptadores, IPs, gateways, DNS, perfil de red y opcionalmente tabla de rutas IPv4."""
    return _data(host, r"""
$o=[ordered]@{
 adapters=@(Get-NetAdapter | ForEach-Object { [ordered]@{name=$_.Name; desc=$_.InterfaceDescription; status=[string]$_.Status;
   mac=$_.MacAddress; speed=$_.LinkSpeed; index=$_.ifIndex} });
 ip_config=@(Get-NetIPConfiguration | ForEach-Object { [ordered]@{interface=$_.InterfaceAlias;
   ipv4=@($_.IPv4Address | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" }); gateway=@($_.IPv4DefaultGateway | ForEach-Object NextHop);
   dns=@($_.DNSServer | Where-Object AddressFamily -eq 2 | ForEach-Object ServerAddresses); dhcp=[string]$_.NetIPv4Interface.Dhcp} });
 profiles=@(Get-NetConnectionProfile | ForEach-Object { [ordered]@{interface=$_.InterfaceAlias; name=$_.Name; category=[string]$_.NetworkCategory} });
 dns_suffix=(Get-DnsClientGlobalSetting).SuffixSearchList; hosts_file_entries=@([IO.File]::ReadAllLines("$env:windir\System32\drivers\etc\hosts") | Where-Object { $_ -match '^\s*[^#\s]' })}
if ($P.routes) { $o.routes=@(Get-NetRoute -AddressFamily IPv4 | ForEach-Object { [ordered]@{dest=$_.DestinationPrefix; next_hop=$_.NextHop; interface=$_.InterfaceAlias; metric=$_.RouteMetric} }) }
Out-Json $o
""", {"routes": include_routes})


@tool("read")
def get_listening_ports(port: Optional[int] = None, host: Optional[str] = None) -> dict[str, Any]:
    """Puertos TCP en escucha y UDP abiertos con el proceso/servicio dueño."""
    return _data(host, r"""
$pn=@{}; Get-Process | ForEach-Object { $pn[$_.Id]=$_.Name }
$sv=@{}; Get-CimInstance Win32_Service -Filter "State='Running'" | ForEach-Object { $sv[[int]$_.ProcessId]+= @($_.Name) }
$t=Get-NetTCPConnection -State Listen | ForEach-Object { [ordered]@{proto='TCP'; address=$_.LocalAddress; port=$_.LocalPort; pid=$_.OwningProcess; process=$pn[[int]$_.OwningProcess]; services=$sv[[int]$_.OwningProcess]} }
$u=Get-NetUDPEndpoint | ForEach-Object { [ordered]@{proto='UDP'; address=$_.LocalAddress; port=$_.LocalPort; pid=$_.OwningProcess; process=$pn[[int]$_.OwningProcess]; services=$sv[[int]$_.OwningProcess]} }
$all=@($t)+@($u)
if ($P.port) { $all=@($all | Where-Object { $_.port -eq $P.port }) }
Out-Json @($all | Sort-Object { $_.proto }, { [int]$_.port })
""", {"port": port})


@tool("read")
def get_tcp_connections(remote_port: Optional[int] = None, local_port: Optional[int] = None,
                        state: str = "Established", limit: int = 300, host: Optional[str] = None) -> dict[str, Any]:
    """Conexiones TCP activas (estado, origen/destino, proceso). Útil para ver quién se conecta a un servicio."""
    return _data(host, r"""
$pn=@{}; Get-Process | ForEach-Object { $pn[$_.Id]=$_.Name }
$c=Get-NetTCPConnection -State $P.state -ErrorAction SilentlyContinue
if ($P.rp) { $c=$c | Where-Object RemotePort -eq $P.rp }
if ($P.lp) { $c=$c | Where-Object LocalPort -eq $P.lp }
Out-Json @($c | Select-Object -First $P.limit | ForEach-Object { [ordered]@{local="$($_.LocalAddress):$($_.LocalPort)";
  remote="$($_.RemoteAddress):$($_.RemotePort)"; state=[string]$_.State; pid=$_.OwningProcess; process=$pn[[int]$_.OwningProcess]} })
""", {"rp": remote_port, "lp": local_port, "state": state, "limit": limit})


@tool("read")
def test_network(target: str, port: Optional[int] = None, host: Optional[str] = None) -> dict[str, Any]:
    """Desde el servidor: resuelve DNS y prueba ping / puerto TCP hacia target (Test-NetConnection)."""
    return _data(host, r"""
$dns=@(); try { $dns=@(Resolve-DnsName $P.t -ErrorAction Stop | Where-Object IPAddress | ForEach-Object IPAddress) } catch {}
$a=@{ComputerName=$P.t; WarningAction='SilentlyContinue'}; if ($P.port) { $a.Port=$P.port }
$r=Test-NetConnection @a
Out-Json ([ordered]@{target=$P.t; resolved=$dns; remote_address=[string]$r.RemoteAddress; ping=$r.PingSucceeded;
  ping_ms=$r.PingReplyDetails.RoundtripTime; port=$P.port; tcp_ok=$(if($P.port){$r.TcpTestSucceeded}else{$null}); source_ip=[string]$r.SourceAddress.IPAddress})
""", {"t": target, "port": port}, timeout=90)


@tool("read")
def get_firewall_rules(name: Optional[str] = None, enabled_only: bool = True,
                       direction: Optional[Literal["Inbound", "Outbound"]] = None, port: Optional[int] = None,
                       limit: int = 150, host: Optional[str] = None) -> dict[str, Any]:
    """Perfiles del firewall y reglas (filtro por nombre con comodines, dirección o puerto local) con puertos/programa/remotos."""
    return _data(host, r"""
$r=Get-NetFirewallRule
if ($P.name) { $r=$r | Where-Object { $_.DisplayName -like $P.name -or $_.Name -like $P.name } }
if ($P.en) { $r=$r | Where-Object Enabled -eq 'True' }
if ($P.dir) { $r=$r | Where-Object Direction -eq $P.dir }
$rows=foreach ($x in $r) {
  $pf=$x | Get-NetFirewallPortFilter
  if ($P.port -and -not (@($pf.LocalPort) -contains [string]$P.port)) { continue }
  $af=$x | Get-NetFirewallAddressFilter; $ap=$x | Get-NetFirewallApplicationFilter
  [ordered]@{name=$x.Name; display=$x.DisplayName; enabled=[string]$x.Enabled; direction=[string]$x.Direction; action=[string]$x.Action;
    profile=[string]$x.Profile; protocol=[string]$pf.Protocol; local_port=@($pf.LocalPort) -join ','; remote_address=@($af.RemoteAddress) -join ',';
    program=$ap.Program; group=$x.DisplayGroup}
  if (++$n -ge $P.limit) { break }
}
Out-Json ([ordered]@{profiles=@(Get-NetFirewallProfile | ForEach-Object { [ordered]@{name=$_.Name; enabled=[string]$_.Enabled;
  inbound=[string]$_.DefaultInboundAction; outbound=[string]$_.DefaultOutboundAction} }); rules=@($rows)})
""", {"name": name, "en": enabled_only, "dir": direction, "port": port, "limit": limit}, timeout=180)


@tool("write")
def add_firewall_rule(display_name: str, local_port: Optional[str] = None,
                      protocol: Literal["TCP", "UDP", "ICMPv4", "Any"] = "TCP",
                      direction: Literal["Inbound", "Outbound"] = "Inbound", action: Literal["Allow", "Block"] = "Allow",
                      remote_address: Optional[str] = None, program: Optional[str] = None,
                      profile: str = "Any", description: Optional[str] = None,
                      host: Optional[str] = None) -> dict[str, Any]:
    """Crea una regla de firewall. local_port: '443', '8080-8090' o '80,443'. remote_address: 'LocalSubnet', IP, CIDR, lista con comas."""
    return _data(host, r"""
$a=@{DisplayName=$P.dn; Direction=$P.dir; Action=$P.act; Protocol=$P.proto; Profile=$P.prof}
if ($P.port) { $a.LocalPort=[string[]]($P.port -split ',') }
if ($P.ra) { $a.RemoteAddress=[string[]]($P.ra -split ',') }
if ($P.prog) { $a.Program=$P.prog }
if ($P.desc) { $a.Description=$P.desc }
$r=New-NetFirewallRule @a
Out-Json ([ordered]@{name=$r.Name; display=$r.DisplayName; enabled=[string]$r.Enabled; direction=[string]$r.Direction; action=[string]$r.Action})
""", {"dn": display_name, "port": local_port, "proto": protocol, "dir": direction, "act": action,
      "ra": remote_address, "prog": program, "prof": profile, "desc": description}, write=True)


@tool("destructive")
def manage_firewall_rule(display_name: str, action: Literal["enable", "disable", "remove"], confirm: bool = False,
                         host: Optional[str] = None) -> dict[str, Any]:
    """Habilita, deshabilita o elimina reglas por DisplayName (comodines). remove y disable requieren confirm=true."""
    if action != "enable" and (c := _need_confirm(confirm, f"{action} regla(s) '{display_name}'")):
        return c
    return _data(host, r"""
$r=@(Get-NetFirewallRule -DisplayName $P.dn)
switch ($P.action) { 'enable' { $r | Enable-NetFirewallRule } 'disable' { $r | Disable-NetFirewallRule } 'remove' { $r | Remove-NetFirewallRule } }
Out-Json @($r | ForEach-Object { [ordered]@{name=$_.Name; display=$_.DisplayName; action=$P.action} })
""", {"dn": display_name, "action": action}, write=True)


# ============================================================
# Tareas programadas
# ============================================================


@tool("read")
def get_scheduled_tasks(name: Optional[str] = None, include_microsoft: bool = False,
                        host: Optional[str] = None) -> dict[str, Any]:
    """Tareas programadas (por defecto sin \\Microsoft\\): estado, última/próxima ejecución, resultado, acción y usuario."""
    return _data(host, r"""
$t=Get-ScheduledTask
if (-not $P.ms) { $t=$t | Where-Object TaskPath -notlike '\Microsoft\*' }
if ($P.name) { $t=$t | Where-Object { $_.TaskName -like $P.name -or "$($_.TaskPath)$($_.TaskName)" -like $P.name } }
Out-Json @($t | ForEach-Object { $i=$_ | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
  [ordered]@{path="$($_.TaskPath)$($_.TaskName)"; state=[string]$_.State; user=$_.Principal.UserId; last_run=(Fmt-Date $i.LastRunTime);
   last_result=$i.LastTaskResult; next_run=(Fmt-Date $i.NextRunTime);
   actions=@($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)".Trim() });
   triggers=@($_.Triggers | ForEach-Object { ($_.CimClass.CimClassName -replace 'MSFT_Task|Trigger','') + $(if($_.StartBoundary){" "+$_.StartBoundary}) })} })
""", {"name": name, "ms": include_microsoft})


@tool("destructive")
def manage_scheduled_task(task_path: str, action: Literal["run", "stop", "enable", "disable", "delete"],
                          confirm: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Ejecuta/detiene/habilita/deshabilita/borra una tarea. task_path completo '\\Carpeta\\Nombre'. delete requiere confirm=true."""
    if action == "delete" and (c := _need_confirm(confirm, f"borrar tarea {task_path}")):
        return c
    return _data(host, r"""
$i=$P.tp.LastIndexOf('\'); $tp= if($i -ge 0){$P.tp.Substring(0,$i+1)}else{'\'}; $tn=$P.tp.Substring($i+1)
if (-not $tp.StartsWith('\')) { $tp='\'+$tp }
$a=@{TaskName=$tn; TaskPath=$tp}
switch ($P.action) { 'run' { Start-ScheduledTask @a } 'stop' { Stop-ScheduledTask @a } 'enable' { Enable-ScheduledTask @a | Out-Null }
  'disable' { Disable-ScheduledTask @a | Out-Null } 'delete' { Unregister-ScheduledTask @a -Confirm:$false } }
$t=Get-ScheduledTask @a -ErrorAction SilentlyContinue
Out-Json ([ordered]@{task="$tp$tn"; action=$P.action; state= if($t){[string]$t.State}else{'(eliminada)'}})
""", {"tp": task_path, "action": action}, write=True)


@tool("write")
def create_scheduled_task(name: str, execute: str, arguments: Optional[str] = None,
                          working_directory: Optional[str] = None,
                          trigger: Literal["once", "daily", "weekly", "startup", "logon"] = "daily",
                          at: str = "03:00", days_of_week: Optional[list[str]] = None,
                          repeat_minutes: Optional[int] = None, run_as: str = "SYSTEM", folder: str = "\\",
                          description: Optional[str] = None, overwrite: bool = False,
                          host: Optional[str] = None) -> dict[str, Any]:
    """
    Crea una tarea programada. execute p.ej. 'powershell.exe' con arguments '-NoProfile -File C:\\x.ps1'.
    at = 'HH:mm' o fecha-hora para 'once'; days_of_week ['Monday',...] para weekly; repeat_minutes repite
    durante 1 día desde 'at'. run_as: SYSTEM, 'NETWORK SERVICE' o 'LOCAL SERVICE' (sin contraseñas).
    """
    return _data(host, r"""
$aa=@{Execute=$P.exe}; if ($P.args) { $aa.Argument=$P.args }; if ($P.wd) { $aa.WorkingDirectory=$P.wd }
$act=New-ScheduledTaskAction @aa
$at=[datetime]$P.at
$tr=switch ($P.trig) { 'once' { New-ScheduledTaskTrigger -Once -At $at } 'daily' { New-ScheduledTaskTrigger -Daily -At $at }
  'weekly' { New-ScheduledTaskTrigger -Weekly -At $at -DaysOfWeek ([string[]]$P.dow) } 'startup' { New-ScheduledTaskTrigger -AtStartup }
  'logon' { New-ScheduledTaskTrigger -AtLogOn } }
if ($P.rep) { $tmp=New-ScheduledTaskTrigger -Once -At $at -RepetitionInterval (New-TimeSpan -Minutes $P.rep) -RepetitionDuration (New-TimeSpan -Days 1); $tr.Repetition=$tmp.Repetition }
$pr=New-ScheduledTaskPrincipal -UserId $P.user -LogonType ServiceAccount -RunLevel Highest
$set=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$ra=@{TaskName=$P.name; TaskPath=$P.folder; Action=$act; Trigger=$tr; Principal=$pr; Settings=$set; Force=$P.ow}
if ($P.desc) { $ra.Description=$P.desc }
$t=Register-ScheduledTask @ra
$i=$t | Get-ScheduledTaskInfo
Out-Json ([ordered]@{task="$($t.TaskPath)$($t.TaskName)"; state=[string]$t.State; next_run=(Fmt-Date $i.NextRunTime)})
""", {"name": name, "exe": execute, "args": arguments, "wd": working_directory, "trig": trigger, "at": at,
      "dow": days_of_week or ["Monday"], "rep": repeat_minutes, "user": run_as, "folder": folder,
      "desc": description, "ow": overwrite}, write=True)


# ============================================================
# Usuarios, registro, entorno, certificados, recursos compartidos
# ============================================================


@tool("read")
def get_local_users_groups(host: Optional[str] = None) -> dict[str, Any]:
    """Usuarios locales (habilitado, último logon, expiración) y grupos locales con sus miembros."""
    return _data(host, r"""
Out-Json ([ordered]@{
 users=@(Get-LocalUser | ForEach-Object { [ordered]@{name=$_.Name; enabled=$_.Enabled; last_logon=(Fmt-Date $_.LastLogon);
   password_last_set=(Fmt-Date $_.PasswordLastSet); password_expires=(Fmt-Date $_.PasswordExpires); description=$_.Description} });
 groups=@(Get-LocalGroup | ForEach-Object { $m=@(); try { $m=@(Get-LocalGroupMember -Group $_.Name -ErrorAction Stop | ForEach-Object Name) } catch { $m=@('(error leyendo miembros)') }
   if ($m.Count) { [ordered]@{name=$_.Name; members=$m} } });
 sessions=@(cmd.exe /d /c "query user 2>nul <nul")})
$global:LASTEXITCODE=0
""")


@tool("write")
def manage_local_group_member(group: str, member: str, action: Literal["add", "remove"] = "add",
                              host: Optional[str] = None) -> dict[str, Any]:
    """Añade/quita un usuario o grupo (local o 'DOMINIO\\usuario') de un grupo local (Administrators, Remote Desktop Users...)."""
    return _data(host, r"""
if ($P.action -eq 'add') { Add-LocalGroupMember -Group $P.g -Member $P.m } else { Remove-LocalGroupMember -Group $P.g -Member $P.m }
Out-Json ([ordered]@{group=$P.g; members=@(Get-LocalGroupMember -Group $P.g | ForEach-Object Name)})
""", {"g": group, "m": member, "action": action}, write=True)


@tool("write")
def manage_local_user(name: str, action: Literal["create", "enable", "disable", "delete", "set_description"],
                      description: Optional[str] = None, password_never_expires: bool = False,
                      confirm: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """
    Crea (sin contraseña; el usuario la pondrá después), habilita, deshabilita o borra (confirm=true) un usuario local.
    Las contraseñas no pasan por aquí: pídele al usuario que la fije él en el servidor (net user <name> *).
    """
    if action == "delete" and (c := _need_confirm(confirm, f"borrar usuario local {name}")):
        return c
    return _data(host, r"""
switch ($P.action) {
 'create' { $a=@{Name=$P.n; NoPassword=$true}; if ($P.d) { $a.Description=$P.d }; New-LocalUser @a | Out-Null; if ($P.pne) { Set-LocalUser -Name $P.n -PasswordNeverExpires $true } }
 'enable' { Enable-LocalUser -Name $P.n } 'disable' { Disable-LocalUser -Name $P.n } 'delete' { Remove-LocalUser -Name $P.n }
 'set_description' { Set-LocalUser -Name $P.n -Description $P.d }
}
$u=Get-LocalUser -Name $P.n -ErrorAction SilentlyContinue
Out-Json ([ordered]@{name=$P.n; exists=[bool]$u; enabled=$u.Enabled; description=$u.Description})
""", {"n": name, "action": action, "d": description, "pne": password_never_expires}, write=True)


@tool("read")
def get_registry(path: str, recurse_depth: int = 0, host: Optional[str] = None) -> dict[str, Any]:
    """Valores y subclaves de una clave de registro, p.ej. 'HKLM:\\SOFTWARE\\Microsoft\\...' (también acepta HKLM\\...)."""
    return _data(host, r"""
$path=$P.path -replace '^(HKLM|HKCU|HKCR|HKU)\\', '$1:\' -replace '^HKEY_LOCAL_MACHINE\\','HKLM:\' -replace '^HKEY_CURRENT_USER\\','HKCU:\'
if ($path -match '^HKU:' -and -not (Get-PSDrive HKU -ErrorAction SilentlyContinue)) { New-PSDrive HKU Registry HKEY_USERS | Out-Null }
function Read-Key($p, $d) { $k=Get-Item -LiteralPath $p
  $o=[ordered]@{key=$k.Name; values=[ordered]@{}; subkeys=@($k.GetSubKeyNames())}
  foreach ($v in $k.GetValueNames()) { $o.values[$(if($v){$v}else{'(default)'})]=[ordered]@{type=[string]$k.GetValueKind($v); data=$k.GetValue($v,$null,'DoNotExpandEnvironmentNames')} }
  if ($d -gt 0) { $o.children=@($o.subkeys | Select-Object -First 50 | ForEach-Object { Read-Key (Join-Path $p $_) ($d-1) }) }
  $o }
Out-Json (Read-Key $path $P.d)
""", {"path": path, "d": min(recurse_depth, 3)})


@tool("write")
def set_registry(path: str, name: str, value: Any,
                 type: Literal["String", "ExpandString", "DWord", "QWord", "MultiString", "Binary"] = "String",
                 host: Optional[str] = None) -> dict[str, Any]:
    """Crea/modifica un valor de registro (crea la clave si falta). MultiString = lista; Binary = lista de bytes. Guarda el valor previo."""
    return _data(host, r"""
$path=$P.path -replace '^(HKLM|HKCU|HKCR)\\', '$1:\'
if (-not (Test-Path -LiteralPath $path)) { New-Item -Path $path -Force | Out-Null }
$prev=(Get-ItemProperty -LiteralPath $path -Name $P.name -ErrorAction SilentlyContinue).($P.name)
$v=switch ($P.type) { 'DWord' { [int]$P.value } 'QWord' { [long]$P.value } 'MultiString' { [string[]]$P.value } 'Binary' { [byte[]]$P.value } default { [string]$P.value } }
New-ItemProperty -LiteralPath $path -Name $P.name -Value $v -PropertyType $P.type -Force | Out-Null
Out-Json ([ordered]@{key=$path; name=$P.name; previous=$prev; new=(Get-ItemProperty -LiteralPath $path -Name $P.name).($P.name)})
""", {"path": path, "name": name, "value": value, "type": type}, write=True)


@tool("destructive")
def delete_registry(path: str, name: Optional[str] = None, confirm: bool = False,
                    host: Optional[str] = None) -> dict[str, Any]:
    """Borra un valor (name) o la clave entera con subclaves (sin name). Requiere confirm=true; exporta antes un .reg de respaldo."""
    if (c := _need_confirm(confirm, f"borrar registro {path}{'\\' + name if name else ' (clave completa)'}")):
        return c
    return _data(host, r"""
$path=$P.path -replace '^(HKLM|HKCU|HKCR)\\', '$1:\'
$native=(Get-Item -LiteralPath $path).Name
$bak=Join-Path $env:ProgramData "windows-ssh-admin-mcp\regbak-$(Get-Date -Format yyyyMMdd-HHmmss).reg"
New-Item -ItemType Directory -Force (Split-Path $bak) | Out-Null
& reg.exe export $native $bak /y | Out-Null
if ($P.name) { Remove-ItemProperty -LiteralPath $path -Name $P.name } else { Remove-Item -LiteralPath $path -Recurse -Force }
Out-Json ([ordered]@{deleted= if($P.name){"$native\$($P.name)"}else{$native}; backup=$bak})
""", {"path": path, "name": name}, write=True)


@tool("read")
def get_environment(scope: Literal["Machine", "User", "Process"] = "Machine",
                    host: Optional[str] = None) -> dict[str, Any]:
    """Variables de entorno del ámbito indicado (Machine = las del sistema; PATH se devuelve partido en lista)."""
    return _data(host, r"""
$e=[Environment]::GetEnvironmentVariables($P.scope); $o=[ordered]@{}
foreach ($k in ($e.Keys | Sort-Object)) { $o[$k]= if($k -eq 'Path'){ @($e[$k] -split ';' | Where-Object { $_ }) } else { $e[$k] } }
Out-Json $o
""", {"scope": scope})


@tool("write")
def set_environment(name: str, value: Optional[str] = None, scope: Literal["Machine", "User"] = "Machine",
                    path_append: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """
    Fija una variable de entorno persistente (value=null la elimina). path_append=true añade 'value' al PATH
    si no estaba. Los procesos/servicios ya iniciados no ven el cambio hasta reiniciarlos.
    """
    return _data(host, r"""
$prev=[Environment]::GetEnvironmentVariable($P.name, $P.scope)
if ($P.pa) { $parts=@($prev -split ';' | Where-Object { $_ }); if ($parts -notcontains $P.value) { $parts+=$P.value }; $new=$parts -join ';' } else { $new=$P.value }
[Environment]::SetEnvironmentVariable($P.name, $new, $P.scope)
Out-Json ([ordered]@{name=$P.name; scope=$P.scope; previous=$prev; new=[Environment]::GetEnvironmentVariable($P.name, $P.scope)})
""", {"name": name, "value": value, "scope": scope, "pa": path_append}, write=True)


@tool("read")
def get_certificates(store: str = "LocalMachine\\My", expiring_days: Optional[int] = None,
                     host: Optional[str] = None) -> dict[str, Any]:
    """Certificados de un almacén (LocalMachine\\My, LocalMachine\\Root, WebHosting...) con caducidad y días restantes."""
    return _data(host, r"""
$c=Get-ChildItem "Cert:\$($P.store)"
if ($P.days) { $lim=(Get-Date).AddDays($P.days); $c=$c | Where-Object NotAfter -lt $lim }
Out-Json @($c | Sort-Object NotAfter | ForEach-Object { [ordered]@{subject=$_.Subject; issuer=$_.Issuer; thumbprint=$_.Thumbprint;
  not_after=(Fmt-Date $_.NotAfter); days_left=[int]($_.NotAfter-(Get-Date)).TotalDays; has_private_key=$_.HasPrivateKey;
  san=@($_.DnsNameList | ForEach-Object Unicode); friendly=$_.FriendlyName} })
""", {"store": store, "days": expiring_days})


@tool("read")
def get_shares(host: Optional[str] = None) -> dict[str, Any]:
    """Recursos compartidos SMB con ruta y permisos de share; sesiones SMB abiertas."""
    return _data(host, r"""
Out-Json ([ordered]@{shares=@(Get-SmbShare | ForEach-Object { $s=$_; [ordered]@{name=$s.Name; path=$s.Path; description=$s.Description;
  access=@(Get-SmbShareAccess -Name $s.Name | ForEach-Object { "$($_.AccountName): $($_.AccessControlType) $($_.AccessRight)" })} });
 sessions=@(Get-SmbSession | ForEach-Object { [ordered]@{client=$_.ClientComputerName; user=$_.ClientUserName; open_files=$_.NumOpens} })})
""")


@tool("write")
def manage_share(name: str, action: Literal["create", "remove", "grant", "revoke"], path: Optional[str] = None,
                 account: Optional[str] = None, access: Literal["Full", "Change", "Read"] = "Read",
                 confirm: bool = False, host: Optional[str] = None) -> dict[str, Any]:
    """Crea/elimina (confirm=true) un share SMB, o concede/revoca acceso de share a una cuenta. Los permisos NTFS van aparte (set_permissions)."""
    if action == "remove" and (c := _need_confirm(confirm, f"eliminar share {name}")):
        return c
    return _data(host, r"""
switch ($P.action) {
 'create' { if (-not (Test-Path $P.path)) { New-Item -ItemType Directory -Force $P.path | Out-Null }
   $a=@{Name=$P.name; Path=$P.path}; if ($P.acct) { $a["$($P.acc)Access"]=$P.acct }; New-SmbShare @a | Out-Null }
 'remove' { Remove-SmbShare -Name $P.name -Force }
 'grant' { Grant-SmbShareAccess -Name $P.name -AccountName $P.acct -AccessRight $P.acc -Force | Out-Null }
 'revoke' { Revoke-SmbShareAccess -Name $P.name -AccountName $P.acct -Force | Out-Null }
}
$s=Get-SmbShare -Name $P.name -ErrorAction SilentlyContinue
Out-Json ([ordered]@{name=$P.name; exists=[bool]$s; path=$s.Path;
  access=@(if($s){Get-SmbShareAccess -Name $P.name | ForEach-Object { "$($_.AccountName): $($_.AccessControlType) $($_.AccessRight)" }})})
""", {"name": name, "action": action, "path": path, "acct": account, "acc": access}, write=True)


# ============================================================
# Auditoría
# ============================================================


@tool("read")
def query_audit_log(limit: int = 50, tool_name: Optional[str] = None, only_writes: bool = False) -> dict[str, Any]:
    """Últimas llamadas a este MCP (local, JSONL): tool, argumentos recortados, resultado, duración."""
    if not AUDIT_LOG.exists():
        return {"entries": []}
    rows = [json.loads(line) for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    if tool_name:
        rows = [r for r in rows if r["tool"] == tool_name]
    if only_writes:
        rows = [r for r in rows if r["kind"] != "read"]
    return {"log": str(AUDIT_LOG), "entries": rows[-limit:]}


# ============================================================
# CLI
# ============================================================


async def _handshake() -> int:
    from mcp.client import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
        for t in tools.tools:
            print(f"- {t.name}")
        print(f"{len(tools.tools)} tools")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows Server vía SSH - MCP server")
    parser.add_argument("--test", nargs="?", const="", metavar="HOST", help="Prueba la conexión y sale")
    parser.add_argument("--handshake", action="store_true", help="Lista tools vía cliente MCP")
    args = parser.parse_args()

    if args.test is not None:
        print(json.dumps(test_connection(host=args.test or None), indent=2, ensure_ascii=False))
        return 0
    if args.handshake:
        return asyncio.run(_handshake())

    logger.info(f"windows-ssh-admin MCP v{__version__} — hosts: {', '.join(SETTINGS.hosts)}")
    try:
        mcp.run()
    finally:
        for s in SESSIONS.values():
            s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
