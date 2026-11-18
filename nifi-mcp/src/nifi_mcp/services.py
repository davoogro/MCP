"""Operaciones de alto nivel sobre NiFi (inventario, estado, búsqueda)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from nifi_mcp.auth import AuthManager, redact_mapping
from nifi_mcp.cache import TTLCache
from nifi_mcp.client import NiFiClient
from nifi_mcp.config import load_config
from nifi_mcp.models import AppConfig
from nifi_mcp.normalize import (
    ROOT_LABEL,
    SEARCH_ENTITY_TYPES,
    SEARCH_TYPE_MAP,
    apply_limit,
    build_breadcrumb,
    extract_breadcrumb_names,
    normalize_bulletin,
    normalize_connection,
    normalize_controller_service,
    normalize_parameter_context,
    normalize_process_group,
    normalize_processor,
    wrap_result,
)
from nifi_mcp.responses import validate_limit

logger = logging.getLogger(__name__)

ROOT_PG_ID = "root"

_PROVENANCE_POLL_ATTEMPTS = 40
_PROVENANCE_POLL_SECONDS = 0.35
_QUEUE_LISTING_POLL_ATTEMPTS = 40
_QUEUE_LISTING_POLL_SECONDS = 0.35


def _nifi_provenance_timestamp(when: datetime) -> str:
    return when.strftime("%m/%d/%Y %H:%M:%S UTC")


def _index_connection_statuses(root_status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Mapa connection_id -> aggregateSnapshot de cola."""

    out: dict[str, dict[str, Any]] = {}

    def walk(pg_status: dict[str, Any]) -> None:
        for conn in pg_status.get("connectionStatusSnapshots") or pg_status.get("connections") or []:
            snap = conn.get("connectionStatusSnapshot") or conn
            comp = conn.get("connection") or snap.get("connection") or {}
            cid = comp.get("id") if isinstance(comp, dict) else None
            if cid:
                out[cid] = snap
        for child in (
            pg_status.get("processGroupStatusSnapshots")
            or pg_status.get("childProcessGroupStatusSnapshots")
            or []
        ):
            child_snap = child.get("processGroupStatusSnapshot") or child
            if isinstance(child_snap, dict):
                walk(child_snap)

    if isinstance(root_status, dict):
        walk(root_status)
    return out


def compact_status_history(history: Any, max_points: int) -> Any:
    """Reduce un statusHistory de NiFi a lo útil para un LLM.

    - Conserva solo los ``max_points`` snapshots agregados más recientes.
    - Omite ``nodeSnapshots`` (uno por nodo de cluster; multiplica el tamaño).
    - ``fieldDescriptors`` queda como mapa campo -> etiqueta.
    """
    if not isinstance(history, dict):
        return history
    out: dict[str, Any] = {
        k: v
        for k, v in history.items()
        if k not in ("aggregateSnapshots", "nodeSnapshots", "fieldDescriptors")
    }
    snaps = history.get("aggregateSnapshots") or []
    max_points = max(1, int(max_points))
    out["points_total"] = len(snaps)
    out["aggregateSnapshots"] = snaps[-max_points:]
    if len(snaps) > max_points:
        out["points_truncated"] = True
    if history.get("nodeSnapshots"):
        out["node_snapshots_omitted"] = len(history["nodeSnapshots"])
    fields = history.get("fieldDescriptors") or []
    if fields:
        out["fields"] = {
            f.get("field"): f.get("label") for f in fields if isinstance(f, dict) and f.get("field")
        }
    return out


