"""Sesión PowerShell persistente sobre OpenSSH (honra ~/.ssh/config: ProxyJump, IdentityFile...).

Protocolo: se lanza `ssh <alias> powershell -EncodedCommand <LOOP>`; cada petición es una línea
"<id> <base64-utf8-script>" por stdin; el servidor responde una línea "@@MCP@@<base64-json>" con
{id, code, out, err, json}. Si algo se cuelga se mata el proceso y la siguiente llamada reconecta.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("ssh-winserver-mcp")

SSH_EXE = os.environ.get("SSHWIN_SSH_EXE") or shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
SCP_EXE = os.environ.get("SSHWIN_SCP_EXE") or shutil.which("scp") or r"C:\Windows\System32\OpenSSH\scp.exe"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4"]
MARK = "@@MCP@@"
READY = "@@READY@@"

# Bucle remoto (PowerShell 5.1). Compacto: va en la línea de comandos vía -EncodedCommand.
# Se lee stdin con $input: [Console]::In / OpenStandardInput no reciben nada bajo el sshd de Windows.
_LOOP = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ProgressPreference='SilentlyContinue'
[Console]::Out.WriteLine('@@READY@@');[Console]::Out.Flush()
foreach($line in $input){
 $p=$line.Split(' ',2); $st=@{c=0;e=(New-Object Text.StringBuilder)}; $o=''
 $global:__json=$null; $global:LASTEXITCODE=0
 try{
  $sb=[scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p[1])))
  $o= & $sb *>&1 | ForEach-Object {
   if($_ -is [Management.Automation.ErrorRecord]){$st.c=1;[void]$st.e.AppendLine(($_|Out-String -Width 300).Trim())}
   elseif($_ -is [Management.Automation.WarningRecord]){[void]$st.e.AppendLine('WARNING: '+$_.Message)}
   else{$_}} | Out-String -Width 300
  if($st.c -eq 0 -and $global:LASTEXITCODE){$st.c=$global:LASTEXITCODE}
 }catch{$st.c=1;[void]$st.e.AppendLine(($_|Out-String -Width 300).Trim())}
 $r=@{id=$p[0];code=$st.c;out=[string]$o;err=$st.e.ToString();json=$global:__json}|ConvertTo-Json -Compress
 [Console]::Out.WriteLine('@@MCP@@'+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($r)))
 [Console]::Out.Flush()
}
"""

# Se antepone a cada script: helpers comunes para las tools estructuradas.
# Ojo: las líneas de Get-Content llevan NoteProperties (PSDrive, PSProvider...) que ConvertTo-Json recorre
# a fondo y lo cuelgan; castear a [string] o usar [IO.File]::ReadAllLines antes de Out-Json.
PRELUDE = r"""
function Out-Json($o){ $global:__json = ConvertTo-Json -InputObject $o -Depth 8 -Compress }
function Fmt-Date($d){ if($d){ ([datetime]$d).ToString('yyyy-MM-dd HH:mm:ss') } else { $null } }
"""


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


@dataclass
class Result:
    ok: bool
    code: int
    stdout: str
    stderr: str
    json: Any
    duration_s: float

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "exit_code": self.code, "duration_s": round(self.duration_s, 2)}
        if self.stdout.strip():
            d["stdout"] = self.stdout.rstrip()
        if self.stderr.strip():
            d["stderr"] = self.stderr.rstrip()
        return d


class RemoteError(RuntimeError):
    pass


class PsSession:
    """Una sesión powershell.exe remota viva, serializada con un lock."""

    def __init__(self, ssh_alias: str):
        self.alias = ssh_alias
        self.proc: Optional[subprocess.Popen] = None
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self.stderr_tail: list[str] = []
        self.lock = threading.Lock()
        self.started_at: Optional[float] = None

    # ---------------------------------------------------------------- ciclo de vida
    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self, timeout: float = 45) -> None:
        self.close()
        cmd = [SSH_EXE, *SSH_OPTS, "-T", self.alias,
               f"powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {_encoded(_LOOP)}"]
        logger.info("Abriendo sesión SSH persistente a %s", self.alias)
        self.lines = queue.Queue()
        self.stderr_tail = []
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump_out, args=(self.proc, self.lines), daemon=True).start()
        threading.Thread(target=self._pump_err, args=(self.proc,), daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if not self._alive():
                    break
                continue
            if line is None:
                break
            if line.strip() == READY:
                self.started_at = time.time()
                return
        err = self._stderr_text()
        self.close()
        raise RemoteError(f"No se pudo abrir la sesión SSH a '{self.alias}': {err or 'timeout esperando al shell remoto'}")

    @staticmethod
    def _pump_out(proc: subprocess.Popen, q: "queue.Queue[Optional[str]]") -> None:
        assert proc.stdout
        for raw in proc.stdout:
            q.put(raw.decode("utf-8", errors="replace"))
        q.put(None)

    def _pump_err(self, proc: subprocess.Popen) -> None:
        assert proc.stderr
        for raw in proc.stderr:
            self.stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())
            del self.stderr_tail[:-40]

    def _stderr_text(self) -> str:
        return "\n".join(line for line in self.stderr_tail if line and "CLIXML" not in line and "<Objs" not in line)

    def close(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None
        self.started_at = None

    # ---------------------------------------------------------------- ejecución
    def run(self, script: str, timeout: float = 120) -> Result:
        with self.lock:
            if not self._alive():
                self._start()
            rid = uuid.uuid4().hex[:12]
            payload = base64.b64encode((PRELUDE + script).encode("utf-8")).decode("ascii")
            t0 = time.monotonic()
            try:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(f"{rid} {payload}\n".encode("ascii"))
                self.proc.stdin.flush()
            except OSError as exc:
                self.close()
                raise RemoteError(f"La sesión SSH se cerró al enviar el comando: {exc}") from exc
            deadline = t0 + timeout
            noise: list[str] = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise TimeoutError(f"Timeout de {timeout:.0f}s; la sesión remota se reinició (el proceso remoto "
                                       "pudo quedar corriendo). Para tareas largas usa run_background.")
                try:
                    line = self.lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    err = self._stderr_text()
                    self.close()
                    raise RemoteError(f"La sesión SSH terminó inesperadamente. {err}".strip())
                if not line.startswith(MARK):
                    noise.append(line.rstrip())
                    continue
                data = json.loads(base64.b64decode(line[len(MARK):].strip()).decode("utf-8"))
                if data.get("id") != rid:
                    continue
                parsed = None
                if data.get("json"):
                    try:
                        parsed = json.loads(data["json"])
                    except ValueError:
                        parsed = data["json"]
                out = data.get("out") or ""
                if noise:
                    out = "\n".join(noise) + "\n" + out
                code = int(data.get("code") or 0)
                return Result(code == 0, code, out, data.get("err") or "", parsed, time.monotonic() - t0)


def run_oneshot(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def scp(src: str, dst: str, recursive: bool = False, timeout: float = 600) -> subprocess.CompletedProcess:
    args = [SCP_EXE, *SSH_OPTS, "-q"]
    if recursive:
        args.append("-r")
    return run_oneshot([*args, src, dst], timeout)
