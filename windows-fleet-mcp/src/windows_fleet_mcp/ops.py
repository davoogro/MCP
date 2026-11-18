"""Operaciones invocadas por las tools MCP."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from windows_fleet_mcp.audit import get_audit
from windows_fleet_mcp.config import (
    ServersSelector,
    get_server,
    get_settings,
    require_permission,
    resolve_server_ids,
)
from windows_fleet_mcp.confirmations import get_restart_confirmations
from windows_fleet_mcp.util import (
    add_human_sizes,
    human_size,
    is_protected_pid,
    is_protected_process,
    path_matches_denylist,
    validate_network_target,
)
from windows_fleet_mcp.worker import WorkerError, get_worker

_PS_REMOTING_KEYS = frozenset(
    {"PSComputerName", "RunspaceId", "PSShowComputerName", "PTO_CLE_Other", "SerializedData"}
)


class ToolError(Exception):
    def __init__(self, message: str, code: str = "RemoteError", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _normalize_results(raw: dict[str, Any], server_ids: list[str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    worker_results = raw.get("results") or {}
    for sid in server_ids:
        entry = worker_results.get(sid)
        if not entry:
            results[sid] = {"ok": False, "error": "sin respuesta del host", "error_code": "Unreachable"}
            continue
        if entry.get("ok") is False or entry.get("error_code"):
            results[sid] = {
                "ok": False,
                "error": entry.get("error", "error remoto"),
                "error_code": entry.get("error_code", "RemoteError"),
            }
            continue
        data = entry.get("data", entry)
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in _PS_REMOTING_KEYS}
            results[sid] = {"ok": True, **data}
        else:
            results[sid] = {"ok": True, "data": data}
    ok_count = sum(1 for r in results.values() if r.get("ok"))
    return {
        "results": results,
        "summary": {"total": len(server_ids), "ok": ok_count, "failed": len(server_ids) - ok_count},
    }


def _run_probe(
    server_ids: list[str],
    probe: str,
    args: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        raw = get_worker().invoke(server_ids, probe, args, timeout)
    except WorkerError as exc:
        raise ToolError(str(exc), exc.code) from exc
    duration_ms = int((time.perf_counter() - started) * 1000)
    body = _normalize_results(raw, server_ids)
    body["duration_ms"] = duration_ms
    return body


def _resolve(selector: ServersSelector) -> list[str]:
    try:
        return resolve_server_ids(selector)
    except ValueError as exc:
        settings = get_settings()
        raise ToolError(
            str(exc),
            "InvalidArgument",
            valid_server_ids=sorted(settings.servers.keys()),
        ) from exc
    except KeyError as exc:
        raise ToolError(f"servidor no encontrado: {exc}", "NotFound") from exc


def _audit_wrap(
    tool: str,
    servers: list[str],
    parameters: dict,
    fn: Callable[[], dict],
) -> dict[str, Any]:
    started = time.perf_counter()
    ok = True
    error_code = None
    try:
        result = fn()
        if isinstance(result, dict) and result.get("ok") is False:
            ok = False
            error_code = result.get("error_code")
        return result
    except ToolError as exc:
        ok = False
        error_code = exc.code
        body: dict[str, Any] = {"ok": False, "error": str(exc), "error_code": exc.code}
        body.update(exc.extra)
        return body
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        get_audit().log_tool(
            tool=tool,
            servers=servers,
            parameters=parameters,
            ok=ok,
            error_code=error_code,
            duration_ms=duration_ms,
        )


def list_servers() -> dict[str, Any]:
    settings = get_settings()
    open_sess = get_worker().list_open_sessions()
    items = [
        get_server(sid).public_dict(session_open=(sid in open_sess)) for sid in sorted(settings.servers)
    ]
    return {"servers": items, "count": len(items)}


def test_connection(servers: ServersSelector = "all") -> dict[str, Any]:
    server_ids = _resolve(servers)
    started = time.perf_counter()
    results: dict[str, Any] = {}
    for sid in server_ids:
        srv = get_server(sid)
        host = srv.hostname
        port = srv.port
        t0 = time.perf_counter()
        dns_ok = True
        resolved = host
        try:
            resolved = socket.gethostbyname(host)
        except socket.gaierror as exc:
            dns_ok = False
            results[sid] = {
                "ok": False,
                "error": f"DNS: {exc}",
                "error_code": "Unreachable",
                "dns_ok": False,
            }
            continue
        tcp_ok = False
        try:
            with socket.create_connection((host, port), timeout=3):
                tcp_ok = True
        except OSError as exc:
            results[sid] = {
                "ok": False,
                "error": f"TCP {port}: {exc}",
                "error_code": "Unreachable",
                "dns_ok": dns_ok,
                "resolved": resolved,
                "tcp_ok": False,
            }
            continue
        probe = _run_probe([sid], "whoami", timeout=60)
        pr = probe["results"].get(sid, {})
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if not pr.get("ok"):
            results[sid] = {**pr, "dns_ok": dns_ok, "tcp_ok": tcp_ok, "resolved": resolved, "latency_ms": latency_ms}
        else:
            results[sid] = {
                "ok": True,
                "dns_ok": dns_ok,
                "tcp_ok": tcp_ok,
                "resolved": resolved,
                "latency_ms": latency_ms,
                **{k: v for k, v in pr.items() if k != "ok"},
            }
    ok_count = sum(1 for r in results.values() if r.get("ok"))
    return {
        "ok": True,
        "results": results,
        "summary": {"total": len(server_ids), "ok": ok_count, "failed": len(server_ids) - ok_count},
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def fleet_overview(
    servers: ServersSelector = "all",
    disk_free_pct_warn: float = 10,
    mem_used_pct_warn: float = 90,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    body = _run_probe(
        server_ids,
        "fleet_overview",
        {"disk_free_pct_warn": disk_free_pct_warn, "mem_used_pct_warn": mem_used_pct_warn},
    )
    alerts: list[dict] = []
    for sid, res in body["results"].items():
        if not res.get("ok"):
            alerts.append({"server": sid, "severity": "critical", "message": res.get("error", "fallo")})
            continue
        for al in res.get("alerts") or []:
            alerts.append({"server": sid, **al})
    sev_order = {"critical": 0, "major": 1, "warning": 2, "info": 3}
    alerts.sort(key=lambda a: sev_order.get(a.get("severity", "info"), 9))
    body["alerts"] = alerts
    return {"ok": True, **body}


def get_server_health(servers: ServersSelector) -> dict[str, Any]:
    server_ids = _resolve(servers)
    body = _run_probe(server_ids, "server_health")
    for res in body["results"].values():
        if res.get("ok") and "memory_total_bytes" in res:
            res["memory_total_human"] = human_size(int(res["memory_total_bytes"]))
            res["memory_free_human"] = human_size(int(res.get("memory_free_bytes", 0)))
    return {"ok": True, **body}


def get_disks(servers: ServersSelector, include_volumes: bool = False) -> dict[str, Any]:
    server_ids = _resolve(servers)
    body = _run_probe(server_ids, "disks", {"include_volumes": include_volumes})
    return {"ok": True, **add_human_sizes(body)}


def get_services(
    servers: ServersSelector,
    name: Optional[str] = None,
    state: Optional[str] = None,
    start_mode: Optional[str] = None,
    problems_only: bool = False,
    include_trigger_start: bool = False,
    limit: int = 300,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    args = {
        "name": name,
        "state": state,
        "start_mode": start_mode,
        "problems_only": problems_only,
        "include_trigger_start": include_trigger_start,
        "limit": limit,
    }
    return {"ok": True, **_run_probe(server_ids, "services", args)}


def get_processes(
    servers: ServersSelector,
    name: Optional[str] = None,
    sort_by: str = "cpu",
    top: int = 25,
    include_command_line: bool = False,
    include_owner: bool = False,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    args = {
        "name": name,
        "sort_by": sort_by,
        "top": top,
        "include_command_line": include_command_line,
        "include_owner": include_owner,
    }
    body = _run_probe(server_ids, "processes", args)
    return {"ok": True, **add_human_sizes(body)}


def get_events(
    servers: ServersSelector,
    log: str = "System",
    level: Optional[str] = None,
    since_hours: float = 24,
    provider: Optional[str] = None,
    event_id: Optional[int] = None,
    contains: Optional[str] = None,
    max_events: int = 50,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    args = {
        "log": log,
        "level": level,
        "since_hours": since_hours,
        "provider": provider,
        "event_id": event_id,
        "contains": contains,
        "max_events": max_events,
    }
    return {"ok": True, **_run_probe(server_ids, "events", args)}


def list_event_logs(server: str, contains: Optional[str] = None) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("list_event_logs requiere un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    body = _run_probe([sid], "event_logs_list", {"contains": contains})
    return {"ok": True, **body}


def get_scheduled_tasks(
    servers: ServersSelector,
    path: str = "\\",
    name: Optional[str] = None,
    failed_only: bool = False,
    include_microsoft: bool = False,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    args = {
        "path": path,
        "name": name,
        "failed_only": failed_only,
        "include_microsoft": include_microsoft,
    }
    return {"ok": True, **_run_probe(server_ids, "scheduled_tasks", args)}


def get_installed_updates(servers: ServersSelector, last: int = 20) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "hotfixes", {"last": last})}


def get_installed_software(servers: ServersSelector, name: Optional[str] = None) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "software", {"name": name})}


def get_network(server: str) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("get_network requiere un solo servidor", "InvalidArgument")
    return {"ok": True, **_run_probe([server_ids[0]], "network")}


def test_network_from_server(
    servers: ServersSelector,
    target: str,
    port: Optional[int] = None,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    try:
        validate_network_target(target)
    except ValueError as exc:
        raise ToolError(str(exc), "InvalidArgument") from exc
    if port is not None and (port < 1 or port > 65535):
        raise ToolError("puerto inválido", "InvalidArgument")
    return {"ok": True, **_run_probe(server_ids, "test_network", {"target": target, "port": port})}


def get_sessions(servers: ServersSelector) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "sessions")}


def get_certificates(
    servers: ServersSelector,
    store: str = "My",
    expiring_days: Optional[int] = None,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "certificates", {"store": store, "expiring_days": expiring_days})}


def get_local_admins(servers: ServersSelector) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "local_admins")}


def get_shares(servers: ServersSelector) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "shares")}


def get_iis(servers: ServersSelector) -> dict[str, Any]:
    server_ids = _resolve(servers)
    return {"ok": True, **_run_probe(server_ids, "iis")}


def get_performance(servers: ServersSelector, samples: int = 3, interval_seconds: int = 1) -> dict[str, Any]:
    server_ids = _resolve(servers)
    timeout = samples * interval_seconds + 30
    return {
        "ok": True,
        **_run_probe(
            server_ids,
            "performance",
            {"samples": samples, "interval_seconds": interval_seconds},
            timeout=timeout,
        ),
    }


def get_firewall_rules(
    server: str,
    enabled_only: bool = True,
    direction: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("get_firewall_rules requiere un solo servidor", "InvalidArgument")
    args = {"enabled_only": enabled_only, "direction": direction, "name": name, "limit": limit}
    return {"ok": True, **_run_probe([server_ids[0]], "firewall", args)}


def list_remote_dir(
    server: str,
    path: str,
    pattern: str = "*",
    recurse: bool = False,
    max_entries: int = 500,
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("list_remote_dir requiere un solo servidor", "InvalidArgument")
    if not path or not os.path.isabs(path.replace("/", "\\")):
        raise ToolError("path debe ser absoluto en el servidor remoto", "InvalidArgument")
    body = _run_probe(
        [server_ids[0]],
        "list_dir",
        {"path": path, "pattern": pattern, "recurse": recurse, "max_entries": max_entries},
    )
    return {"ok": True, **add_human_sizes(body)}


def read_remote_file(
    server: str,
    path: str,
    offset: int = 0,
    max_bytes: int = 1048576,
    encoding: str = "auto",
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("read_remote_file requiere un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    srv = get_server(sid)
    if path_matches_denylist(path, srv.file_read_denylist):
        raise ToolError(
            "ruta bloqueada por file_read_denylist del inventario",
            "NotAllowed",
        )
    body = _run_probe(
        [sid],
        "read_file",
        {"path": path, "offset": offset, "max_bytes": max_bytes, "encoding": encoding},
    )
    res = body["results"].get(sid, {})
    if not res.get("ok"):
        err = res.get("error", "")
        if "binario" in err.lower():
            raise ToolError(err, "InvalidArgument")
        return {"ok": False, "error": err, "error_code": res.get("error_code", "RemoteError")}
    return {"ok": True, **body}


def tail_remote_file(
    server: str,
    path: str,
    lines: int = 200,
    contains: Optional[str] = None,
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("tail_remote_file requiere un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    if path_matches_denylist(path, get_server(sid).file_read_denylist):
        raise ToolError("ruta bloqueada por denylist", "NotAllowed")
    return {"ok": True, **_run_probe([sid], "tail_file", {"path": path, "lines": lines, "contains": contains})}


def search_remote_files(
    server: str,
    path: str,
    name_pattern: str = "*",
    contains: Optional[str] = None,
    max_results: int = 200,
    max_depth: int = 8,
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("search_remote_files requiere un solo servidor", "InvalidArgument")
    args = {
        "path": path,
        "name_pattern": name_pattern,
        "contains": contains,
        "max_results": max_results,
        "max_depth": max_depth,
    }
    return {"ok": True, **_run_probe([server_ids[0]], "search_files", args)}


def _compare_key(aspect: str, data: dict) -> set[str]:
    if aspect == "services":
        return {s.get("name", "") for s in (data.get("services") or [])}
    if aspect == "hotfixes":
        return {h.get("id", "") for h in (data.get("hotfixes") or [])}
    if aspect == "software":
        return {f"{s.get('name')}|{s.get('version')}" for s in (data.get("software") or [])}
    if aspect == "scheduled_tasks":
        keys = set()
        for t in data.get("tasks") or []:
            tp = t.get("path") or t.get("task_path") or ""
            tn = t.get("name") or t.get("task_name") or ""
            keys.add(f"{tp}{tn}")
        return keys
    return set()


def compare_servers(servers: ServersSelector, aspect: str = "services") -> dict[str, Any]:
    if aspect not in ("services", "hotfixes", "software", "scheduled_tasks"):
        raise ToolError("aspect inválido", "InvalidArgument")
    server_ids = _resolve(servers)
    probe_map = {
        "services": ("services", {}),
        "hotfixes": ("hotfixes", {"last": 100}),
        "software": ("software", {}),
        "scheduled_tasks": ("scheduled_tasks", {"include_microsoft": True}),
    }
    probe, args = probe_map[aspect]
    body = _run_probe(server_ids, probe, args)
    per_server: dict[str, set[str]] = {}
    for sid, res in body["results"].items():
        if res.get("ok"):
            per_server[sid] = _compare_key(aspect, res)
        else:
            per_server[sid] = set()
    all_items: set[str] = set()
    for s in per_server.values():
        all_items |= s
    only_in: dict[str, list[str]] = {}
    for sid, items in per_server.items():
        others = set()
        for o_sid, o_items in per_server.items():
            if o_sid != sid:
                others |= o_items
        only_in[sid] = sorted(items - others)
    return {"ok": True, "aspect": aspect, "only_in": only_in, "union_count": len(all_items), **body}


def query_audit_log(
    since_hours: float = 24,
    tool: Optional[str] = None,
    server: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    rows = get_audit().query(since_hours=since_hours, tool=tool, server=server, limit=limit)
    return {"ok": True, "entries": rows, "count": len(rows)}


def wait_for_server(server: str, timeout_seconds: int = 900, poll_seconds: int = 15) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("wait_for_server requiere un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    srv = get_server(sid)
    deadline = time.time() + timeout_seconds
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            with socket.create_connection((srv.hostname, srv.port), timeout=3):
                probe = _run_probe([sid], "whoami", timeout=60)
                if probe["results"].get(sid, {}).get("ok"):
                    return {
                        "ok": True,
                        "server": sid,
                        "online": True,
                        "attempts": attempts,
                        "waited_seconds": attempts * poll_seconds,
                    }
        except OSError:
            pass
        time.sleep(poll_seconds)
    return {
        "ok": False,
        "error": f"timeout tras {timeout_seconds}s esperando WinRM",
        "error_code": "Timeout",
        "server": sid,
        "attempts": attempts,
    }


def run_powershell(
    servers: ServersSelector,
    script: str,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    server_ids = _resolve(servers)
    for sid in server_ids:
        try:
            require_permission(get_server(sid), "run_powershell")
        except PermissionError as exc:
            raise ToolError(str(exc), "NotAllowed") from exc
    started = time.perf_counter()
    wrap_script = f"""
