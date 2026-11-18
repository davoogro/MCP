"""Pool de workers PowerShell persistentes (worker_host.ps1)."""

from __future__ import annotations

import atexit
import json
import logging
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from windows_winrm_fleet_mcp.config import ServerRecord, get_server, get_settings

logger = logging.getLogger("windows-winrm-fleet-mcp.worker")

MODULE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = MODULE_DIR / "worker_host.ps1"


class WorkerError(Exception):
    def __init__(self, message: str, code: str = "RemoteError") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PendingRequest:
    req_id: str
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class PowerShellWorkerProcess:
    def __init__(self, worker_index: int) -> None:
        self.index = worker_index
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        self._pending: dict[str, PendingRequest] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._alive = False

    def start(self) -> None:
        settings = get_settings()
        if not WORKER_SCRIPT.is_file():
            raise WorkerError(f"worker_host.ps1 no encontrado: {WORKER_SCRIPT}", "InvalidArgument")
        cmd = [
            settings.powershell_exe,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WORKER_SCRIPT),
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._alive = True
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("worker %s: línea JSON inválida", self.index)
                continue
            req_id = msg.get("id")
            if req_id and req_id in self._pending:
                pending = self._pending.pop(req_id)
                pending.response = msg
                pending.event.set()
            else:
                logger.debug("worker %s: respuesta sin pending id=%s", self.index, req_id)

    def _send(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            if not self._alive or not self._proc or not self._proc.stdin:
                raise WorkerError("worker no iniciado", "RemoteError")
            req_id = payload.get("id") or str(uuid.uuid4())
            payload["id"] = req_id
            pending = PendingRequest(req_id=req_id)
            self._pending[req_id] = pending
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                self._pending.pop(req_id, None)
                raise WorkerError(str(exc), "RemoteError") from exc
        if not pending.event.wait(timeout=timeout):
            self._pending.pop(req_id, None)
            self.kill()
            raise WorkerError(f"timeout tras {timeout}s", "Timeout")
        if pending.response is None:
            raise WorkerError("respuesta vacía del worker", "RemoteError")
        return pending.response

    def kill(self) -> None:
        self._alive = False
        proc = self._proc
        if proc and proc.pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except OSError:
                pass
        self._proc = None

    def close(self) -> None:
        if self._alive and self._proc and self._proc.stdin:
            try:
                self._send({"op": "close_sessions", "hosts": []}, timeout=30)
            except WorkerError:
                pass
        self.kill()


def _host_meta(server: ServerRecord) -> dict[str, Any]:
    return {
        "hostname": server.hostname,
        "port": server.port,
        "use_ssl": server.use_ssl,
        "open_timeout_ms": server.open_timeout_seconds * 1000,
        "operation_timeout_ms": server.operation_timeout_seconds * 1000,
    }


class PowerShellWorkerPool:
    """Pool thread-safe de workers; una petición toma un worker libre."""

    def __init__(self, size: Optional[int] = None) -> None:
        settings = get_settings()
        self._size = size or settings.worker_count
        self._workers: list[PowerShellWorkerProcess] = []
        self._available: queue.Queue[PowerShellWorkerProcess] = queue.Queue()
        self._init_lock = threading.Lock()
        self._started = False
        self._session_hosts: set[str] = set()
        self._session_lock = threading.Lock()
        self._primary: Optional[PowerShellWorkerProcess] = None
        self._primary_lock = threading.Lock()

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._init_lock:
            if self._started:
                return
            for i in range(self._size):
                w = PowerShellWorkerProcess(i)
                w.start()
                self._workers.append(w)
                if i == 0:
                    self._primary = w
                else:
                    self._available.put(w)
            self._started = True

    def _borrow(self, timeout: float = 600) -> PowerShellWorkerProcess:
        self._ensure_started()
        try:
            return self._available.get(timeout=timeout)
        except queue.Empty as exc:
            raise WorkerError("cola de workers saturada", "Timeout") from exc

    def _return(self, worker: PowerShellWorkerProcess) -> None:
        if worker.index == 0:
            if not worker._alive:
                new_w = PowerShellWorkerProcess(0)
                new_w.start()
                self._workers[0] = new_w
                self._primary = new_w
            return
        if worker._alive:
            self._available.put(worker)
        else:
            new_w = PowerShellWorkerProcess(worker.index)
            new_w.start()
            self._workers[worker.index] = new_w
            self._available.put(new_w)

    def _borrow_for_invoke(self, wait_seconds: float = 2.0) -> tuple[PowerShellWorkerProcess, bool]:
        """Worker principal (sesiones calientes); reserva secundarios si el principal está ocupado >2s."""
        self._ensure_started()
        assert self._primary is not None
        deadline = time.monotonic() + wait_seconds
        while True:
            if self._primary_lock.acquire(blocking=False):
                return self._primary, True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return self._borrow(timeout=600), False

    def _return_invoke(self, worker: PowerShellWorkerProcess, from_primary: bool) -> None:
        if from_primary:
            self._primary_lock.release()
        else:
            self._return(worker)

    def invoke(
        self,
        server_ids: list[str],
        probe: str,
        args: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        op_timeout = timeout
        if op_timeout is None:
            op_timeout = max(s.operation_timeout_seconds for sid in server_ids for s in [get_server(sid)])
        host_meta = {sid: _host_meta(get_server(sid)) for sid in server_ids}
        payload = {
            "op": "invoke",
            "hosts": server_ids,
            "probe": probe,
            "args": args or {},
            "timeout": int(op_timeout),
            "host_meta": host_meta,
        }
        worker, from_primary = self._borrow_for_invoke()
        try:
            resp = worker._send(payload, timeout=op_timeout + 15)
        finally:
            self._return_invoke(worker, from_primary)
        with self._session_lock:
            for sid in server_ids:
                self._session_hosts.add(sid)
        return resp

    def invoke_script(
        self,
        server_ids: list[str],
        script: str,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        op_timeout = timeout or 180
        host_meta = {sid: _host_meta(get_server(sid)) for sid in server_ids}
        payload = {
            "op": "invoke_script",
            "hosts": server_ids,
            "script": script,
            "timeout": int(op_timeout),
            "host_meta": host_meta,
        }
        worker = self._borrow(timeout=op_timeout + 30)
        try:
            resp = worker._send(payload, timeout=op_timeout + 15)
        finally:
            self._return(worker)
        return resp

    def invoke_local(self, probe: str, args: Optional[dict] = None, timeout: float = 30) -> dict[str, Any]:
        payload = {"op": "invoke_local", "probe": probe, "args": args or {}}
        worker = self._borrow(timeout=timeout + 10)
        try:
            return worker._send(payload, timeout=timeout)
        finally:
            self._return(worker)

    def ping(self, timeout: float = 15) -> dict[str, Any]:
        worker = self._borrow(timeout=timeout)
        try:
            return worker._send({"op": "ping"}, timeout=timeout)
        finally:
            self._return(worker)

    def copy(
        self,
        op: str,
        server_id: str,
        *,
        local_path: str,
        remote_path: str,
        recurse: bool = False,
        timeout: float = 300,
    ) -> dict[str, Any]:
        srv = get_server(server_id)
        payload = {
            "op": op,
            "host": server_id,
            "local_path": local_path,
            "remote_path": remote_path,
            "recurse": recurse,
            "host_meta": _host_meta(srv),
        }
        worker = self._borrow(timeout=timeout + 30)
        try:
            return worker._send(payload, timeout=timeout)
        finally:
            self._return(worker)

    def close_sessions(self, hosts: Optional[list[str]] = None) -> None:
        if not self._started:
            return
        payload = {"op": "close_sessions", "hosts": hosts or []}
        for w in list(self._workers):
            try:
                w._send(payload, timeout=30)
            except WorkerError:
                pass
        with self._session_lock:
            if hosts:
                for h in hosts:
                    self._session_hosts.discard(h)
            else:
                self._session_hosts.clear()

    def list_open_sessions(self) -> set[str]:
        with self._session_lock:
            return set(self._session_hosts)

    def shutdown(self) -> None:
        self.close_sessions([])
        for w in self._workers:
            w.close()
        self._started = False


class FakeWorker:
    """Worker simulado para tests sin red."""

    def __init__(self, handler: Callable[[dict], dict]) -> None:
        self.handler = handler
        self._sessions: set[str] = set()

    def invoke(
        self,
        server_ids: list[str],
        probe: str,
        args: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        results = {}
        for sid in server_ids:
            inner = self.handler({"probe": probe, "args": args or {}, "server_id": sid})
            if inner.get("ok") is False:
                results[sid] = inner
            else:
                results[sid] = {"ok": True, "data": inner.get("data", inner)}
            self._sessions.add(sid)
        return {"ok": True, "results": results}

    def ping(self, timeout: float = 15) -> dict[str, Any]:
        return {"ok": True, "pong": True}

    def close_sessions(self, hosts: Optional[list[str]] = None) -> None:
        if hosts:
            for h in hosts:
                self._sessions.discard(h)
        else:
            self._sessions.clear()

    def list_open_sessions(self) -> set[str]:
        return set(self._sessions)


_pool: Optional[PowerShellWorkerPool] = None
_fake: Optional[FakeWorker] = None


def get_worker_pool() -> PowerShellWorkerPool:
    global _pool
    if _pool is None:
        _pool = PowerShellWorkerPool()
        atexit.register(_pool.shutdown)
    return _pool


def set_fake_worker(fake: Optional[FakeWorker]) -> None:
    global _fake
    _fake = fake


def get_worker() -> PowerShellWorkerPool | FakeWorker:
    if _fake is not None:
        return _fake
    return get_worker_pool()
