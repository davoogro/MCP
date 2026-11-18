"""Antigüedad de datos por prefijo (monitoreo de cargas)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from s3_mcp.ops import ToolError, _Scan, _scan_limit, client_for_bucket, iter_objects, resolve_bucket_prefix
from s3_mcp.util import as_utc, group_label, to_iso


def _normalize_prefixes(prefixes: Union[str, list[str], None]) -> list[str]:
    if prefixes is None:
        raise ToolError("Indica al menos un prefix.", "InvalidArgument")
    if isinstance(prefixes, str):
        parts = [part.strip() for part in prefixes.split(",") if part.strip()]
        return parts or [prefixes.strip()]
    items = [str(part).strip() for part in prefixes if str(part).strip()]
    if not items:
        raise ToolError("Indica al menos un prefix.", "InvalidArgument")
    return items


def freshness(
    prefixes: Union[str, list[str]],
    bucket: Optional[str] = None,
    group_by_depth: int = 1,
    max_objects: Optional[int] = None,
) -> dict[str, Any]:
    if int(group_by_depth) < 1:
        raise ToolError("group_by_depth debe ser >= 1.", "InvalidArgument")
    prefix_list = _normalize_prefixes(prefixes)
    name, _ = resolve_bucket_prefix(bucket, prefix_list[0])
    limit = _scan_limit(max_objects)
    client = client_for_bucket(name)
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    scan_truncated = False
    scanned_total = 0

    for base_prefix in prefix_list:
        _, resolved = resolve_bucket_prefix(bucket, base_prefix)
        if scanned_total >= limit:
            scan_truncated = True
            break
        scan = _Scan()
        for obj in iter_objects(client, name, resolved, limit - scanned_total, scan):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            label = group_label(key, resolved, int(group_by_depth))
            gkey = (resolved, label)
            moment = as_utc(obj.get("LastModified")) or datetime.fromtimestamp(0, timezone.utc)
            entry = groups.get(gkey)
            if entry is None:
                entry = {
                    "prefix": resolved,
                    "group": label,
                    "object_count": 0,
                    "objects_last_24h": 0,
                    "latest_key": key,
                    "latest_last_modified": moment,
                }
                groups[gkey] = entry
            entry["object_count"] += 1
            if moment >= cutoff_24h:
                entry["objects_last_24h"] += 1
            if moment >= entry["latest_last_modified"]:
                entry["latest_last_modified"] = moment
                entry["latest_key"] = key
        scanned_total += scan.scanned
        if scan.truncated:
            scan_truncated = True
            break

    rows: list[dict[str, Any]] = []
    for entry in sorted(groups.values(), key=lambda item: (item["prefix"], item["group"])):
        latest = entry["latest_last_modified"]
        age_hours = round((now - latest).total_seconds() / 3600.0, 2)
        rows.append({
            "prefix": entry["prefix"],
            "group": entry["group"],
            "latest_key": entry["latest_key"],
            "latest_last_modified": to_iso(latest),
            "age_hours": age_hours,
            "object_count": entry["object_count"],
            "objects_last_24h": entry["objects_last_24h"],
        })

    return {
        "bucket": name,
        "prefixes": prefix_list,
        "group_by_depth": int(group_by_depth),
        "groups": rows,
        "scanned": scanned_total,
        "scan_truncated": scan_truncated,
    }