$ErrorActionPreference = 'Continue'
try {{
  $r = & {{ {script} }}
  if ($null -ne $r) {{ $r | ConvertTo-Json -Depth 6 -Compress }}
  else {{ '{{"output":null}}' }}
}} catch {{
  @{{ errors = @($_.Exception.Message); had_errors = $true }} | ConvertTo-Json -Compress
}}
"""
    try:
        raw = get_worker().invoke_script(server_ids, wrap_script, timeout=timeout_seconds)
    except WorkerError as exc:
        raise ToolError(str(exc), exc.code) from exc
    results: dict[str, Any] = {}
    for sid in server_ids:
        entry = (raw.get("results") or {}).get(sid, {})
        if not entry.get("ok", True) and entry.get("error"):
            results[sid] = {"ok": False, "error": entry["error"], "error_code": "RemoteError"}
            continue
        data = entry.get("data")
        if isinstance(data, list):
            data = data[0] if len(data) == 1 else data
        results[sid] = {"ok": True, "output": data, "had_errors": False}
    return {
        "ok": True,
        "results": results,
        "summary": {"total": len(server_ids), "ok": sum(1 for r in results.values() if r.get("ok"))},
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def manage_service(
    servers: ServersSelector,
    name: str,
    action: str = "start",
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    if action not in ("start", "stop", "restart"):
        raise ToolError("action inválida", "InvalidArgument")
    if "*" in name or "?" in name:
        raise ToolError("nombre de servicio debe ser exacto (sin comodines)", "InvalidArgument")
    settings = get_settings()
    if name in settings.protected_services and action in ("stop", "restart"):
        raise ToolError(f"servicio protegido: {name}", "NotAllowed")
    server_ids = _resolve(servers)
    for sid in server_ids:
        try:
            require_permission(get_server(sid), "manage_services")
        except PermissionError as exc:
            raise ToolError(str(exc), "NotAllowed") from exc
    return {
        "ok": True,
        **_run_probe(
            server_ids,
            "manage_service",
            {"name": name, "action": action, "dry_run": dry_run, "force": force},
        ),
    }


def set_service_start_mode(
    server: str,
    name: str,
    start_mode: str = "Automatic",
    dry_run: bool = True,
) -> dict[str, Any]:
    if start_mode not in ("Automatic", "Manual", "Disabled"):
        raise ToolError("start_mode inválido", "InvalidArgument")
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "manage_services")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    return {
        "ok": True,
        **_run_probe([sid], "set_service_startmode", {"name": name, "start_mode": start_mode, "dry_run": dry_run}),
    }


def stop_process(
    server: str,
    pid: Optional[int] = None,
    name: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    if pid is None and not name:
        raise ToolError("indica pid o name", "InvalidArgument")
    if pid is not None and is_protected_pid(pid):
        raise ToolError(f"PID protegido: {pid}", "NotAllowed")
    if name and is_protected_process(name):
        raise ToolError(f"proceso protegido: {name}", "NotAllowed")
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "manage_processes")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    return {
        "ok": True,
        **_run_probe([sid], "stop_process", {"pid": pid, "name": name, "dry_run": dry_run}),
    }


def manage_scheduled_task(
    server: str,
    task_path: str,
    task_name: str,
    action: str = "run",
    dry_run: bool = True,
) -> dict[str, Any]:
    if action not in ("run", "stop", "enable", "disable"):
        raise ToolError("action inválida", "InvalidArgument")
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "manage_tasks")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    args = {"task_path": task_path, "task_name": task_name, "action": action, "dry_run": dry_run}
    return {"ok": True, **_run_probe([sid], "manage_task", args)}


def write_remote_file(
    server: str,
    path: str,
    content: str,
    append: bool = False,
    create_dirs: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "file_transfer")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    args = {
        "path": path,
        "content": content,
        "append": append,
        "create_dirs": create_dirs,
        "backup": backup,
        "dry_run": False,
    }
    return {"ok": True, **_run_probe([sid], "write_file", args)}


def copy_to_server(server: str, local_path: str, remote_path: str) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "file_transfer")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    lp = Path(local_path)
    if not lp.is_file() and not lp.is_dir():
        raise ToolError("local_path no existe", "NotFound")
    size = lp.stat().st_size if lp.is_file() else 0
    try:
        get_worker().copy(
            "copy_to",
            sid,
            local_path=str(lp.resolve()),
            remote_path=remote_path,
            recurse=lp.is_dir(),
        )
    except WorkerError as exc:
        raise ToolError(str(exc), exc.code) from exc
    return {"ok": True, "results": {sid: {"ok": True, "bytes_copied": size, "bytes_copied_human": human_size(size)}}}


def copy_from_server(server: str, remote_path: str, local_path: str) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "file_transfer")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    try:
        get_worker().copy("copy_from", sid, local_path=local_path, remote_path=remote_path, recurse=False)
    except WorkerError as exc:
        raise ToolError(str(exc), exc.code) from exc
    lp = Path(local_path)
    size = lp.stat().st_size if lp.is_file() else 0
    return {"ok": True, "results": {sid: {"ok": True, "bytes_copied": size, "bytes_copied_human": human_size(size)}}}


def prepare_server_restart(server: str, reason: str, delay_seconds: int = 60) -> dict[str, Any]:
    if not reason or len(reason.strip()) < 3:
        raise ToolError("reason demasiado corto", "InvalidArgument")
    delay_seconds = max(30, min(delay_seconds, 3600))
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    try:
        require_permission(get_server(sid), "restart_server")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    info = _run_probe([sid], "restart_prepare")
    params = {"reason": reason.strip(), "delay_seconds": delay_seconds}
    token, record = get_restart_confirmations().issue(server_id=sid, params=params)
    return {
        "ok": True,
        "server": sid,
        "reason": reason,
        "delay_seconds": delay_seconds,
        "token_prefix": record.token_prefix,
        "expires_at": record.expires_at.isoformat().replace("+00:00", "Z"),
        "restart_token": token,
        "context": info["results"].get(sid),
        "message": "Llama confirm_server_restart con el token dentro del TTL (no se audita el token).",
    }


def confirm_server_restart(
    server: str,
    reason: str,
    delay_seconds: int,
    token: str,
) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    srv = get_server(sid)
    try:
        require_permission(srv, "restart_server")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    params = {"reason": reason.strip(), "delay_seconds": delay_seconds}
    try:
        get_restart_confirmations().consume(token, server_id=sid, params=params)
    except ValueError as exc:
        raise ToolError(str(exc), "InvalidArgument") from exc
    delay_seconds = max(30, min(delay_seconds, 3600))
    proc = subprocess.run(
        [
            "shutdown.exe",
            "/r",
            "/m",
            f"\\\\{srv.hostname}",
            "/t",
            str(delay_seconds),
            "/c",
            reason,
            "/d",
            "p:0:0",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"shutdown.exe código {proc.returncode}",
            "error_code": "RemoteError",
            "stderr": (proc.stderr or "")[:500],
        }
    return {"ok": True, "initiated": True, "server": sid, "delay_seconds": delay_seconds}


def cancel_server_restart(server: str) -> dict[str, Any]:
    server_ids = _resolve(server)
    if len(server_ids) != 1:
        raise ToolError("un solo servidor", "InvalidArgument")
    sid = server_ids[0]
    srv = get_server(sid)
    try:
        require_permission(srv, "restart_server")
    except PermissionError as exc:
        raise ToolError(str(exc), "NotAllowed") from exc
    proc = subprocess.run(
        ["shutdown.exe", "/a", "/m", f"\\\\{srv.hostname}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return {
        "ok": True,
        "cancelled": proc.returncode == 0,
        "exit_code": proc.returncode,
        "no_shutdown_in_progress": proc.returncode == 1116,
    }


def disconnect_sessions(servers: Optional[ServersSelector] = None) -> dict[str, Any]:
    if servers is None:
        get_worker().close_sessions(None)
        return {"ok": True, "disconnected": "all"}
    server_ids = _resolve(servers)
    get_worker().close_sessions(server_ids)
    return {"ok": True, "disconnected": server_ids}
