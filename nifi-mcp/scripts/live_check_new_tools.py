#!/usr/bin/env python3
"""Prueba en vivo (solo lectura) de las herramientas NiFi añadidas en IMPROVE."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nifi_mcp.aggregate import AggregateService
from nifi_mcp.config import load_config
from nifi_mcp.exceptions import NiFiAuthError


def _summarize(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        err = result.get("error") or result.get("errors")
        return f"FAIL: {err}"
    if "items" in result:
        return f"ok items={result.get('returned', len(result.get('items') or []))} total={result.get('total')}"
    if "results" in result:
        ok_n = sum(1 for r in result["results"] if r.get("ok"))
        err_n = len(result.get("errors") or [])
        return f"ok servers_ok={ok_n} errors={err_n}"
    if "diagnostics" in result:
        return "ok diagnostics"
    return "ok"


async def _timed(
    label: str,
    coro_fn: Callable[[], Awaitable[dict[str, Any]]],
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    t0 = time.perf_counter()
    record: dict[str, Any] = {"tool": label}
    try:
        result = await coro_fn()
        elapsed = time.perf_counter() - t0
        record["elapsed_s"] = round(elapsed, 2)
        record["summary"] = _summarize(result)
        record["ok"] = bool(result.get("ok"))
        if not record["ok"]:
            record["error"] = result.get("error") or result.get("errors")
        records.append(record)
        print(f"[{elapsed:6.2f}s] {label}: {record['summary']}")
        return result
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        record["elapsed_s"] = round(elapsed, 2)
        record["summary"] = f"EXCEPTION: {exc}"
        record["ok"] = False
        record["error"] = str(exc)
        records.append(record)
        print(f"[{elapsed:6.2f}s] {label}: EXCEPTION {exc}")
        return None


async def main() -> int:
    cfg = load_config()
    server_ids = list(cfg.enabled_servers().keys())
    print(f"Servidores habilitados: {', '.join(server_ids)}\n")

    agg = AggregateService()
    svc = agg.service
    records: list[dict[str, Any]] = []
    queue_target: tuple[str, str] | None = None
    cs_targets: dict[str, str] = {}
    pc_targets: dict[str, str] = {}

    try:
        await _timed(
            "list_connections (all, min_queued=1)",
            lambda: agg.list_connections_multi(min_queued=1, limit=20),
            records,
        )
        conn_out = await agg.list_connections_multi(min_queued=1, limit=50)
        if conn_out.get("ok"):
            for item in conn_out.get("items") or []:
                sid = item.get("server")
                cid = item.get("id")
                if sid and cid and queue_target is None:
                    queue_target = (sid, cid)
                    break

        await _timed(
            "flow_versions (all)",
            lambda: agg.flow_versions_multi(limit=50),
            records,
        )
        await _timed(
            "cluster_status (all)",
            lambda: agg.cluster_status_multi(),
            records,
        )
        await _timed(
            "system_diagnostics (all)",
            lambda: agg.system_diagnostics_multi(),
            records,
        )

        for sid in server_ids:
            await _timed(
                f"get_provenance ({sid})",
                lambda s=sid: svc.get_provenance(
                    s, max_results=10, since_minutes=120
                ),
                records,
            )

        for sid in server_ids:
            try:
                cs = await svc.list_controller_services(sid, limit=1)
                if cs.get("ok") and cs.get("items"):
                    cs_targets[sid] = cs["items"][0]["id"]
                pc = await svc.list_parameter_contexts(sid, limit=1)
                if pc.get("ok") and pc.get("items"):
                    pc_targets[sid] = pc["items"][0]["id"]
            except NiFiAuthError as exc:
                print(f"  (omitido descubrimiento CS/PC en {sid}: {exc})")

        for sid, cs_id in cs_targets.items():
            await _timed(
                f"get_controller_service ({sid})",
                lambda s=sid, i=cs_id: svc.get_controller_service(s, i),
                records,
            )

        for sid, pc_id in pc_targets.items():
            await _timed(
                f"get_parameter_context ({sid})",
                lambda s=sid, i=pc_id: svc.get_parameter_context(s, i),
                records,
            )

        if queue_target:
            sid, cid = queue_target
            await _timed(
                f"list_queue ({sid}, {cid[:8]}…)",
                lambda: svc.list_queue(sid, cid, max_results=5),
                records,
            )
        else:
            msg = "list_queue: omitido (ninguna conexión con cola >= 1)"
            print(msg)
            records.append({"tool": "list_queue", "ok": None, "summary": msg})

        print("\n--- Resumen ---")
        fails = [r for r in records if r.get("ok") is False]
        skips = [r for r in records if r.get("ok") is None]
        oks = [r for r in records if r.get("ok") is True]
        print(f"OK: {len(oks)}  FAIL: {len(fails)}  SKIP: {len(skips)}")
        for r in records:
            mark = "OK" if r.get("ok") else ("SKIP" if r.get("ok") is None else "FAIL")
            print(f"  [{mark}] {r['tool']}: {r.get('summary', r.get('error', ''))}")

        return 1 if fails else 0
    finally:
        await agg.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
