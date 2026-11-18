#!/usr/bin/env python3
"""Validación en vivo read-only de servidores NiFi configurados."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from typing import Any

from dotenv import load_dotenv

from nifi_mcp.config import project_root
from nifi_mcp.diagnostics import DiagnosticsService
from nifi_mcp.services import NiFiService


def _dns_probe(host: str) -> dict[str, Any]:
    try:
        socket.getaddrinfo(host, None)
        return {"ok": True, "error": None}
    except socket.gaierror as exc:
        return {"ok": False, "error": str(exc)}


async def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _next_action(record: dict[str, Any]) -> str:
    if not record.get("dns", {}).get("ok"):
        return "Verificar DNS/hostname y conectividad de red"
    tcp_ok = any(p.get("ok") for p in record.get("tcp_probes") or [])
    if not tcp_ok:
        return "Verificar firewall y puertos 443/8443/8445/8080"
    if not record.get("nifi_api", {}).get("ok"):
        return "Confirmar URL NiFi, TLS y credenciales en .env"
    if not record.get("authentication", {}).get("ok"):
        return "Configurar auth (username_password/bearer_token/client_certificate)"
    if record.get("result") != "ok":
        return "Revisar salud del cluster y bulletins ERROR"
    return "Servidor operativo"


async def validate_host(server_id: str, service: NiFiService, diag: DiagnosticsService) -> dict[str, Any]:
    cfg = service.config
    srv = cfg.servers[server_id]
    record: dict[str, Any] = {
        "hostname": srv.host,
        "server_id": server_id,
        "name": srv.name,
        "environment": srv.environment,
        "enabled": srv.enabled,
    }
    if not srv.enabled:
        record.update(
            {
                "dns": {"ok": False, "error": "disabled"},
                "tcp": {"ok": False, "error": "disabled"},
                "https": {"ok": False, "error": "disabled"},
                "nifi_api": {"ok": False, "error": "disabled"},
                "authentication": {"ok": False, "error": "disabled"},
                "version": None,
                "result": "skipped",
                "error": "server disabled",
                "next_action": "Habilitar servidor en configuración",
            }
        )
        return record

    dns = _dns_probe(srv.host)
    record["dns"] = dns
    tcp_probes = []
    ports = [443, 8443, 8445, 8080]
    if srv.port and srv.port not in ports:
        ports.insert(0, srv.port)
    for port in ports:
        probe = await _tcp_probe(srv.host, port)
        tcp_probes.append({"port": port, **probe})
    record["tcp_probes"] = tcp_probes
    record["tcp"] = {"ok": any(p["ok"] for p in tcp_probes), "ports": tcp_probes}

    conn = await service.test_connection(server_id)
    effective_url = conn.get("effective_url") or ""
    record["https"] = {
        "ok": effective_url.lower().startswith("https://"),
        "protocol": effective_url.split(":", 1)[0].lower() if effective_url else None,
        "effective_url": effective_url or None,
        "error": conn.get("error"),
    }
    record["nifi_api"] = {
        "ok": conn.get("ok", False),
        "discovered": conn.get("discovered"),
        "discovery_attempts": conn.get("discovery_attempts"),
        "latency_ms": conn.get("latency_ms"),
        "error": conn.get("error"),
    }
    record["authentication"] = {
        "ok": conn.get("authenticated", False),
        "auth_type": conn.get("auth_type"),
        "warnings": conn.get("warnings"),
    }
    record["version"] = conn.get("nifi_version")
    record["cluster_mode"] = conn.get("cluster_mode")

    if conn.get("ok"):
        health = await diag.health_check(server_id)
        record["health"] = health.get("overall_status")
        record["result"] = "ok" if health.get("overall_status") == "healthy" else health.get("overall_status")
        record["error"] = None
    else:
        record["result"] = "failed"
        record["error"] = conn.get("error")

    record["next_action"] = _next_action(record)
    return record


async def run_validation(server_filter: str | None = None) -> dict[str, Any]:
    load_dotenv(project_root() / ".env", override=False)
    service = NiFiService()
    diag = DiagnosticsService(service)
    cfg = service.config
    ids = list(cfg.servers.keys())
    if server_filter:
        ids = [s.strip() for s in server_filter.split(",") if s.strip()]
    results = []
    for sid in ids:
        if sid not in cfg.servers:
            results.append(
                {
                    "hostname": sid,
                    "dns": {"ok": False, "error": "unknown"},
                    "tcp": {"ok": False},
                    "https": {"ok": False},
                    "nifi_api": {"ok": False},
                    "authentication": {"ok": False},
                    "version": None,
                    "result": "failed",
                    "error": "unknown server",
                    "next_action": "Corregir server_id en configuración",
                }
            )
            continue
        results.append(await validate_host(sid, service, diag))
    await service.close()
    ok_count = sum(1 for r in results if r.get("result") == "ok")
    return {"ok": ok_count == len([r for r in results if r.get("result") != "skipped"]), "hosts": results, "ok_count": ok_count, "total": len(results)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validación live read-only NiFi MCP")
    parser.add_argument("--server", help="IDs separados por coma (default: todos)")
    parser.add_argument("--json", action="store_true", help="Salida JSON")
    args = parser.parse_args()
    payload = asyncio.run(run_validation(args.server))
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        for r in payload["hosts"]:
            print(
                f"{r.get('hostname')}: result={r.get('result')} version={r.get('version')} "
                f"dns={r.get('dns', {}).get('ok')} tcp={r.get('tcp', {}).get('ok')} "
                f"api={r.get('nifi_api', {}).get('ok')} auth={r.get('authentication', {}).get('ok')}"
            )
            print(f"  next_action: {r.get('next_action')}")
        print(f"Resumen: {payload['ok_count']}/{payload['total']} OK")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
