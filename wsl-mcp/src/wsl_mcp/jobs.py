"""Jobs en segundo plano: salida en %%LOCALAPPDATA%%\\wsl-mcp\\jobs\\.

Los jobs sobreviven entre llamadas MCP pero no al reinicio del proceso del servidor MCP.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from wsl_mcp.config import MAX_COMMAND_TIMEOUT, get_settings
from wsl_mcp.util import CREATE_NO_WINDOW, clamp_timeout, err, linux_path, ok, shell_path, truncate

def jobs_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "wsl-mcp" / "jobs"


@dataclass
class JobRecord:
    job_id: str
    command: str
    cwd: Optional[str]
    distro: str
    status: str  # running | completed | failed | stopped
    pid: Optional[int]
    started_at: float
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    timeout_s: int = 120
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _job_dir(job_id: str) -> Path:
    return jobs_dir() / job_id


def _meta_path(job_id: str) -> Path:
    return _job_dir(job_id) / "meta.json"


def _save_meta(record: JobRecord) -> None:
    path = _meta_path(record.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_meta(job_id: str) -> Optional[JobRecord]:
    path = _meta_path(job_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return JobRecord(**data)


def _refresh_running(record: JobRecord) -> JobRecord:
    if record.status != "running" or not record.pid:
        return record
    if os.name != "nt":
        return record
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {record.pid}", "/NH"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (proc.stdout or b"").decode("cp1252", errors="replace")
        if str(record.pid) not in out:
            exit_p = _job_dir(record.job_id) / "exit.code"
            if exit_p.is_file():
                code_text = exit_p.read_text(encoding="utf-8").strip()
                if code_text.isdigit() or (code_text.startswith("-") and code_text[1:].isdigit()):
                    record.exit_code = int(code_text)
            if record.exit_code is None:
                record.exit_code = -1
            record.status = "completed" if record.exit_code == 0 else "failed"
            if record.finished_at is None:
                record.finished_at = time.time()
    except Exception:
        pass
    stdout_p = _job_dir(record.job_id) / "stdout.log"
    stderr_p = _job_dir(record.job_id) / "stderr.log"
    record.stdout_bytes = stdout_p.stat().st_size if stdout_p.is_file() else 0
    record.stderr_bytes = stderr_p.stat().st_size if stderr_p.is_file() else 0
    return record


def build_wsl_argv(distro: str, shell_cmd: str, timeout: int, shell: str) -> list[str]:
    settings = get_settings()
    args = ["wsl.exe", "-d", distro]
    if settings.user:
        args.extend(["-u", settings.user])
    args.extend(
        ["-e", "timeout", "-k", "5", str(timeout), shell, "-lc", shell_cmd],
    )
    return args


def start_job(
    command: str,
    cwd: Optional[str] = None,
    distro: Optional[str] = None,
    timeout: int = 0,
) -> dict[str, Any]:
    from wsl_mcp.ops import build_shell_command  # evita import circular

    if not command.strip():
        return err("Comando vacío")
    settings = get_settings()
    use_distro = settings.resolve_distro(distro)
    timeout_s = clamp_timeout(timeout or None, settings.command_timeout, MAX_COMMAND_TIMEOUT)
    job_id = uuid.uuid4().hex[:12]
    job_path = _job_dir(job_id)
    job_path.mkdir(parents=True, exist_ok=True)
    stdout_f = open(job_path / "stdout.log", "wb")  # noqa: SIM115
    stderr_f = open(job_path / "stderr.log", "wb")  # noqa: SIM115
    exit_file = job_path / "exit.code"
    inner = command.replace("\r\n", "\n")
    exit_linux = linux_path(str(exit_file))
    wrapped = f"{inner}\necho $? > {shell_path(exit_linux)}"
    shell_cmd = build_shell_command(wrapped, cwd)
    argv = build_wsl_argv(use_distro, shell_cmd, timeout_s, settings.shell)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        stdout_f.close()
        stderr_f.close()
        return err("Comando 'wsl' no encontrado. ¿Está WSL instalado?")
    except Exception as exc:
        stdout_f.close()
        stderr_f.close()
        return err(str(exc))
    stdout_f.close()
    stderr_f.close()
    shown = command if len(command) <= 500 else command[:500] + "... [recortado]"
    record = JobRecord(
        job_id=job_id,
        command=shown,
        cwd=cwd,
        distro=use_distro,
        status="running",
        pid=proc.pid,
        started_at=time.time(),
        timeout_s=timeout_s,
    )
    _save_meta(record)
    return ok({
        "job_id": job_id,
        "status": "running",
        "pid": proc.pid,
        "distro": use_distro,
        "jobs_dir": str(jobs_dir()),
        "note": "El job sobrevive entre llamadas MCP pero no si se reinicia el proceso del servidor.",
    })


def _read_log_tail(path: Path, tail_lines: int, offset: int, max_chars: int) -> tuple[str, int, int, bool]:
    if not path.is_file():
        return "", offset, 0, False
    size = path.stat().st_size
    if tail_lines > 0:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        chunk = "\n".join(lines[-tail_lines:])
        truncated, _ = truncate(chunk, max_chars)
        return truncated, size, len(lines), len(lines) > tail_lines
    if offset >= size:
        return "", size, size, False
    with open(path, "rb") as fh:
        fh.seek(offset)
        raw = fh.read(max_chars * 4)
    text = raw.decode("utf-8", errors="replace")
    truncated, was = truncate(text, max_chars)
    new_offset = offset + len(raw)
    if new_offset > size:
        new_offset = size
    return truncated, new_offset, size, was or new_offset < size


def get_job(
    job_id: str,
    tail_lines: int = 200,
    stdout_offset: int = 0,
    stderr_offset: int = 0,
) -> dict[str, Any]:
    record = _load_meta(job_id)
    if not record:
        return err("Job no encontrado", job_id=job_id)
    record = _refresh_running(record)
    _save_meta(record)
    settings = get_settings()
    max_chars = settings.max_output
    tail_lines = max(0, int(tail_lines))
    stdout_off = max(0, int(stdout_offset))
    stderr_off = max(0, int(stderr_offset))
    stdout_path = _job_dir(job_id) / "stdout.log"
    stderr_path = _job_dir(job_id) / "stderr.log"
    if tail_lines > 0:
        stdout_text, stdout_next, stdout_size, stdout_more = _read_log_tail(
            stdout_path, tail_lines, 0, max_chars
        )
        stderr_text, stderr_next, stderr_size, stderr_more = _read_log_tail(
            stderr_path, tail_lines, 0, max_chars
        )
        stdout_next = stdout_size
        stderr_next = stderr_size
    else:
        stdout_text, stdout_next, stdout_size, stdout_more = _read_log_tail(
            stdout_path, 0, stdout_off, max_chars
        )
        stderr_text, stderr_next, stderr_size, stderr_more = _read_log_tail(
            stderr_path, 0, stderr_off, max_chars
        )
    duration = None
    if record.finished_at:
        duration = round(record.finished_at - record.started_at, 3)
    elif record.status == "running":
        duration = round(time.time() - record.started_at, 3)
    return ok({
        "job_id": job_id,
        "status": record.status,
        "exit_code": record.exit_code,
        "duration_s": duration,
        "distro": record.distro,
        "command": record.command,
        "cwd": record.cwd,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_offset": stdout_next,
        "stderr_offset": stderr_next,
        "stdout_size_bytes": stdout_size,
        "stderr_size_bytes": stderr_size,
        "stdout_has_more": stdout_more,
        "stderr_has_more": stderr_more,
        "pid": record.pid,
    })


def list_jobs() -> dict[str, Any]:
    jobs_dir().mkdir(parents=True, exist_ok=True)
    jobs: list[dict[str, Any]] = []
    for entry in sorted(jobs_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        record = _load_meta(entry.name)
        if not record:
            continue
        record = _refresh_running(record)
        _save_meta(record)
        jobs.append({
            "job_id": record.job_id,
            "status": record.status,
            "exit_code": record.exit_code,
            "distro": record.distro,
            "command": record.command,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
        })
    return ok({"count": len(jobs), "jobs_dir": str(jobs_dir()), "jobs": jobs})


def stop_job(job_id: str) -> dict[str, Any]:
    record = _load_meta(job_id)
    if not record:
        return err("Job no encontrado", job_id=job_id)
    if record.status != "running":
        return ok({"job_id": job_id, "status": record.status, "message": "El job ya no está en ejecución"})
    pid = record.pid
    if not pid:
        return err("Sin PID registrado", job_id=job_id)
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            os.kill(pid, 9)
    except Exception as exc:
        return err(f"No se pudo detener el job: {exc}", job_id=job_id)
    record.status = "stopped"
    record.finished_at = time.time()
    record.exit_code = -1
    _save_meta(record)
    return ok({"job_id": job_id, "status": "stopped", "pid": pid})