class NiFiService:
    def __init__(
        self,
        config: AppConfig | None = None,
        auth_manager: AuthManager | None = None,
        cache: TTLCache | None = None,
    ) -> None:
        self.config = config or load_config()
        self.auth = auth_manager or AuthManager()
        self.cache = cache or TTLCache()
        self._clients: dict[str, NiFiClient] = {}

    def _client(self, server_id: str) -> NiFiClient:
        if server_id not in self._clients:
            srv = self.config.get_server(server_id)
            self._clients[server_id] = NiFiClient(srv, self.config.global_config, self.auth)
        return self._clients[server_id]

    def _effective_limit(self, limit: int | None) -> int:
        g = self.config.global_config
        return validate_limit(limit, default=g.default_limit, max_limit=g.max_limit)

    async def close(self) -> None:
        for c in self._clients.values():
            await c.close()

    async def test_connection(self, server_id: str) -> dict[str, Any]:
        srv = self.config.servers[server_id]
        client = self._client(server_id)
        started = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "server_id": server_id,
            "name": srv.name,
            "host": srv.host,
            "hostname": srv.host,
            "ok": False,
            "reachable": False,
            "authenticated": False,
        }
        warnings: list[str] = []
        try:
            if not client.base_url:
                await client.discover_base_url()
            about = await client.get_about()
            auth_cfg = await client.get_auth_configuration()
            cluster = await client.get_cluster_summary()
            cluster_info = cluster.get("cluster") if isinstance(cluster, dict) else cluster
            result.update(
                {
                    "ok": True,
                    "reachable": True,
                    "authenticated": client.authenticated or srv.auth.type.value == "none",
                    "base_url": client.base_url,
                    "effective_url": client.base_url,
                    "discovered": srv.base_url is None and bool(client.discovery_attempts),
                    "discovery_attempts": [a.model_dump() for a in client.discovery_attempts],
                    "nifi_version": (about.get("about") or about).get("version"),
                    "auth_type": srv.auth.type.value,
                    "tls_verify": srv.tls.verify,
                    "cluster_mode": "cluster"
                    if cluster_info and cluster_info.get("connectedToCluster")
                    else "standalone",
                    "cluster_connected": (cluster_info or {}).get("connectedToCluster"),
                    "latency_ms": next(
                        (
                            a.latency_ms
                            for a in client.discovery_attempts
                            if a.ok and a.latency_ms is not None
                        ),
                        None,
                    ),
                }
            )
            if not srv.tls.verify:
                warnings.append("tls_verification_disabled_expired_or_untrusted_cert")
            if auth_cfg is not None:
                result["auth_configuration"] = redact_mapping(auth_cfg)
            else:
                warnings.append("auth_configuration_unavailable")
            if cluster is None:
                warnings.append("cluster_summary_unavailable")
            else:
                result["cluster"] = redact_mapping(cluster if isinstance(cluster, dict) else {})
        except Exception as exc:
            result["error"] = str(exc)
            result["discovery_attempts"] = [a.model_dump() for a in client.discovery_attempts]
        result["warnings"] = warnings
        result["timestamp"] = started.isoformat()
        return result

    async def server_info(self, server_id: str) -> dict[str, Any]:
        srv = self.config.servers[server_id]
        client = self._client(server_id)
        warnings: list[str] = []
        payload: dict[str, Any] = {
            "server_id": server_id,
            "name": srv.name,
            "hostname": srv.host,
            "environment": srv.environment,
            "description": srv.description,
            "configured_base_url": srv.base_url,
            "auth_type": srv.auth.type.value,
            "tags": srv.tags,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            conn = await self.test_connection(server_id)
            payload["connection"] = {
                k: conn.get(k)
                for k in (
                    "ok",
                    "reachable",
                    "authenticated",
                    "base_url",
                    "effective_url",
                    "discovered",
                    "nifi_version",
                    "cluster_mode",
                    "cluster_connected",
                    "latency_ms",
                    "warnings",
                )
            }
            payload["effective_url"] = conn.get("effective_url")
            payload["nifi_version"] = conn.get("nifi_version")
            payload["cluster_mode"] = conn.get("cluster_mode")
            if conn.get("cluster"):
                payload["cluster"] = conn["cluster"]
            if not conn.get("ok"):
                payload["ok"] = False
                payload["error"] = conn.get("error")
                payload["warnings"] = conn.get("warnings") or []
                return payload

            status = await self.get_status(server_id, recursive=False)
            payload["general_status"] = status.get("status")
            user = await client.get_current_user()
            if user is not None:
                identity = user.get("identity") or user.get("currentUser") or user
                payload["current_user"] = redact_mapping(
                    identity if isinstance(identity, dict) else {"identity": identity}
                )
            else:
                warnings.append("current_user_unavailable")
            payload["ok"] = True
        except Exception as exc:
            payload["ok"] = False
            payload["error"] = str(exc)
        payload["warnings"] = warnings + payload.get("warnings", [])
        return payload

    async def _fetch_pg_flow(self, server_id: str, pg_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        pg = ROOT_PG_ID if pg_id in (ROOT_PG_ID, "root") else pg_id
        return await client.get_json(f"/flow/process-groups/{pg}")

    async def all_processors(
        self,
        server_id: str,
        *,
        include_details: bool = False,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Todos los procesadores del flow (sin tope de limit); uso interno de diagnósticos."""
        return await self._collect_processors_recursive(
            server_id, include_details=include_details, refresh=refresh
        )

    async def _collect_processors_recursive(
        self,
        server_id: str,
        *,
        include_details: bool = False,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = ("all_processors", str(include_details))
        if not refresh:
            cached = await self.cache.get("inventory", server_id, *cache_key)
            if cached is not None:
                return cached

        items: list[dict[str, Any]] = []
        visited: set[str] = set()

        async def walk(pg_id: str, ancestor_names: list[str]) -> None:
            norm_id = ROOT_PG_ID if pg_id in (ROOT_PG_ID, "root") else pg_id
            if norm_id in visited:
                return
            visited.add(norm_id)
            data = await self._fetch_pg_flow(server_id, norm_id)
            names = extract_breadcrumb_names(data) or ancestor_names
            pg_flow = data.get("processGroupFlow") or {}
            flow = pg_flow.get("flow") or {}
            pg_name = (pg_flow.get("breadcrumb") or {}).get("name") or (
                names[-1] if names else ROOT_LABEL
            )
            parent_path = build_breadcrumb(names[:-1] if names else [], include_self=pg_name)
            for proc in flow.get("processors") or []:
                comp = proc.get("component") or proc
                path = build_breadcrumb(names, include_self=comp.get("name", ""))
                items.append(
                    normalize_processor(
                        server_id,
                        proc,
                        path=path,
                        parent_path=parent_path,
                        include_details=include_details,
                    )
                )
            for child in flow.get("processGroups") or []:
                comp = child.get("component") or child
                child_id = comp.get("id")
                child_name = comp.get("name")
                if child_id:
                    await walk(child_id, names + ([child_name] if child_name else []))

        await walk(ROOT_PG_ID, [])
        await self.cache.set(
            "inventory",
            server_id,
            *cache_key,
            value=items,
            ttl_seconds=self.config.global_config.cache.inventory_ttl_seconds,
        )
        return items

    async def _collect_process_groups_recursive(
        self,
        server_id: str,
        *,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = ("all_pgs",)
        if not refresh:
            cached = await self.cache.get("inventory", server_id, *cache_key)
            if cached is not None:
                return cached
        items: list[dict[str, Any]] = []
        visited: set[str] = set()

        async def walk(pg_id: str, ancestor_names: list[str]) -> None:
            norm_id = ROOT_PG_ID if pg_id in (ROOT_PG_ID, "root") else pg_id
            if norm_id in visited:
                return
            visited.add(norm_id)
            data = await self._fetch_pg_flow(server_id, norm_id)
            names = extract_breadcrumb_names(data) or ancestor_names
            pg_flow = data.get("processGroupFlow") or {}
            flow = pg_flow.get("flow") or {}
            parent_path = build_breadcrumb(names[:-1] if len(names) > 1 else [])
            for child in flow.get("processGroups") or []:
                comp = child.get("component") or child
                child_name = comp.get("name", "")
                path = build_breadcrumb(names, include_self=child_name)
                items.append(
                    normalize_process_group(
                        server_id,
                        child,
                        path=path,
                        parent_path=parent_path,
                    )
                )
                if comp.get("id"):
                    await walk(comp["id"], names + ([child_name] if child_name else []))

        await walk(ROOT_PG_ID, [])
        await self.cache.set(
            "inventory",
            server_id,
            *cache_key,
            value=items,
            ttl_seconds=self.config.global_config.cache.inventory_ttl_seconds,
        )
        return items

    async def get_root_flow(self, server_id: str, *, refresh: bool = False) -> dict[str, Any]:
        cache_key = ("root_flow",)
        if not refresh:
            cached = await self.cache.get("inventory", server_id, *cache_key)
            if cached is not None:
                return cached
        data = await self._fetch_pg_flow(server_id, ROOT_PG_ID)
        pg_flow = data.get("processGroupFlow") or data
        names = extract_breadcrumb_names(data)
        pg = normalize_process_group(
            server_id,
            pg_flow,
            path=build_breadcrumb(names) if names else ROOT_LABEL,
            parent_path=ROOT_LABEL,
            include_details=True,
        )
        payload = wrap_result(ok=True, items=[pg], total=1, returned=1)
        await self.cache.set(
            "inventory",
            server_id,
            *cache_key,
            value=payload,
            ttl_seconds=self.config.global_config.cache.inventory_ttl_seconds,
        )
        return payload

    async def list_process_groups(
        self,
        server_id: str,
        *,
        process_group_id: str = ROOT_PG_ID,
        recursive: bool = False,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        if recursive and process_group_id in (ROOT_PG_ID, "root"):
            items = await self._collect_process_groups_recursive(server_id, refresh=refresh)
        else:
            data = await self._fetch_pg_flow(server_id, process_group_id)
            names = extract_breadcrumb_names(data)
            pg_flow = data.get("processGroupFlow") or {}
            flow = pg_flow.get("flow") or {}
            parent_path = build_breadcrumb(names[:-1] if len(names) > 1 else [])
            items = []
            for child in flow.get("processGroups") or []:
                comp = child.get("component") or child
                path = build_breadcrumb(names, include_self=comp.get("name", ""))
                items.append(
                    normalize_process_group(
                        server_id,
                        child,
                        path=path,
                        parent_path=parent_path,
                    )
                )
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def get_process_group(
        self,
        server_id: str,
        process_group_id: str,
        *,
        include_details: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = ("pg", process_group_id, str(include_details))
        if not refresh:
            cached = await self.cache.get("inventory", server_id, *cache_key)
            if cached is not None:
                return cached
        data = await self._fetch_pg_flow(server_id, process_group_id)
        names = extract_breadcrumb_names(data)
        pg = normalize_process_group(
            server_id,
            data.get("processGroupFlow") or data,
            path=build_breadcrumb(names),
            parent_path=build_breadcrumb(names[:-1] if len(names) > 1 else []),
            include_details=include_details,
        )
        payload = wrap_result(ok=True, items=[pg], total=1, returned=1)
        await self.cache.set(
            "inventory",
            server_id,
            *cache_key,
            value=payload,
            ttl_seconds=self.config.global_config.cache.inventory_ttl_seconds,
        )
        return payload

    async def list_processors(
        self,
        server_id: str,
        *,
        process_group_id: str = ROOT_PG_ID,
        recursive: bool = False,
        limit: int | None = None,
        include_details: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        if recursive and process_group_id in (ROOT_PG_ID, "root"):
            items = await self._collect_processors_recursive(
                server_id, include_details=include_details, refresh=refresh
            )
        else:
            data = await self._fetch_pg_flow(server_id, process_group_id)
            names = extract_breadcrumb_names(data)
            pg_flow = data.get("processGroupFlow") or {}
            flow = pg_flow.get("flow") or {}
            pg_name = (pg_flow.get("breadcrumb") or {}).get("name") or (
                names[-1] if names else ROOT_LABEL
            )
            parent_path = build_breadcrumb(names[:-1] if names else [], include_self=pg_name)
            items = []
            for proc in flow.get("processors") or []:
                comp = proc.get("component") or proc
                path = build_breadcrumb(names, include_self=comp.get("name", ""))
                items.append(
                    normalize_processor(
                        server_id,
                        proc,
                        path=path,
                        parent_path=parent_path,
                        include_details=include_details,
                    )
                )
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def get_processor(
        self,
        server_id: str,
        processor_id: str,
        *,
        include_details: bool = True,
    ) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json(f"/processors/{processor_id}")
        proc = normalize_processor(server_id, data, include_details=include_details)
        return wrap_result(ok=True, items=[proc], total=1, returned=1)

    async def search(
        self,
        server_id: str,
        query: str,
        *,
        component_type: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        client = self._client(server_id)
        data = await client.get_json("/flow/search-results", params={"q": query})
        results = data.get("searchResultsDTO") or data
        items: list[dict[str, Any]] = []
        keys_to_scan: list[tuple[str, str]] = []
        if component_type:
            ctype = component_type.lower()
            for key_group in SEARCH_TYPE_MAP.get(ctype, ()):
                keys_to_scan.append((key_group, ctype))
            if not keys_to_scan:
                keys_to_scan = [(k, v) for k, v in SEARCH_ENTITY_TYPES.items()]
        else:
            keys_to_scan = [(k, v) for k, v in SEARCH_ENTITY_TYPES.items()]

        for key, entity_type in keys_to_scan:
            for hit in results.get(key) or []:
                items.append(
                    {
                        "server": server_id,
                        "type": entity_type,
                        "id": hit.get("id"),
                        "name": hit.get("name"),
                        "groupId": hit.get("groupId"),
                        "path": hit.get("groupName") or hit.get("path"),
                    }
                )
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True,
            items=limited,
            total=total,
            returned=len(limited),
            truncated=truncated,
            query=query,
            component_type=component_type,
        )

    async def get_status(
        self,
        server_id: str,
        *,
        recursive: bool = True,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = ("status", str(recursive))
        if not refresh:
            cached = await self.cache.get("status", server_id, *cache_key)
            if cached is not None:
                return cached
        client = self._client(server_id)
        data = await client.get_json(
            "/flow/process-groups/root/status",
            params={"recursive": str(recursive).lower()},
        )
        status = data.get("processGroupStatus") or data
        payload = wrap_result(ok=True, status=status, returned=1)
        await self.cache.set(
            "status",
            server_id,
            *cache_key,
            value=payload,
            ttl_seconds=self.config.global_config.cache.status_ttl_seconds,
        )
        return payload

    async def get_process_group_status(
        self,
        server_id: str,
        process_group_id: str,
        *,
        recursive: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        del refresh
        client = self._client(server_id)
        pg = process_group_id if process_group_id not in (ROOT_PG_ID, "root") else "root"
        data = await client.get_json(
            f"/flow/process-groups/{pg}/status",
            params={"recursive": str(recursive).lower()},
        )
        return wrap_result(ok=True, status=data.get("processGroupStatus") or data)

    async def get_processor_status(self, server_id: str, processor_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json(f"/flow/processors/{processor_id}/status")
        return wrap_result(ok=True, status=data.get("processorStatus") or data)

    async def get_connection_status(self, server_id: str, connection_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json(f"/flow/connections/{connection_id}/status")
        return wrap_result(ok=True, status=data.get("connectionStatus") or data)

    async def get_bulletins(
        self,
        server_id: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        client = self._client(server_id)
        data = await client.get_json("/flow/bulletin-board", params={"limit": eff_limit})
        board = data.get("bulletinBoard") or data
        bulletins = board.get("bulletins") or []
        items = [normalize_bulletin(server_id, b.get("bulletin") or b) for b in bulletins]
        return wrap_result(ok=True, items=items, total=len(items), returned=len(items))

    async def list_controller_services(
        self,
        server_id: str,
        *,
        process_group_id: str = ROOT_PG_ID,
        include_details: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        client = self._client(server_id)
        pg = process_group_id if process_group_id not in (ROOT_PG_ID, "root") else "root"
        data = await client.get_json(f"/flow/process-groups/{pg}/controller-services")
        services = data.get("controllerServices") or []
        items = [
            normalize_controller_service(server_id, s, include_details=include_details)
            for s in services
        ]
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def list_parameter_contexts(
        self,
        server_id: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        client = self._client(server_id)
        data = await client.get_json("/flow/parameter-contexts")
        contexts = data.get("parameterContexts") or []
        items = [normalize_parameter_context(server_id, c) for c in contexts]
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def processor_status_history(
        self,
        server_id: str,
        processor_id: str,
        *,
        max_points: int = 30,
    ) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json(f"/flow/processors/{processor_id}/status/history")
        return wrap_result(
            ok=True, history=compact_status_history(data.get("statusHistory") or data, max_points)
        )

    async def process_group_status_history(
        self,
        server_id: str,
        process_group_id: str,
        *,
        max_points: int = 30,
    ) -> dict[str, Any]:
        client = self._client(server_id)
        pg = process_group_id if process_group_id not in (ROOT_PG_ID, "root") else "root"
        data = await client.get_json(f"/flow/process-groups/{pg}/status/history")
        return wrap_result(
            ok=True, history=compact_status_history(data.get("statusHistory") or data, max_points)
        )

    async def debug_get(
        self,
        server_id: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.debug_get_json(endpoint, params=params)
        if isinstance(data, dict):
            data = redact_mapping(data)
        return wrap_result(ok=True, data=data)

    async def _collect_connections_recursive(
        self,
        server_id: str,
        *,
        process_group_id: str | None = None,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = ("all_connections", process_group_id or "all")
        if not refresh:
            cached = await self.cache.get("inventory", server_id, *cache_key)
            if cached is not None:
                return cached

        items: list[dict[str, Any]] = []
        visited: set[str] = set()
        scope_pg = (
            None
            if process_group_id in (None, ROOT_PG_ID, "root")
            else process_group_id
        )

        async def walk(pg_id: str, ancestor_names: list[str], *, in_subtree: bool) -> None:
            norm_id = ROOT_PG_ID if pg_id in (ROOT_PG_ID, "root") else pg_id
            if norm_id in visited:
                return
            visited.add(norm_id)
            if scope_pg is not None and norm_id == scope_pg:
                in_subtree = True
            data = await self._fetch_pg_flow(server_id, norm_id)
            names = extract_breadcrumb_names(data) or ancestor_names
            pg_flow = data.get("processGroupFlow") or {}
            flow = pg_flow.get("flow") or {}
            parent_path = build_breadcrumb(names[:-1] if names else [], include_self=names[-1] if names else None)
            if scope_pg is None or in_subtree:
                for raw in flow.get("connections") or []:
                    comp = raw.get("component") or raw
                    path = build_breadcrumb(names, include_self=comp.get("name", ""))
                    items.append(
                        normalize_connection(
                            server_id,
                            raw,
                            path=path,
                            parent_path=parent_path,
                        )
                    )
            for child in flow.get("processGroups") or []:
                comp = child.get("component") or child
                child_id = comp.get("id")
                child_name = comp.get("name")
                if child_id:
                    await walk(
                        child_id,
                        names + ([child_name] if child_name else []),
                        in_subtree=in_subtree,
                    )

        start = scope_pg or ROOT_PG_ID
        await walk(start, [], in_subtree=scope_pg is None)
        await self.cache.set(
            "inventory",
            server_id,
            *cache_key,
            value=items,
            ttl_seconds=self.config.global_config.cache.inventory_ttl_seconds,
        )
        return items

    async def list_connections(
        self,
        server_id: str,
        *,
        process_group_id: str | None = None,
        min_queued: int = 0,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        connections = await self._collect_connections_recursive(
            server_id, process_group_id=process_group_id, refresh=refresh
        )
        status_payload = await self.get_status(server_id, recursive=True, refresh=refresh)
        root = status_payload.get("status") or {}
        status_by_id = _index_connection_statuses(root if isinstance(root, dict) else {})

        items: list[dict[str, Any]] = []
        for conn in connections:
            cid = conn.get("id")
            snap = status_by_id.get(cid or "")
            if snap:
                agg = snap.get("aggregateSnapshot") or snap
                conn = dict(conn)
                conn["queue"] = {
                    "flowFilesQueued": int(agg.get("flowFilesQueued") or 0),
                    "bytesQueued": int(agg.get("bytesQueued") or 0),
                    "percentUseCount": agg.get("percentUseCount"),
                    "percentUseBytes": agg.get("percentUseBytes"),
                }
            queue = conn.get("queue") or {}
            ff = int(queue.get("flowFilesQueued") or 0)
            if ff < min_queued:
                continue
            items.append(conn)

        def _queue_sort_key(c: dict[str, Any]) -> tuple[int, int]:
            q = c.get("queue") or {}
            return (
                -int(q.get("flowFilesQueued") or 0),
                -int(q.get("bytesQueued") or 0),
            )

        items.sort(key=_queue_sort_key)
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def _poll_provenance(self, client: NiFiClient, provenance_id: str) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for _ in range(_PROVENANCE_POLL_ATTEMPTS):
            data = await client.get_json(f"/provenance/{provenance_id}")
            prov = data.get("provenance") if isinstance(data, dict) else {}
            if isinstance(prov, dict):
                last = prov
                if prov.get("finished") is True:
                    return prov
            await asyncio.sleep(_PROVENANCE_POLL_SECONDS)
        return last

    async def get_provenance(
        self,
        server_id: str,
        *,
        component_id: str | None = None,
        max_results: int = 100,
        since_minutes: int = 60,
    ) -> dict[str, Any]:
        client = self._client(server_id)
        max_results = max(1, min(int(max_results), self.config.global_config.max_limit))
        since_minutes = max(1, int(since_minutes))
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=since_minutes)
        search_terms: dict[str, Any] = {}
        if component_id:
            search_terms["Component ID"] = {"value": component_id, "inverse": False}
        body = {
            "provenance": {
                "request": {
                    "maxResults": max_results,
                    "summarize": True,
                    "incrementalResults": False,
                    "searchTerms": search_terms,
                    "startDate": _nifi_provenance_timestamp(start),
                    "endDate": _nifi_provenance_timestamp(end),
                }
            }
        }
        created = await client.post_json_read_only("/provenance", body)
        prov_id = (created.get("provenance") or created).get("id")
        if not prov_id:
            return wrap_result(ok=False, error="NiFi no devolvió id de consulta provenance")
        try:
            prov = await self._poll_provenance(client, str(prov_id))
            results_block = prov.get("results")
            if isinstance(results_block, dict):
                events = results_block.get("provenanceEvents") or []
            else:
                events = prov.get("provenanceEvents") or []
            normalized = [redact_mapping(e) for e in events if isinstance(e, dict)]
            limited, truncated = apply_limit(normalized, max_results)
            return wrap_result(
                ok=True,
                items=limited,
                total=len(normalized),
                returned=len(limited),
                truncated=truncated,
                query={
                    "component_id": component_id,
                    "since_minutes": since_minutes,
                    "max_results": max_results,
                },
                provenance_query_id=prov_id,
                note="Consulta read-only: POST/GET/DELETE /provenance (no modifica el flow).",
            )
        finally:
            try:
                await client.delete_json_read_only(f"/provenance/{prov_id}")
            except Exception as exc:
                logger.warning("No se pudo limpiar provenance %s: %s", prov_id, exc)

    async def _poll_queue_listing(
        self, client: NiFiClient, connection_id: str, request_id: str
    ) -> dict[str, Any]:
        path = f"/flowfile-queues/{connection_id}/listing-requests/{request_id}"
        last: dict[str, Any] = {}
        for _ in range(_QUEUE_LISTING_POLL_ATTEMPTS):
            data = await client.get_json(path)
            listing = data.get("listingRequest") if isinstance(data, dict) else {}
            if isinstance(listing, dict):
                last = listing
                if listing.get("finished") is True:
                    return listing
            await asyncio.sleep(_QUEUE_LISTING_POLL_SECONDS)
        return last

    async def list_queue(
        self,
        server_id: str,
        connection_id: str,
        *,
        max_results: int = 100,
    ) -> dict[str, Any]:
        client = self._client(server_id)
        max_results = max(1, min(int(max_results), self.config.global_config.max_limit))
        created = await client.post_json_read_only(
            f"/flowfile-queues/{connection_id}/listing-requests", {}
        )
        req = created.get("listingRequest") or created
        request_id = req.get("id") if isinstance(req, dict) else None
        if not request_id:
            return wrap_result(ok=False, error="NiFi no devolvió id de listing request")
        delete_path = f"/flowfile-queues/{connection_id}/listing-requests/{request_id}"
        try:
            listing = await self._poll_queue_listing(client, connection_id, str(request_id))
            summaries = listing.get("flowFileSummaries") or []
            items = [redact_mapping(s) for s in summaries if isinstance(s, dict)]
            limited, truncated = apply_limit(items, max_results)
            return wrap_result(
                ok=True,
                items=limited,
                total=len(items),
                returned=len(limited),
                truncated=truncated,
                connection_id=connection_id,
                listing_request_id=request_id,
                note="Listado read-only: POST/GET/DELETE listing-requests (no borra flowfiles).",
            )
        finally:
            try:
                await client.delete_json_read_only(delete_path)
            except Exception as exc:
                logger.warning("No se pudo limpiar listing request %s: %s", request_id, exc)

    async def cluster_status(self, server_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        summary = await client.get_cluster_summary()
        cluster = await client.get_json_optional("/controller/cluster")
        nodes: list[dict[str, Any]] = []
        connected = None
        if isinstance(summary, dict):
            cluster_info = summary.get("cluster") or summary
            connected = cluster_info.get("connectedToCluster")
        if isinstance(cluster, dict):
            cluster_body = cluster.get("cluster")
            if isinstance(cluster_body, dict):
                node_list = cluster_body.get("nodes") or []
            else:
                node_list = cluster.get("nodes") or []
            for node in node_list:
                if not isinstance(node, dict):
                    continue
                nodes.append(
                    {
                        "nodeId": node.get("nodeId") or node.get("id"),
                        "address": node.get("address"),
                        "apiPort": node.get("apiPort"),
                        "status": node.get("status") or node.get("nodeStatus"),
                        "roles": node.get("roles"),
                        "activeThreadCount": node.get("activeThreadCount"),
                        "queued": node.get("queued"),
                    }
                )
        return wrap_result(
            ok=True,
            connected_to_cluster=connected,
            node_count=len(nodes),
            nodes=nodes,
            cluster_summary=redact_mapping(summary) if isinstance(summary, dict) else summary,
        )

    async def get_controller_service(self, server_id: str, service_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json(f"/controller-services/{service_id}")
        refs = await client.get_json_optional(f"/controller-services/{service_id}/references")
        svc = normalize_controller_service(server_id, data, include_details=True)
        payload = wrap_result(ok=True, items=[svc], total=1, returned=1)
        if refs is not None:
            payload["references"] = redact_mapping(refs)
        return payload

    async def flow_versions(
        self,
        server_id: str,
        *,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        items = await self._flow_versions_from_inventory(server_id, refresh=refresh)
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def _flow_versions_from_inventory(
        self, server_id: str, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        visited: set[str] = set()

        async def walk(pg_id: str, ancestor_names: list[str]) -> None:
            norm_id = ROOT_PG_ID if pg_id in (ROOT_PG_ID, "root") else pg_id
            if norm_id in visited:
                return
            visited.add(norm_id)
            data = await self._fetch_pg_flow(server_id, norm_id)
            names = extract_breadcrumb_names(data) or ancestor_names
            pg_flow = data.get("processGroupFlow") or {}
            flow = pg_flow.get("flow") or {}
            comp = pg_flow.get("component") or pg_flow
            vci = comp.get("versionControlInformation")
            if vci:
                items.append(
                    {
                        "server": server_id,
                        "id": comp.get("id") or norm_id,
                        "name": comp.get("name") or (names[-1] if names else ROOT_LABEL),
                        "path": build_breadcrumb(names),
                        "version_state": vci.get("state"),
                        "registry_id": vci.get("registryId"),
                        "registry_name": vci.get("registryName"),
                        "bucket_id": vci.get("bucketId"),
                        "bucket_name": vci.get("bucketName"),
                        "flow_id": vci.get("flowId"),
                        "flow_name": vci.get("flowName"),
                        "version": vci.get("version"),
                    }
                )
            for child in flow.get("processGroups") or []:
                child_comp = child.get("component") or child
                child_name = child_comp.get("name", "")
                vci_child = child_comp.get("versionControlInformation")
                if vci_child:
                    items.append(
                        {
                            "server": server_id,
                            "id": child_comp.get("id"),
                            "name": child_name,
                            "path": build_breadcrumb(names, include_self=child_name),
                            "version_state": vci_child.get("state"),
                            "registry_id": vci_child.get("registryId"),
                            "registry_name": vci_child.get("registryName"),
                            "bucket_id": vci_child.get("bucketId"),
                            "bucket_name": vci_child.get("bucketName"),
                            "flow_id": vci_child.get("flowId"),
                            "flow_name": vci_child.get("flowName"),
                            "version": vci_child.get("version"),
                        }
                    )
                child_id = child_comp.get("id")
                if child_id:
                    await walk(child_id, names + ([child_name] if child_name else []))

        await walk(ROOT_PG_ID, [])
        return items

    async def get_parameter_context(self, server_id: str, context_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json(f"/parameter-contexts/{context_id}")
        ctx = normalize_parameter_context(server_id, data)
        return wrap_result(ok=True, items=[ctx], total=1, returned=1)

    async def system_diagnostics(self, server_id: str) -> dict[str, Any]:
        client = self._client(server_id)
        data = await client.get_json("/system-diagnostics")
        diag = data.get("systemDiagnostics") or data
        if isinstance(diag, dict):
            diag = redact_mapping(diag)
        return wrap_result(ok=True, diagnostics=diag)
