"""Agregación multi-servidor con concurrencia acotada."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nifi_readonly_mcp.config import resolve_server_ids
from nifi_readonly_mcp.diagnostics import DiagnosticsService
from nifi_readonly_mcp.models import AuthType
from nifi_readonly_mcp.normalize import apply_limit, wrap_result
from nifi_readonly_mcp.responses import validate_limit
from nifi_readonly_mcp.services import NiFiService

_SEVERITY_ORDER = {"critical": 3, "warning": 2, "informational": 1}


class AggregateService:
    def __init__(self, service: NiFiService | None = None) -> None:
        self.service = service or NiFiService()
        self.diagnostics = DiagnosticsService(self.service)
        self._sem = asyncio.Semaphore(self.service.config.global_config.max_parallel_servers)

    def _effective_limit(self, limit: int | None) -> int:
        g = self.service.config.global_config
        return validate_limit(limit, default=g.default_limit, max_limit=g.max_limit)

    async def _run_for_servers(
        self,
        server_ids: list[str],
        fn: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        async def guarded(sid: str) -> None:
            async with self._sem:
                try:
                    payload = await fn(sid)
                    payload.setdefault("server_id", sid)
                    results.append(payload)
                except Exception as exc:
                    errors.append({"server_id": sid, "error": str(exc)})

        await asyncio.gather(*(guarded(s) for s in server_ids))
        return wrap_result(
            ok=len(errors) == 0,
            results=results,
            total=len(server_ids),
            returned=len(results),
            errors=errors,
        )

    async def _aggregate_items(
        self,
        server_ids: list[str],
        fn: Callable[[str], Awaitable[dict[str, Any]]],
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        all_items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        per_server: list[dict[str, Any]] = []

        async def guarded(sid: str) -> None:
            async with self._sem:
                try:
                    payload = await fn(sid)
                    items = payload.get("items") or []
                    all_items.extend(items)
                    per_server.append(
                        {
                            "server_id": sid,
                            "ok": payload.get("ok", True),
                            "total": payload.get("total", len(items)),
                            "returned": len(items),
                        }
                    )
                except Exception as exc:
                    errors.append({"server_id": sid, "error": str(exc)})

        await asyncio.gather(*(guarded(s) for s in server_ids))
        total = len(all_items)
        limited, truncated = apply_limit(all_items, eff_limit)
        return wrap_result(
            ok=len(errors) == 0,
            items=limited,
            total=total,
            returned=len(limited),
            truncated=truncated,
            servers_checked=len(server_ids),
            per_server=per_server,
            errors=errors,
        )

    def _credentials_ready(self, sid: str) -> bool:
        srv = self.service.config.servers[sid]
        creds = self.service.auth.resolve_credentials(srv)
        if srv.auth.type == AuthType.NONE:
            return True
        if srv.auth.type == AuthType.BEARER_TOKEN:
            return bool(creds.get("token"))
        if srv.auth.type == AuthType.USERNAME_PASSWORD:
            return bool(creds.get("username") and creds.get("password"))
        return True

    async def list_servers(self) -> dict[str, Any]:
        cfg = self.service.config
        items = []
        for sid, srv in cfg.servers.items():
            entry: dict[str, Any] = {
                "server_id": sid,
                "name": srv.name,
                "host": srv.host,
                "environment": srv.environment,
                "enabled": srv.enabled,
                "base_url": srv.base_url,
                "auth_type": srv.auth.type.value,
                "tags": srv.tags,
            }
            if srv.enabled:
                entry["credentials_ready"] = self._credentials_ready(sid)
            items.append(entry)
        return wrap_result(ok=True, items=items, total=len(items), returned=len(items))

    async def test_all_connections(
        self, server_ids: list[str] | None = None
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        return await self._run_for_servers(ids, self.service.test_connection)

    async def health_check_multi(
        self,
        server_ids: list[str] | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        if len(ids) == 1:
            single = await self.diagnostics.health_check(ids[0], refresh=refresh)
            return self._health_summary([single], ids)

        async def check(sid: str) -> dict[str, Any]:
            return await self.diagnostics.health_check(sid, refresh=refresh)

        payload = await self._run_for_servers(ids, check)
        return self._health_summary(payload.get("results") or [], ids, errors=payload.get("errors"))

    def _health_summary(
        self,
        results: list[dict[str, Any]],
        ids: list[str],
        *,
        errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        counts = {"healthy": 0, "warning": 0, "critical": 0, "unreachable": 0, "error": 0}
        rows: list[dict[str, Any]] = []
        all_findings: list[dict[str, Any]] = []
        for r in results:
            status = r.get("overall_status") or r.get("health") or "error"
            if status in counts:
                counts[status] += 1
            else:
                counts["error"] += 1
            by_severity: dict[str, int] = {}
            for f in r.get("findings") or []:
                sev = str(f.get("severity") or "unknown")
                by_severity[sev] = by_severity.get(sev, 0) + 1
                all_findings.append({**f, "server_id": r.get("server_id")})
            rows.append(
                {
                    "server_id": r.get("server_id"),
                    "overall_status": status,
                    "issue_count": r.get("issue_count", len(r.get("findings") or [])),
                    "by_severity": by_severity,
                    "nifi_version": r.get("nifi_version"),
                }
            )
        for e in errors or []:
            counts["error"] += 1
            rows.append({"server_id": e.get("server_id"), "overall_status": "error", "error": e.get("error")})
        # Críticos primero: si la respuesta se recorta por tamaño, se conservan.
        all_findings.sort(key=lambda f: -_SEVERITY_ORDER.get(str(f.get("severity")), 0))
        overall = "healthy"
        if counts["critical"] or counts["unreachable"] or counts["error"]:
            overall = "critical"
        elif counts["warning"]:
            overall = "warning"
        return wrap_result(
            ok=not (errors or []),
            overall_status=overall,
            servers_checked=len(ids),
            summary=counts,
            rows=rows,
            findings=all_findings,
            # Sin findings por servidor: ya están (etiquetados) en la lista global.
            results=[{k: v for k, v in r.items() if k != "findings"} for r in results],
            errors=errors or [],
        )

    async def health_overview(
        self,
        server_ids: list[str] | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        return await self.health_check_multi(server_ids, refresh=refresh)

    async def compare_servers(
        self,
        server_ids: list[str] | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)

        async def snapshot(sid: str) -> dict[str, Any]:
            conn = await self.service.test_connection(sid)
            # all_processors: el total real, no los primeros default_limit (100).
            items = await self.service.all_processors(sid, refresh=refresh)
            states = [(p.get("state") or "").upper() for p in items]
            status = await self.service.get_status(sid, recursive=False, refresh=refresh)
            root = status.get("status") or {}
            agg = root.get("aggregateSnapshot") or root if isinstance(root, dict) else {}
            bulletins = await self.service.get_bulletins(sid, limit=500)
            bitems = bulletins.get("items") or []
            err_count = sum(1 for b in bitems if (b.get("level") or "").upper() == "ERROR")
            warn_count = sum(1 for b in bitems if (b.get("level") or "").upper() in ("WARN", "WARNING"))
            pgs = await self.service.list_process_groups(sid, recursive=True, refresh=refresh)
            return {
                "server_id": sid,
                "ok": conn.get("ok", False),
                "version": conn.get("nifi_version"),
                "cluster_mode": conn.get("cluster_mode"),
                "cluster_connected": conn.get("cluster_connected"),
                "processor_total": len(items),
                "processor_running": sum(1 for s in states if s == "RUNNING"),
                "processor_stopped": sum(1 for s in states if s == "STOPPED"),
                "processor_invalid": sum(1 for s in states if s == "INVALID"),
                "process_group_count": pgs.get("total", 0),
                "queued_flowfiles": int(agg.get("flowFilesQueued") or 0),
                "queued_bytes": int(agg.get("bytesQueued") or 0),
                "bulletins_error": err_count,
                "bulletins_warn": warn_count,
                "effective_url": conn.get("effective_url"),
            }

        payload = await self._run_for_servers(ids, snapshot)
        payload["comparisons"] = payload.pop("results", [])
        return payload

    async def search_multi(
        self,
        query: str,
        *,
        server_ids: list[str] | None = None,
        component_type: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        del refresh
        ids = resolve_server_ids(server_ids)

        async def do_search(sid: str) -> dict[str, Any]:
            return await self.service.search(
                sid, query, component_type=component_type, limit=None
            )

        return await self._aggregate_items(ids, do_search, limit=limit)

    async def get_errors_multi(
        self,
        *,
        server_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        return await self._aggregate_items(
            ids, lambda sid: self.diagnostics.get_errors(sid, limit=limit), limit=limit
        )

    async def find_processors_multi(
        self,
        *,
        server_ids: list[str] | None = None,
        name: str | None = None,
        processor_type: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
        include_details: bool = False,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        max_limit = self.service.config.global_config.max_limit

        async def do_find(sid: str) -> dict[str, Any]:
            return await self.diagnostics.find_processors(
                sid,
                name=name,
                processor_type=processor_type,
                state=state,
                limit=max_limit,
                refresh=refresh,
                include_details=include_details,
            )

        return await self._aggregate_items(ids, do_find, limit=limit)

    async def find_large_queues_multi(
        self,
        *,
        server_ids: list[str] | None = None,
        min_flowfiles: int | None = None,
        min_bytes: int | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)

        async def do_find(sid: str) -> dict[str, Any]:
            return await self.diagnostics.find_large_queues(
                sid,
                min_flowfiles=min_flowfiles,
                min_bytes=min_bytes,
                limit=None,
                refresh=refresh,
            )

        return await self._aggregate_items(ids, do_find, limit=limit)

    async def find_stopped_processors_multi(
        self,
        *,
        server_ids: list[str] | None = None,
        refresh: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        max_limit = self.service.config.global_config.max_limit
        return await self._aggregate_items(
            ids,
            lambda sid: self.diagnostics.find_stopped_processors(
                sid, refresh=refresh, limit=max_limit
            ),
            limit=limit,
        )

    async def find_invalid_components_multi(
        self,
        *,
        server_ids: list[str] | None = None,
        refresh: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        max_limit = self.service.config.global_config.max_limit
        return await self._aggregate_items(
            ids,
            lambda sid: self.diagnostics.find_invalid_components(
                sid, refresh=refresh, limit=max_limit
            ),
            limit=limit,
        )

    async def find_problem_controller_services_multi(
        self,
        *,
        server_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        return await self._aggregate_items(
            ids, self.diagnostics.find_problem_controller_services
        )

    async def list_connections_multi(
        self,
        *,
        server_ids: list[str] | None = None,
        process_group_id: str | None = None,
        min_queued: int = 0,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        max_limit = self.service.config.global_config.max_limit

        async def do_list(sid: str) -> dict[str, Any]:
            return await self.service.list_connections(
                sid,
                process_group_id=process_group_id,
                min_queued=min_queued,
                limit=max_limit,
                refresh=refresh,
            )

        return await self._aggregate_items(ids, do_list, limit=limit)

    async def cluster_status_multi(
        self, server_ids: list[str] | None = None
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        return await self._run_for_servers(ids, self.service.cluster_status)

    async def flow_versions_multi(
        self,
        server_ids: list[str] | None = None,
        *,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        max_limit = self.service.config.global_config.max_limit

        async def do_list(sid: str) -> dict[str, Any]:
            return await self.service.flow_versions(sid, limit=max_limit, refresh=refresh)

        return await self._aggregate_items(ids, do_list, limit=limit)

    async def system_diagnostics_multi(
        self, server_ids: list[str] | None = None
    ) -> dict[str, Any]:
        ids = resolve_server_ids(server_ids)
        return await self._run_for_servers(ids, self.service.system_diagnostics)

    async def close(self) -> None:
        await self.service.close()
