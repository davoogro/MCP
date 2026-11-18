"""Diagnósticos y chequeos de salud configurables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nifi_readonly_mcp.models import HealthConfig
from nifi_readonly_mcp.normalize import apply_limit, wrap_result
from nifi_readonly_mcp.responses import validate_limit
from nifi_readonly_mcp.services import NiFiService

# Abreviaturas de zona que NiFi usa en BulletinDTO.timestamp ("HH:mm:ss z").
_TZ_OFFSETS_H: dict[str, float] = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "COT": -5,
    "PET": -5,
    "ECT": -5,
    "EST": -5,
    "CDT": -5,
    "CST": -6,
    "MDT": -6,
    "MST": -7,
    "PDT": -7,
    "PST": -8,
    "EDT": -4,
    "AST": -4,
    "VET": -4,
    "BOT": -4,
    "CLT": -4,
    "CLST": -3,
    "ART": -3,
    "BRT": -3,
    "UYT": -3,
    "CET": 1,
    "CEST": 2,
}


def _parse_ts(value: str | None, now: datetime | None = None) -> datetime | None:
    """Parsea timestamps de bulletins: ISO-8601 o el formato de NiFi "HH:mm:ss TZ".

    NiFi 1.x/2.x devuelve solo la hora con abreviatura de zona (p.ej. "17:09:45 COT").
    Se asume el día actual en esa zona (o el anterior si quedaría en el futuro).
    """
    if not value:
        return None
    text = value.strip()
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    parts = text.split()
    if len(parts) != 2 or parts[1].upper() not in _TZ_OFFSETS_H:
        return None
    try:
        t = datetime.strptime(parts[0], "%H:%M:%S").time()
    except ValueError:
        return None
    tz = timezone(timedelta(hours=_TZ_OFFSETS_H[parts[1].upper()]))
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    candidate = datetime.combine(now_local.date(), t, tzinfo=tz)
    if candidate > now_local + timedelta(minutes=5):
        candidate -= timedelta(days=1)
    return candidate


def _without_properties(item: dict[str, Any]) -> dict[str, Any]:
    """Copia del componente sin el mapa de propiedades (pesado y poco útil para diagnóstico)."""
    details = item.get("details")
    if not isinstance(details, dict) or "properties" not in details:
        return item
    out = dict(item)
    out["details"] = {k: v for k, v in details.items() if k != "properties"}
    return out


def _severity_rank(sev: str) -> int:
    return {"informational": 0, "warning": 1, "critical": 2}.get(sev, 0)


_MAX_STOPPED_FINDINGS = 20


class DiagnosticsService:
    def __init__(self, service: NiFiService) -> None:
        self.service = service
        self.health_cfg: HealthConfig = service.config.global_config.health

    def _effective_limit(self, limit: int | None) -> int:
        g = self.service.config.global_config
        return validate_limit(limit, default=g.default_limit, max_limit=g.max_limit)

    async def health_check(self, server_id: str, *, refresh: bool = False) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        overall = "healthy"
        try:
            conn = await self.service.test_connection(server_id)
            if not conn.get("ok"):
                return wrap_result(
                    ok=False,
                    server_id=server_id,
                    overall_status="unreachable",
                    health="unreachable",
                    findings=[{"severity": "critical", "category": "connectivity", "message": conn.get("error", "unreachable")}],
                )

            cluster = conn.get("cluster") or {}
            cluster_nodes = (cluster.get("cluster") or cluster).get("nodes") or []
            for node in cluster_nodes:
                status = (node.get("status") or node.get("nodeStatus") or "").upper()
                if status in ("DISCONNECTED", "OFFLOADING", "OFFLOADED", "CONNECTING"):
                    findings.append(
                        {
                            "severity": "critical",
                            "category": "cluster_node",
                            "message": f"node {node.get('address') or node.get('nodeId')} status={status}",
                        }
                    )

            # recursive=False basta: aggregateSnapshot del root ya totaliza todo el flow.
            status = await self.service.get_status(server_id, recursive=False, refresh=refresh)
            root_status = status.get("status") or {}
            agg = (
                (root_status.get("aggregateSnapshot") or root_status)
                if isinstance(root_status, dict)
                else {}
            )
            active_threads = int(agg.get("activeThreadCount") or 0)
            if (
                self.health_cfg.active_threads_warning is not None
                and active_threads >= self.health_cfg.active_threads_warning
            ):
                findings.append(
                    {
                        "severity": "warning",
                        "category": "threads",
                        "message": f"active_threads={active_threads}",
                    }
                )

            stopped_pg_count = int(agg.get("stoppedCount") or 0)
            if stopped_pg_count:
                findings.append(
                    {
                        "severity": "informational",
                        "category": "process_group_stopped",
                        "message": f"stopped_process_group_components={stopped_pg_count}",
                    }
                )

            bulletins = await self.service.get_bulletins(server_id, limit=200)
            now = datetime.now(timezone.utc)
            for b in bulletins.get("items") or []:
                ts = _parse_ts(b.get("timestamp"), now)
                # Sin timestamp interpretable se asume reciente (NiFi solo retiene ~5 min).
                age_min = (
                    max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds() / 60)
                    if ts
                    else None
                )
                if age_min is not None and age_min > self.health_cfg.bulletin_age_minutes:
                    continue
                level = (b.get("level") or "").upper()
                if level == "ERROR":
                    sev = "critical"
                elif level in ("WARN", "WARNING"):
                    sev = "warning"
                else:
                    continue
                findings.append(
                    {
                        "severity": sev,
                        "category": "bulletin",
                        "message": b.get("message"),
                        "source": b.get("sourceName"),
                        "age_minutes": round(age_min, 1) if age_min is not None else None,
                    }
                )

            stopped = await self.find_stopped_processors(
                server_id, refresh=refresh, limit=_MAX_STOPPED_FINDINGS
            )
            for item in stopped.get("items") or []:
                findings.append(
                    {
                        "severity": self.health_cfg.stopped_processor_severity,
                        "category": "stopped_processor",
                        "message": f"{item.get('name')} is STOPPED",
                        "id": item.get("id"),
                        "path": item.get("path"),
                    }
                )
            stopped_total = int(stopped.get("total") or 0)
            if stopped_total > len(stopped.get("items") or []):
                findings.append(
                    {
                        "severity": self.health_cfg.stopped_processor_severity,
                        "category": "stopped_processor",
                        "message": (
                            f"{stopped_total} procesadores STOPPED en total (se listan "
                            f"{_MAX_STOPPED_FINDINGS}); ver nifi_find_stopped_processors"
                        ),
                    }
                )

            invalid = await self.find_invalid_components(server_id, refresh=refresh)
            for item in invalid.get("items") or []:
                state = (item.get("state") or "").upper()
                sev = "warning" if state == "DISABLED" else "critical"
                findings.append(
                    {
                        "severity": sev,
                        "category": (
                            "disabled_component" if state == "DISABLED" else "invalid_component"
                        ),
                        "message": f"{item.get('type')} {item.get('name')} {item.get('state') or 'invalid'}",
                        "id": item.get("id"),
                    }
                )

            queues = await self.find_large_queues(server_id, refresh=refresh)
            for item in queues.get("items") or []:
                findings.append(
                    {
                        "severity": item.get("severity", "warning"),
                        "category": "queue",
                        "message": item.get("message"),
                        "id": item.get("id"),
                    }
                )
                if item.get("backpressure"):
                    findings.append(
                        {
                            "severity": "warning",
                            "category": "backpressure",
                            "message": item.get("backpressure"),
                            "id": item.get("id"),
                        }
                    )

            for issue in findings:
                if _severity_rank(issue.get("severity", "")) >= _severity_rank("critical"):
                    overall = "critical"
                elif (
                    _severity_rank(issue.get("severity", "")) >= _severity_rank("warning")
                    and overall != "critical"
                ):
                    overall = "warning"

            return wrap_result(
                ok=True,
                server_id=server_id,
                overall_status=overall,
                health=overall,
                findings=findings,
                issue_count=len(findings),
                nifi_version=conn.get("nifi_version"),
            )
        except Exception as exc:
            return wrap_result(
                ok=False,
                server_id=server_id,
                overall_status="error",
                health="error",
                findings=[{"severity": "critical", "category": "error", "message": str(exc)}],
            )

    async def _all_controller_services(self, server_id: str) -> list[dict[str, Any]]:
        services = await self.service.list_controller_services(
            server_id,
            include_details=True,
            limit=self.service.config.global_config.max_limit,
        )
        return services.get("items") or []

    async def find_stopped_processors(
        self,
        server_id: str,
        *,
        refresh: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        # Todos los procesadores (no solo los primeros default_limit); sin details: no hacen falta.
        procs = await self.service.all_processors(server_id, refresh=refresh)
        items = [p for p in procs if (p.get("state") or "").upper() == "STOPPED"]
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=len(items), returned=len(limited), truncated=truncated
        )

    async def find_invalid_components(
        self,
        server_id: str,
        *,
        refresh: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        items: list[dict[str, Any]] = []
        procs = await self.service.all_processors(server_id, include_details=True, refresh=refresh)
        for p in procs:
            state = (p.get("state") or "").upper()
            if state in ("INVALID", "DISABLED") or any(
                str(i).startswith("validation:") for i in p.get("issues") or []
            ):
                items.append(_without_properties(p))
        for s in await self._all_controller_services(server_id):
            if (s.get("state") or "").upper() in ("INVALID", "DISABLED") or any(
                str(i).startswith("validation:") for i in s.get("issues") or []
            ):
                items.append(_without_properties(s))
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=len(items), returned=len(limited), truncated=truncated
        )

    async def find_large_queues(
        self,
        server_id: str,
        *,
        min_flowfiles: int | None = None,
        min_bytes: int | None = None,
        limit: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        ff_warn = min_flowfiles if min_flowfiles is not None else self.health_cfg.queue_flowfiles_warning
        ff_crit = min_flowfiles if min_flowfiles is not None else self.health_cfg.queue_flowfiles_critical
        byt_warn = min_bytes if min_bytes is not None else self.health_cfg.queue_bytes_warning
        byt_crit = min_bytes if min_bytes is not None else self.health_cfg.queue_bytes_critical

        status = await self.service.get_status(server_id, recursive=True, refresh=refresh)
        items: list[dict[str, Any]] = []
        root = status.get("status") or {}

        def walk(pg_status: dict[str, Any]) -> None:
            for conn in pg_status.get("connectionStatusSnapshots") or pg_status.get("connections") or []:
                snap = conn.get("connectionStatusSnapshot") or conn
                agg = snap.get("aggregateSnapshot") or snap
                ff = int(agg.get("flowFilesQueued") or 0)
                byt = int(agg.get("bytesQueued") or 0)
                sev = None
                msg = None
                if ff >= ff_crit or byt >= byt_crit:
                    sev = "critical"
                    msg = f"queue flowfiles={ff} bytes={byt}"
                elif ff >= ff_warn or byt >= byt_warn:
                    sev = "warning"
                    msg = f"queue flowfiles={ff} bytes={byt}"
                if sev:
                    comp = conn.get("connection") or conn
                    entry: dict[str, Any] = {
                        "server": server_id,
                        "id": comp.get("id"),
                        "name": comp.get("name"),
                        "severity": sev,
                        "message": msg,
                        "flowFilesQueued": ff,
                        "bytesQueued": byt,
                    }
                    pct = agg.get("percentUseCount") or agg.get("percentUseBytes")
                    if pct and int(str(pct).replace("%", "") or 0) >= 90:
                        entry["backpressure"] = f"backpressure ~{pct}%"
                    items.append(entry)
            for child in (
                pg_status.get("processGroupStatusSnapshots")
                or pg_status.get("childProcessGroupStatusSnapshots")
                or []
            ):
                child_snap = child.get("processGroupStatusSnapshot") or child
                walk(child_snap)

        if isinstance(root, dict):
            walk(root)
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def find_processors(
        self,
        server_id: str,
        *,
        processor_type: str | None = None,
        name: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        refresh: bool = False,
        include_details: bool = False,
    ) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit)
        items = await self.service.all_processors(
            server_id, include_details=include_details, refresh=refresh
        )
        if processor_type:
            needle = processor_type.lower().rsplit(".", 1)[-1]
            items = [
                p
                for p in items
                if needle
                in str(p.get("component_type") or (p.get("details") or {}).get("type", "")).lower()
            ]
        if name:
            needle = name.lower()
            items = [p for p in items if needle in str(p.get("name", "")).lower()]
        if state:
            st = state.upper()
            items = [p for p in items if (p.get("state") or "").upper() == st]
        total = len(items)
        limited, truncated = apply_limit(items, eff_limit)
        return wrap_result(
            ok=True, items=limited, total=total, returned=len(limited), truncated=truncated
        )

    async def find_problem_controller_services(self, server_id: str) -> dict[str, Any]:
        items = [
            _without_properties(s)
            for s in await self._all_controller_services(server_id)
            if (s.get("state") or "").upper() in ("DISABLED", "INVALID")
            or any(str(i).startswith("validation:") for i in s.get("issues") or [])
        ]
        return wrap_result(ok=True, items=items, total=len(items), returned=len(items))

    async def get_errors(self, server_id: str, *, limit: int | None = None) -> dict[str, Any]:
        eff_limit = self._effective_limit(limit if limit is not None else 100)
        bulletins = await self.service.get_bulletins(server_id, limit=eff_limit)
        items = [
            b
            for b in bulletins.get("items") or []
            if (b.get("level") or "").upper() in ("ERROR", "WARN", "WARNING")
        ]
        return wrap_result(ok=True, items=items, total=len(items), returned=len(items))
