"""Normalización de entidades NiFi para consumo LLM."""

from __future__ import annotations

from typing import Any

from nifi_mcp.auth import redact_mapping

ROOT_LABEL = "Root"


def _entity_base(
    server_id: str,
    entity_id: str,
    name: str,
    entity_type: str,
    *,
    path: str | None = None,
    parent_path: str | None = None,
    state: str | None = None,
    status: dict[str, Any] | None = None,
    issues: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "server": server_id,
        "id": entity_id,
        "name": name,
        "type": entity_type,
    }
    if path is not None:
        item["path"] = path
    if parent_path is not None:
        item["parent_path"] = parent_path
    if state is not None:
        item["state"] = state
    if status is not None:
        item["status"] = status
    if issues:
        item["issues"] = issues
    if details is not None:
        item["details"] = redact_mapping(details)
    return item


def extract_breadcrumb_names(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    bc = data.get("breadcrumb") or {}
    if isinstance(bc, dict):
        inner = bc.get("breadcrumb") or bc
        if isinstance(inner, dict):
            for ancestor in inner.get("ancestors") or []:
                n = ancestor.get("name")
                if n:
                    names.append(n)
            current = inner.get("name") or bc.get("name")
            if current and (not names or names[-1] != current):
                names.append(current)
    pg_flow = data.get("processGroupFlow") or {}
    if not names:
        pg_bc = pg_flow.get("breadcrumb") or {}
        if isinstance(pg_bc, dict) and pg_bc.get("name"):
            names.append(pg_bc["name"])
    return names


def build_breadcrumb(names: list[str], *, include_self: str | None = None) -> str:
    parts = list(names)
    if include_self and (not parts or parts[-1] != include_self):
        parts.append(include_self)
    if not parts:
        return ROOT_LABEL
    return ROOT_LABEL + " / " + " / ".join(parts)


def normalize_process_group(
    server_id: str,
    pg: dict[str, Any],
    *,
    path: str | None = None,
    parent_path: str | None = None,
    include_details: bool = False,
) -> dict[str, Any]:
    comp = pg.get("component") or pg
    cid = comp.get("id") or pg.get("id") or ""
    name = comp.get("name") or pg.get("name") or cid
    state = (comp.get("state") or pg.get("state") or "").upper() or None
    issues: list[str] = []
    if state and state not in ("RUNNING", "ENABLED"):
        issues.append(f"process_group_state:{state}")
    details = None
    if include_details:
        details = {
            k: v
            for k, v in comp.items()
            if k not in ("id", "name", "state", "parentGroupId", "contents")
        }
    return _entity_base(
        server_id,
        cid,
        name,
        "process_group",
        path=path or build_breadcrumb([name]),
        parent_path=parent_path or ROOT_LABEL,
        state=state,
        issues=issues,
        details=details,
    )


def normalize_processor(
    server_id: str,
    proc: dict[str, Any],
    *,
    path: str | None = None,
    parent_path: str | None = None,
    include_details: bool = False,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comp = proc.get("component") or proc
    cid = comp.get("id") or proc.get("id") or ""
    name = comp.get("name") or proc.get("name") or cid
    state = (comp.get("state") or proc.get("state") or "").upper() or None
    proc_type = comp.get("type") or proc.get("type") or "processor"
    issues: list[str] = []
    validation = (comp.get("validationStatus") or proc.get("validationStatus") or "").upper()
    if validation and validation != "VALID":
        issues.append(f"validation:{validation}")
    if state == "STOPPED":
        issues.append("stopped")
    elif state == "DISABLED":
        issues.append("disabled")
    elif state == "INVALID":
        issues.append("invalid")
    details = None
    if include_details:
        config = comp.get("config") or {}
        props = config.get("properties") or {}
        details = {
            "type": proc_type,
            "schedulingPeriod": config.get("schedulingPeriod"),
            "schedulingStrategy": config.get("schedulingStrategy"),
            "executionNode": config.get("executionNode"),
            "properties": redact_mapping(dict(props)),
            "validationErrors": comp.get("validationErrors") or [],
        }
    item = _entity_base(
        server_id,
        cid,
        name,
        "processor",
        path=path,
        parent_path=parent_path,
        state=state,
        status=status,
        issues=issues,
        details=details,
    )
    # Nombre corto de la clase (p.ej. "ExecuteSQL"): útil para filtrar sin pedir details.
    item["component_type"] = str(proc_type).rsplit(".", 1)[-1]
    return item


def normalize_controller_service(
    server_id: str,
    svc: dict[str, Any],
    *,
    path: str | None = None,
    parent_path: str | None = None,
    include_details: bool = False,
) -> dict[str, Any]:
    comp = svc.get("component") or svc
    cid = comp.get("id") or svc.get("id") or ""
    name = comp.get("name") or svc.get("name") or cid
    state = (comp.get("state") or svc.get("state") or "").upper() or None
    svc_type = comp.get("type") or svc.get("type") or "controller_service"
    issues: list[str] = []
    validation = (comp.get("validationStatus") or "").upper()
    if validation and validation != "VALID":
        issues.append(f"validation:{validation}")
    if state == "DISABLED":
        issues.append("disabled")
    elif state == "INVALID":
        issues.append("invalid")
    details = None
    if include_details:
        props = comp.get("properties") or {}
        details = {
            "type": svc_type,
            "properties": redact_mapping(dict(props)),
            "validationErrors": comp.get("validationErrors") or [],
        }
    return _entity_base(
        server_id,
        cid,
        name,
        "controller_service",
        path=path or name,
        parent_path=parent_path,
        state=state,
        issues=issues,
        details=details,
    )


def normalize_parameter_context(server_id: str, ctx: dict[str, Any]) -> dict[str, Any]:
    comp = ctx.get("component") or ctx
    cid = comp.get("id") or ctx.get("id") or ""
    name = comp.get("name") or ctx.get("name") or cid
    params = comp.get("parameters") or []
    redacted_params = [redact_mapping(p) for p in params if isinstance(p, dict)]
    return _entity_base(
        server_id,
        cid,
        name,
        "parameter_context",
        details={"parameters": redacted_params, "description": comp.get("description")},
    )


def _endpoint_label(endpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if not endpoint or not isinstance(endpoint, dict):
        return None
    return {
        "id": endpoint.get("id"),
        "name": endpoint.get("name"),
        "type": endpoint.get("type"),
        "groupId": endpoint.get("groupId"),
    }


def normalize_connection(
    server_id: str,
    conn: dict[str, Any],
    *,
    path: str | None = None,
    parent_path: str | None = None,
    queue_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comp = conn.get("component") or conn
    cid = comp.get("id") or conn.get("id") or ""
    name = comp.get("name") or conn.get("name") or cid
    item = _entity_base(
        server_id,
        cid,
        name,
        "connection",
        path=path,
        parent_path=parent_path,
        details={
            "source": _endpoint_label(comp.get("source")),
            "destination": _endpoint_label(comp.get("destination")),
            "selectedRelationships": comp.get("selectedRelationships") or [],
            "backPressureObjectThreshold": comp.get("backPressureObjectThreshold"),
            "backPressureDataSizeThreshold": comp.get("backPressureDataSizeThreshold"),
        },
    )
    if queue_status:
        agg = queue_status.get("aggregateSnapshot") or queue_status
        item["queue"] = {
            "flowFilesQueued": int(agg.get("flowFilesQueued") or 0),
            "bytesQueued": int(agg.get("bytesQueued") or 0),
            "percentUseCount": agg.get("percentUseCount"),
            "percentUseBytes": agg.get("percentUseBytes"),
        }
    return item


def normalize_bulletin(server_id: str, bulletin: dict[str, Any]) -> dict[str, Any]:
    return {
        "server": server_id,
        "id": bulletin.get("id"),
        "level": bulletin.get("level") or bulletin.get("bulletinLevel"),
        "message": bulletin.get("message"),
        "sourceType": bulletin.get("sourceType"),
        "sourceId": bulletin.get("sourceId"),
        "sourceName": bulletin.get("sourceName"),
        "timestamp": bulletin.get("timestamp"),
        "groupId": bulletin.get("groupId"),
    }


def apply_limit(items: list[Any], limit: int | None) -> tuple[list[Any], bool]:
    if limit is None:
        return items, False
    if len(items) <= limit:
        return items, False
    return items[:limit], True


def wrap_result(
    *,
    ok: bool = True,
    items: list[Any] | None = None,
    total: int | None = None,
    returned: int | None = None,
    truncated: bool = False,
    errors: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": ok}
    if items is not None:
        payload["items"] = items
    if total is not None:
        payload["total"] = total
    if returned is not None:
        payload["returned"] = returned
    if truncated:
        payload["truncated"] = True
    if errors:
        payload["errors"] = errors
    payload.update(extra)
    return payload


SEARCH_TYPE_MAP: dict[str, tuple[str, ...]] = {
    "processor": ("processorResults",),
    "process_group": ("processGroupResults",),
    "connection": ("connectionResults",),
    "controller_service": (
        "controllerServiceResults",
        "controllerServiceNodeResults",
    ),
    "input_port": ("inputPortResults",),
    "output_port": ("outputPortResults",),
    "remote_process_group": ("remoteProcessGroupResults",),
}

SEARCH_ENTITY_TYPES: dict[str, str] = {
    "processorResults": "processor",
    "processGroupResults": "process_group",
    "connectionResults": "connection",
    "controllerServiceResults": "controller_service",
    "controllerServiceNodeResults": "controller_service",
    "inputPortResults": "input_port",
    "outputPortResults": "output_port",
    "remoteProcessGroupResults": "remote_process_group",
}
