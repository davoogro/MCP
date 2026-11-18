"""Operaciones de solo lectura contra Amazon S3.

Ninguna función llama Put*, Delete*, Copy* ni Restore*, ni genera URLs prefirmadas.
Los errores de AWS se traducen en el servidor; aquí se lanzan ``ToolError`` o
``ClientError`` / ``BotoCoreError``.
"""

from __future__ import annotations

import csv
import fnmatch
import gzip
import heapq
import io
import json
import re
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from s3_mcp.config import get_settings, reload_settings, settings_generation
from s3_mcp.util import (
    as_utc,
    clean_etag,
    clip_cell,
    extension_of,
    group_label,
    human_size,
    looks_binary,
    parse_datetime,
    parse_s3_uri,
    parse_size,
    relative_name,
    to_iso,
)

BOTO_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=10,
    read_timeout=60,
)

# Bloque de las lecturas por rango. El footer de parquet y el primer row group
# piden trozos distintos; cachear solo el último bloque evita repetir el GET.
_S3_RANGE_BLOCK = 1024 * 1024
_COUNT_WORKERS = 4

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}
_CLIENTS: dict[tuple, Any] = {}
_REGIONS: dict[str, str] = {}
_LOCK = threading.Lock()


class ToolError(Exception):
    """Error de herramienta ya traducible a ``{"ok": false, ...}``."""

    def __init__(self, message: str, code: str = "InvalidArgument", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def error_code(exc: ClientError) -> str:
    return str((exc.response.get("Error") or {}).get("Code") or type(exc).__name__)


def error_message(exc: ClientError) -> str:
    message = (exc.response.get("Error") or {}).get("Message")
    return str(message or error_code(exc))


def is_soft_error(code: str) -> bool:
    """AccessDenied y ausencias de una feature (NoSuch*, NotFound), no de un bucket."""
    if code in {"NoSuchBucket"}:
        return False
    if code in {"AccessDenied", "AllAccessDisabled", "403", "Unauthorized", "AccessDeniedException"}:
        return True
    if code.startswith("NoSuch") or "NotFound" in code:
        return True
    return code in {"MethodNotAllowed", "NotImplemented", "UnsupportedOperation"}


def reset_clients() -> None:
    with _LOCK:
        _CLIENTS.clear()
        _REGIONS.clear()


def activate_settings():
    """Recarga la configuración y descarta clientes boto3 cacheados."""
    settings = reload_settings()
    reset_clients()
    return settings


def _new_client(service: str, region: str, *, with_endpoint: bool) -> Any:
    settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": region, "config": BOTO_CONFIG}
    if settings.access_key_id and settings.secret_access_key:
        kwargs["aws_access_key_id"] = settings.access_key_id
        kwargs["aws_secret_access_key"] = settings.secret_access_key
    if settings.session_token:
        kwargs["aws_session_token"] = settings.session_token
    if with_endpoint and settings.endpoint_url:
        kwargs["endpoint_url"] = settings.endpoint_url
    return boto3.client(service, **kwargs)


def client_for(region: Optional[str] = None) -> Any:
    settings = get_settings()
    region = region or settings.region
    key = (settings_generation(), "s3", region, settings.endpoint_url or "", settings.access_key_id)
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = _new_client("s3", region, with_endpoint=True)
            _CLIENTS[key] = client
        return client


def sts_client() -> Any:
    settings = get_settings()
    key = (settings_generation(), "sts", settings.region, settings.access_key_id)
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            client = _new_client("sts", settings.region, with_endpoint=False)
            _CLIENTS[key] = client
        return client


def region_from_response(response: Optional[dict]) -> Optional[str]:
    """Lee ``x-amz-bucket-region`` de una respuesta o de un error 301/403."""
    if not isinstance(response, dict):
        return None
    headers = ((response.get("ResponseMetadata") or {}).get("HTTPHeaders") or {})
    for key, value in headers.items():
        if key.lower() == "x-amz-bucket-region" and value:
            return str(value)
    for key in ("BucketRegion", "x-amz-bucket-region"):
        if response.get(key):
            return str(response[key])
    return None


def normalize_location(constraint: Optional[str]) -> str:
    if not constraint or constraint in {"US", "us-east-1"}:
        return "us-east-1"
    if constraint == "EU":
        return "eu-west-1"
    return constraint


def bucket_region(bucket: str) -> str:
    cached = _REGIONS.get(bucket)
    if cached:
        return cached
    settings = get_settings()
    client = client_for(settings.region)
    region = None
    try:
        region = region_from_response(client.head_bucket(Bucket=bucket))
    except ClientError as exc:
        region = region_from_response(exc.response)
    if not region:
        try:
            loc = client.get_bucket_location(Bucket=bucket)
            region = normalize_location(loc.get("LocationConstraint"))
        except ClientError:
            region = settings.region
    region = region or settings.region
    _REGIONS[bucket] = region
    return region


def client_for_bucket(bucket: str) -> Any:
    return client_for(bucket_region(bucket))


def _merge_uri(bucket: Optional[str], path: Optional[str]) -> tuple[Optional[str], str]:
    name = (bucket or "").strip() or None
    extra = None
    if name:
        uri_bucket, uri_path = parse_s3_uri(name)
        if uri_bucket:
            name = uri_bucket
            extra = uri_path
    current = path
    if current:
        uri_bucket, uri_path = parse_s3_uri(current)
        if uri_bucket:
            name = uri_bucket
            current = uri_path
            extra = None
    if extra:
        if current:
            left = extra.rstrip("/")
            right = current.lstrip("/")
            current = f"{left}/{right}" if left else right
        else:
            current = extra
    return name, current or ""


def _ensure_bucket(bucket: Optional[str]) -> str:
    settings = get_settings()
    name = bucket or settings.default_bucket
    if not name:
        raise ToolError(
            "Indica un bucket, una URI s3:// o configura S3_DEFAULT_BUCKET / S3_BUCKETS.",
            "BucketRequired",
        )
    if settings.allowlist_only and name not in settings.buckets:
        raise ToolError(
            f"El bucket '{name}' no está en S3_BUCKETS y S3_BUCKET_ALLOWLIST_ONLY está activo.",
            "BucketNotAllowed",
        )
    return name


def resolve_bucket_prefix(bucket: Optional[str], prefix: Optional[str]) -> tuple[str, str]:
    name, path = _merge_uri(bucket, prefix)
    return _ensure_bucket(name), path


def resolve_bucket_key(bucket: Optional[str], key: Optional[str]) -> tuple[str, str]:
    name, path = _merge_uri(bucket, key)
    if not path:
        raise ToolError("Indica la key del objeto (o una URI s3://bucket/key).", "InvalidArgument")
    return _ensure_bucket(name), path


def _folderish(key: str) -> bool:
    if not key or key.endswith("/"):
        return True
    return "." not in key.rsplit("/", 1)[-1]


def _missing_object(exc: ClientError, key: str) -> None:
    code = error_code(exc)
    if code not in _NOT_FOUND:
        raise exc
    extra: dict[str, Any] = {}
    if _folderish(key):
        prefix = key if key.endswith("/") else key + "/"
        extra["suggestion"] = f"Parece una carpeta. Usa list_objects con prefix='{prefix}'."
    raise ToolError(f"No existe el objeto '{key}'.", code, **extra) from exc


def _version_kwargs(version_id: Optional[str]) -> dict[str, str]:
    return {"VersionId": version_id} if version_id else {}


def _scan_limit(requested: Optional[int]) -> int:
    ceiling = get_settings().max_scan_objects
    if requested is None:
        return ceiling
    try:
        value = int(requested)
    except (TypeError, ValueError) as exc:
        raise ToolError("max_objects debe ser un entero.", "InvalidArgument") from exc
    if value < 1:
        raise ToolError("max_objects debe ser >= 1.", "InvalidArgument")
    return min(value, ceiling)


def _clamp(value: int, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{name} debe ser un entero.", "InvalidArgument") from exc
    return max(low, min(number, high))


def _call_soft(name: str, fn, unavailable: dict[str, str]) -> Any:
    try:
        return fn()
    except ClientError as exc:
        code = error_code(exc)
        if is_soft_error(code):
            unavailable[name] = code
            return None
        raise


class _Scan:
    def __init__(self) -> None:
        self.scanned = 0
        self.truncated = False


def iter_objects(client: Any, bucket: str, prefix: str, max_objects: int, scan: _Scan):
    """Recorre objetos sin guardarlos todos. Marca ``scan.truncated`` al llegar al tope."""
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            if scan.scanned >= max_objects:
                scan.truncated = True
                return
            scan.scanned += 1
            yield obj


def _summary(obj: dict, prefix: str = "") -> dict[str, Any]:
    key = obj["Key"]
    size = int(obj.get("Size") or 0)
    return {
        "key": key,
        "name": relative_name(key, prefix),
        "size": size,
        "size_human": human_size(size),
        "last_modified": to_iso(as_utc(obj.get("LastModified"))),
        "storage_class": obj.get("StorageClass") or "STANDARD",
        "etag": clean_etag(obj.get("ETag")),
    }


def _agg_rows(mapping: dict[str, list[int]], name_key: str, limit: Optional[int] = None) -> list[dict]:
    rows = [
        {name_key: name, "count": count, "bytes": total, "bytes_human": human_size(total)}
        for name, (count, total) in mapping.items()
    ]
    rows.sort(key=lambda row: (-row["bytes"], row[name_key]))
    if limit is not None:
        rows = rows[:limit]
    return rows


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_connection() -> dict[str, Any]:
    settings = get_settings()
    identity = None
    identity_error = None
    try:
        raw = sts_client().get_caller_identity()
        identity = {"account": raw.get("Account"), "arn": raw.get("Arn"), "user_id": raw.get("UserId")}
    except ClientError as exc:
        identity_error = {"error": error_message(exc), "error_code": error_code(exc)}
    except BotoCoreError as exc:
        identity_error = {"error": str(exc), "error_code": type(exc).__name__}

    list_allowed = False
    list_error = None
    try:
        client_for(settings.region).list_buckets()
        list_allowed = True
    except ClientError as exc:
        list_error = error_code(exc)
    except BotoCoreError as exc:
        list_error = type(exc).__name__

    buckets = []
    for name in settings.buckets:
        entry: dict[str, Any] = {"name": name, "accessible": False, "region": None, "error": None}
        try:
            region = bucket_region(name)
            entry["region"] = region
            client_for(region).head_bucket(Bucket=name)
            entry["accessible"] = True
        except ClientError as exc:
            entry["error"] = error_code(exc)
            entry["region"] = region_from_response(exc.response) or entry["region"]
        except BotoCoreError as exc:
            entry["error"] = type(exc).__name__
        buckets.append(entry)

    return {
        "identity": identity,
        "identity_error": identity_error,
        "access_key_masked": settings.masked_access_key(),
        "region": settings.region,
        "endpoint_url": settings.endpoint_url,
        "list_buckets_allowed": list_allowed,
        "list_buckets_error": list_error,
        "buckets": buckets,
    }


def list_buckets(include_region: bool = False) -> dict[str, Any]:
    settings = get_settings()
    account: dict[str, Any] = {}
    allowed = False
    list_error = None
    try:
        resp = client_for(settings.region).list_buckets()
        allowed = True
        for item in resp.get("Buckets") or []:
            account[item["Name"]] = item.get("CreationDate")
    except ClientError as exc:
        if not is_soft_error(error_code(exc)):
            raise
        list_error = error_code(exc)
    except BotoCoreError:
        raise

    names = list(dict.fromkeys([*settings.buckets, *account.keys()]))
    if settings.allowlist_only:
        names = [name for name in names if name in settings.buckets]
    rows = []
    for name in names:
        row: dict[str, Any] = {
            "name": name,
            "origin": "configured" if name in settings.buckets else "account",
            "creation_date": to_iso(as_utc(account.get(name))) if name in account else None,
        }
        if include_region:
            try:
                row["region"] = bucket_region(name)
            except (ClientError, BotoCoreError):
                row["region"] = None
        rows.append(row)
    return {
        "buckets": rows,
        "list_buckets_allowed": allowed,
        "list_buckets_error": list_error,
        "allowlist_only": settings.allowlist_only,
    }


def _versioning_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    return {"status": resp.get("Status"), "mfa_delete": resp.get("MFADelete")}


def _encryption_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    rules = []
    for rule in (resp.get("ServerSideEncryptionConfiguration") or {}).get("Rules") or []:
        default = rule.get("ApplyServerSideEncryptionByDefault") or {}
        rules.append({
            "algorithm": default.get("SSEAlgorithm"),
            "kms_key_id": default.get("KMSMasterKeyID"),
            "bucket_key_enabled": rule.get("BucketKeyEnabled"),
        })
    return {"rules": rules}


def _lifecycle_view(resp: Optional[dict]) -> Optional[list]:
    if resp is None:
        return None
    rules = []
    for rule in resp.get("Rules") or []:
        filt = rule.get("Filter") or {}
        prefix = filt.get("Prefix", rule.get("Prefix"))
        transitions = []
        for item in rule.get("Transitions") or []:
            transitions.append({
                "days": item.get("Days"),
                "date": to_iso(as_utc(item.get("Date"))) if item.get("Date") else None,
                "storage_class": item.get("StorageClass"),
            })
        expiration = rule.get("Expiration") or {}
        rules.append({
            "id": rule.get("ID"),
            "status": rule.get("Status"),
            "prefix": prefix,
            "transitions": transitions,
            "expiration_days": expiration.get("Days"),
            "expiration_date": to_iso(as_utc(expiration["Date"])) if expiration.get("Date") else None,
        })
    return rules


def _tag_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    return {item["Key"]: item["Value"] for item in resp.get("TagSet") or []}


def _public_access_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    cfg = resp.get("PublicAccessBlockConfiguration") or {}
    return {
        "block_public_acls": cfg.get("BlockPublicAcls"),
        "ignore_public_acls": cfg.get("IgnorePublicAcls"),
        "block_public_policy": cfg.get("BlockPublicPolicy"),
        "restrict_public_buckets": cfg.get("RestrictPublicBuckets"),
    }


def _policy_status_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    return {"is_public": (resp.get("PolicyStatus") or {}).get("IsPublic")}


def _logging_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    enabled = resp.get("LoggingEnabled")
    if not enabled:
        return None
    return {"target_bucket": enabled.get("TargetBucket"), "target_prefix": enabled.get("TargetPrefix")}


def _object_lock_view(resp: Optional[dict]) -> Optional[dict]:
    if resp is None:
        return None
    cfg = resp.get("ObjectLockConfiguration") or {}
    rule = (cfg.get("Rule") or {}).get("DefaultRetention") or {}
    if not cfg and not rule:
        return None
    return {
        "enabled": cfg.get("ObjectLockEnabled"),
        "mode": rule.get("Mode"),
        "days": rule.get("Days"),
        "years": rule.get("Years"),
    }


def get_bucket_info(bucket: str) -> dict[str, Any]:
    name, _path = resolve_bucket_prefix(bucket, "")
    client = client_for_bucket(name)
    unavailable: dict[str, str] = {}
    versioning = _call_soft("versioning", lambda: client.get_bucket_versioning(Bucket=name), unavailable)
    encryption = _call_soft("encryption", lambda: client.get_bucket_encryption(Bucket=name), unavailable)
    lifecycle = _call_soft(
        "lifecycle", lambda: client.get_bucket_lifecycle_configuration(Bucket=name), unavailable
    )
    tagging = _call_soft("tagging", lambda: client.get_bucket_tagging(Bucket=name), unavailable)
    public_access = _call_soft(
        "public_access_block", lambda: client.get_public_access_block(Bucket=name), unavailable
    )
    policy_status = _call_soft(
        "policy_status", lambda: client.get_bucket_policy_status(Bucket=name), unavailable
    )
    logging_cfg = _call_soft("logging", lambda: client.get_bucket_logging(Bucket=name), unavailable)
    object_lock = _call_soft(
        "object_lock", lambda: client.get_object_lock_configuration(Bucket=name), unavailable
    )
    return {
        "bucket": name,
        "region": bucket_region(name),
        "versioning": _versioning_view(versioning),
        "encryption": _encryption_view(encryption),
        "lifecycle": _lifecycle_view(lifecycle),
        "tagging": _tag_view(tagging),
        "public_access_block": _public_access_view(public_access),
        "policy_status": _policy_status_view(policy_status),
        "logging": _logging_view(logging_cfg),
        "object_lock": _object_lock_view(object_lock),
        "unavailable": unavailable,
    }


def list_objects(
    bucket: Optional[str] = None,
    prefix: str = "",
    delimiter: str = "/",
    max_keys: int = 200,
    continuation_token: Optional[str] = None,
    start_after: Optional[str] = None,
) -> dict[str, Any]:
    name, resolved_prefix = resolve_bucket_prefix(bucket, prefix)
    max_keys = _clamp(max_keys, 1, 1000, "max_keys")
    kwargs: dict[str, Any] = {"Bucket": name, "Prefix": resolved_prefix, "MaxKeys": max_keys}
    if delimiter:
        kwargs["Delimiter"] = delimiter
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token
    elif start_after:
        kwargs["StartAfter"] = start_after
    resp = client_for_bucket(name).list_objects_v2(**kwargs)
    files = [_summary(obj, resolved_prefix) for obj in resp.get("Contents") or []]
    folders = [item["Prefix"] for item in resp.get("CommonPrefixes") or []]
    page_size = sum(item["size"] for item in files)
    return {
        "bucket": name,
        "prefix": resolved_prefix,
        "delimiter": delimiter or "",
        "folders": folders,
        "files": files,
        "files_count": len(files),
        "page_size_total": page_size,
        "page_size_total_human": human_size(page_size),
        "is_truncated": bool(resp.get("IsTruncated")),
        "next_continuation_token": resp.get("NextContinuationToken"),
    }


def get_object_metadata(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    version_id: Optional[str] = None,
    include_tags: bool = True,
) -> dict[str, Any]:
    name, object_key = resolve_bucket_key(bucket, key)
    client = client_for_bucket(name)
    ver = _version_kwargs(version_id)
    try:
        head = client.head_object(Bucket=name, Key=object_key, **ver)
    except ClientError as exc:
        _missing_object(exc, object_key)
        raise
    size = int(head.get("ContentLength") or 0)
    checksum = {}
    for src, dest in (
        ("ChecksumCRC32", "crc32"),
        ("ChecksumCRC32C", "crc32c"),
        ("ChecksumSHA1", "sha1"),
        ("ChecksumSHA256", "sha256"),
        ("ChecksumCRC64NVME", "crc64nvme"),
        ("ChecksumType", "type"),
    ):
        if head.get(src):
            checksum[dest] = head[src]
    lock = {}
    if head.get("ObjectLockMode"):
        lock["mode"] = head["ObjectLockMode"]
    if head.get("ObjectLockRetainUntilDate"):
        lock["retain_until"] = to_iso(as_utc(head["ObjectLockRetainUntilDate"]))
    if head.get("ObjectLockLegalHoldStatus"):
        lock["legal_hold"] = head["ObjectLockLegalHoldStatus"]
    unavailable: dict[str, str] = {}
    tags = None
    if include_tags:
        try:
            tagging = client.get_object_tagging(Bucket=name, Key=object_key, **ver)
            tags = {item["Key"]: item["Value"] for item in tagging.get("TagSet") or []}
        except ClientError as exc:
            code = error_code(exc)
            if code in _NOT_FOUND:
                _missing_object(exc, object_key)
            if is_soft_error(code):
                unavailable["tags"] = code
            else:
                raise
    return {
        "bucket": name,
        "key": object_key,
        "size": size,
        "size_human": human_size(size),
        "content_type": head.get("ContentType"),
        "content_encoding": head.get("ContentEncoding"),
        "content_disposition": head.get("ContentDisposition"),
        "cache_control": head.get("CacheControl"),
        "etag": clean_etag(head.get("ETag")),
        "last_modified": to_iso(as_utc(head.get("LastModified"))),
        "storage_class": head.get("StorageClass") or "STANDARD",
        "version_id": head.get("VersionId"),
        "server_side_encryption": head.get("ServerSideEncryption"),
        "sse_kms_key_id": head.get("SSEKMSKeyId"),
        "metadata": head.get("Metadata") or {},
        "restore": head.get("Restore"),
        "archive_status": head.get("ArchiveStatus"),
        "object_lock": lock or None,
        "parts_count": head.get("PartsCount"),
        "checksum": checksum or None,
        "tags": tags,
        "unavailable": unavailable,
    }


def _consider_top(heap: list, sort_value: tuple, item: dict, limit: int) -> None:
    entry = (sort_value, item["key"], item)
    if len(heap) < limit:
        heapq.heappush(heap, entry)
    elif entry[0] > heap[0][0]:
        heapq.heapreplace(heap, entry)


def prefix_stats(
    bucket: Optional[str] = None,
    prefix: str = "",
    group_by_depth: int = 1,
    top_n: int = 10,
    max_objects: Optional[int] = None,
) -> dict[str, Any]:
    if int(group_by_depth) < 1:
        raise ToolError("group_by_depth debe ser >= 1.", "InvalidArgument")
    top_n = _clamp(top_n, 1, 100, "top_n")
    name, resolved_prefix = resolve_bucket_prefix(bucket, prefix)
    limit = _scan_limit(max_objects)
    client = client_for_bucket(name)
    scan = _Scan()
    total_bytes = 0
    min_size: Optional[int] = None
    max_size: Optional[int] = None
    zero_byte = 0
    oldest: Optional[dict] = None
    oldest_dt: Optional[datetime] = None
    extensions: dict[str, list[int]] = {}
    classes: dict[str, list[int]] = {}
    groups: dict[str, list[int]] = {}
    largest_heap: list = []
    newest_heap: list = []

    for obj in iter_objects(client, name, resolved_prefix, limit, scan):
        item = _summary(obj, resolved_prefix)
        size = item["size"]
        total_bytes += size
        min_size = size if min_size is None else min(min_size, size)
        max_size = size if max_size is None else max(max_size, size)
        if size == 0:
            zero_byte += 1
        ext = extension_of(item["key"]) or "(sin extensión)"
        extensions.setdefault(ext, [0, 0])
        extensions[ext][0] += 1
        extensions[ext][1] += size
        storage = item["storage_class"]
        classes.setdefault(storage, [0, 0])
        classes[storage][0] += 1
        classes[storage][1] += size
        label = group_label(item["key"], resolved_prefix, int(group_by_depth))
        groups.setdefault(label, [0, 0])
        groups[label][0] += 1
        groups[label][1] += size
        moment = as_utc(obj.get("LastModified")) or datetime.fromtimestamp(0, timezone.utc)
        if oldest_dt is None or moment < oldest_dt:
            oldest_dt = moment
            oldest = item
        _consider_top(largest_heap, (size, item["key"]), item, top_n)
        _consider_top(newest_heap, (moment.timestamp(), item["key"]), item, top_n)

    count = scan.scanned
    largest = [entry[2] for entry in sorted(largest_heap, key=lambda entry: entry[0], reverse=True)]
    newest = [entry[2] for entry in sorted(newest_heap, key=lambda entry: entry[0], reverse=True)]
    return {
        "bucket": name,
        "prefix": resolved_prefix,
        "object_count": count,
        "total_bytes": total_bytes,
        "total_bytes_human": human_size(total_bytes),
        "average_bytes": round(total_bytes / count, 2) if count else None,
        "min_size": min_size,
        "max_size": max_size,
        "oldest": oldest,
        "newest": newest,
        "by_extension": _agg_rows(extensions, "extension", 20),
        "by_storage_class": _agg_rows(classes, "storage_class"),
        "by_prefix": _agg_rows(groups, "prefix", 50),
        "largest": largest,
        "zero_byte_count": zero_byte,
        "scanned": count,
        "scan_truncated": scan.truncated,
        "group_by_depth": int(group_by_depth),
    }


class _Reverse:
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: "_Reverse") -> bool:
        return self.value > other.value


def _as_str_list(value: Any) -> Optional[list[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        return parts or None
    parts = [str(part).strip() for part in value if str(part).strip()]
    return parts or None


def _glob_match(key: str, pattern: str) -> bool:
    name = key.rstrip("/").rsplit("/", 1)[-1]
    target = key if "/" in pattern else name
    return fnmatch.fnmatchcase(target.lower(), pattern.lower())


def _ext_match(key: str, extensions: list[str]) -> bool:
    name = key.rsplit("/", 1)[-1].lower()
    for ext in extensions:
        cleaned = ext.lower().strip().lstrip(".")
        if cleaned and name.endswith("." + cleaned):
            return True
    return False


def _parse_user_size(value: Any, field: str) -> Optional[int]:
    try:
        return parse_size(value)
    except ValueError as exc:
        raise ToolError(f"{field} no es un tamaño válido: {value}", "InvalidArgument") from exc


def _parse_user_dt(value: Optional[str], field: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        return parse_datetime(value)
    except ValueError as exc:
        raise ToolError(f"{field} no es una fecha ISO válida: {value}", "InvalidArgument") from exc


def find_objects(
    bucket: Optional[str] = None,
    prefix: str = "",
    pattern: Optional[str] = None,
    regex: Optional[str] = None,
    extensions: Optional[list[str]] = None,
    min_size: Any = None,
    max_size: Any = None,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    storage_class: Optional[str] = None,
    sort_by: str = "key",
    descending: bool = False,
    limit: int = 100,
    max_objects: Optional[int] = None,
) -> dict[str, Any]:
    if sort_by not in {"key", "size", "last_modified"}:
        raise ToolError("sort_by debe ser key, size o last_modified.", "InvalidArgument")
    limit = _clamp(limit, 1, 1000, "limit")
    compiled = None
    if regex:
        if len(regex) > 1000:
            raise ToolError("regex demasiado larga.", "InvalidRegex")
        try:
            compiled = re.compile(regex)
        except re.error as exc:
            raise ToolError(f"regex inválida: {exc}", "InvalidRegex") from exc
    ext_list = _as_str_list(extensions)
    low = _parse_user_size(min_size, "min_size")
    high = _parse_user_size(max_size, "max_size")
    after = _parse_user_dt(modified_after, "modified_after")
    before = _parse_user_dt(modified_before, "modified_before")
    name, resolved_prefix = resolve_bucket_prefix(bucket, prefix)
    scan_max = _scan_limit(max_objects)
    client = client_for_bucket(name)
    scan = _Scan()
    matched = 0
    matched_bytes = 0
    heap: list = []
    seq = 0

    for obj in iter_objects(client, name, resolved_prefix, scan_max, scan):
        key = obj["Key"]
        size = int(obj.get("Size") or 0)
        if pattern and not _glob_match(key, pattern):
            continue
        if compiled is not None and compiled.search(key) is None:
            continue
        if ext_list and not _ext_match(key, ext_list):
            continue
        if low is not None and size < low:
            continue
        if high is not None and size > high:
            continue
        moment = as_utc(obj.get("LastModified"))
        if after is not None and (moment is None or moment < after):
            continue
        if before is not None and (moment is None or moment > before):
            continue
        actual = (obj.get("StorageClass") or "STANDARD").upper()
        if storage_class and actual != storage_class.upper():
            continue
        matched += 1
        matched_bytes += size
        if sort_by == "size":
            sort_value: tuple = (size, key)
        elif sort_by == "last_modified":
            sort_value = (moment.timestamp() if moment else 0.0, key)
        else:
            sort_value = (key,)
        seq += 1
        payload = _summary(obj, resolved_prefix)
        if descending:
            item = (sort_value, seq, payload)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif sort_value > heap[0][0]:
                heapq.heapreplace(heap, item)
        else:
            item = (_Reverse(sort_value), seq, payload)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif sort_value < heap[0][0].value:
                heapq.heapreplace(heap, item)

    if descending:
        matches = [entry[2] for entry in sorted(heap, key=lambda entry: entry[0], reverse=True)]
    else:
        matches = [entry[2] for entry in sorted(heap, key=lambda entry: entry[0].value)]
    return {
        "bucket": name,
        "prefix": resolved_prefix,
        "matches": matches,
        "matched_total": matched,
        "matched_total_bytes": matched_bytes,
        "matched_total_bytes_human": human_size(matched_bytes),
        "scanned": scan.scanned,
        "scan_truncated": scan.truncated,
        "limit": limit,
        "sort_by": sort_by,
        "descending": bool(descending),
    }


def _content_range(resp: dict) -> Optional[str]:
    if resp.get("ContentRange"):
        return str(resp["ContentRange"])
    headers = (resp.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    for key, value in headers.items():
        if key.lower() == "content-range" and value:
            return str(value)
    return None


def _window_from_get(resp: dict, offset: int, length: int) -> bytes:
    data = resp["Body"].read()
    ranged = _content_range(resp)
    if ranged and "bytes" in ranged.lower():
        return data[:length] if length else b""
    if offset:
        return data[offset:offset + length]
    return data[:length] if length else b""


def _gzip_window(raw: bytes, offset: int, max_bytes: int) -> tuple[bytes, bool]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            skipped = 0
            while skipped < offset:
                chunk = handle.read(min(65536, offset - skipped))
                if not chunk:
                    return b"", False
                skipped += len(chunk)
            out = bytearray()
            remaining = max_bytes
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    return bytes(out), False
                out += chunk
                remaining -= len(chunk)
            extra = handle.read(1)
            return bytes(out), bool(extra)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise ToolError(f"No se pudo descomprimir el gzip: {exc}", "InvalidGzip") from exc


def _fetch_head_and_bytes(
    client: Any,
    bucket: str,
    key: str,
    offset: int,
    length: int,
    version_id: Optional[str],
) -> tuple[dict, bytes, bool]:
    ver = _version_kwargs(version_id)
    try:
        head = client.head_object(Bucket=bucket, Key=key, **ver)
    except ClientError as exc:
        _missing_object(exc, key)
        raise
    size = int(head.get("ContentLength") or 0)
    if length <= 0 or offset >= size:
        return head, b"", False
    end = min(size - 1, offset + length - 1)
    try:
        resp = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={offset}-{end}", **ver)
    except ClientError as exc:
        _missing_object(exc, key)
        raise
    data = _window_from_get(resp, offset, length)
    return head, data, True


def read_object(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    offset: int = 0,
    max_bytes: int = 65536,
    encoding: str = "utf-8",
    decompress: bool = True,
    version_id: Optional[str] = None,
) -> dict[str, Any]:
    try:
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ToolError("offset debe ser un entero.", "InvalidArgument") from exc
    if offset < 0:
        raise ToolError("offset no puede ser negativo.", "InvalidArgument")
    try:
        import codecs

        codecs.lookup(encoding)
    except LookupError as exc:
        raise ToolError(f"encoding desconocido: {encoding}", "InvalidArgument") from exc
    settings = get_settings()
    max_bytes = _clamp(max_bytes, 0, settings.max_read_bytes, "max_bytes")
    name, object_key = resolve_bucket_key(bucket, key)
    client = client_for_bucket(name)
    do_gzip = bool(decompress) and object_key.lower().endswith(".gz")
    if do_gzip:
        head, raw, _used = _fetch_head_and_bytes(
            client, name, object_key, 0, settings.max_read_bytes, version_id
        )
        object_size = int(head.get("ContentLength") or 0)
        download_partial = object_size > len(raw)
        try:
            payload, more = _gzip_window(raw, offset, max_bytes)
        except ToolError:
            if download_partial:
                raise ToolError(
                    "El objeto gzip supera S3_MAX_READ_BYTES y no se pudo descomprimir por completo.",
                    "ObjectTooLarge",
                ) from None
            raise
        truncated = more or download_partial
        content_type = head.get("ContentType")
        decompressed = True
    else:
        head, payload, _used = _fetch_head_and_bytes(
            client, name, object_key, offset, max_bytes, version_id
        )
        object_size = int(head.get("ContentLength") or 0)
        truncated = offset + len(payload) < object_size
        content_type = head.get("ContentType")
        decompressed = False

    binary = looks_binary(payload)
    if binary:
        content = None
        content_hex = payload[:64].hex()
    else:
        content = payload.decode(encoding, errors="replace")
        content_hex = None
    return {
        "bucket": name,
        "key": object_key,
        "content_type": content_type,
        "object_size": object_size,
        "object_size_human": human_size(object_size),
        "offset": offset,
        "bytes_returned": len(payload),
        "truncated": truncated,
        "next_offset": offset + len(payload),
        "is_binary": binary,
        "decompressed": decompressed,
        "encoding": encoding,
        "content": content,
        "content_hex": content_hex,
    }


_FORMATS = {
    "csv": "csv",
    "tsv": "tsv",
    "tab": "tsv",
    "jsonl": "jsonl",
    "ndjson": "jsonl",
    "json": "json",
    "parquet": "parquet",
}


def infer_format(key: str) -> str:
    name = key.lower().rsplit("/", 1)[-1]
    if name.endswith(".gz"):
        name = name[:-3]
    for ext, fmt in (
        (".parquet", "parquet"),
        (".jsonl", "jsonl"),
        (".ndjson", "jsonl"),
        (".csv", "csv"),
        (".tsv", "tsv"),
        (".tab", "tsv"),
        (".json", "json"),
    ):
        if name.endswith(ext):
            return fmt
    raise ToolError(
        "No se pudo inferir el formato por la extensión. Indica format: csv, tsv, jsonl, json o parquet.",
        "InvalidArgument",
    )


def _normalize_format(fmt: Optional[str], key: str) -> str:
    if fmt is None or str(fmt).strip() == "":
        return infer_format(key)
    key_fmt = str(fmt).strip().lower()
    if key_fmt not in _FORMATS:
        raise ToolError(
            f"format '{fmt}' no es válido. Usa csv, tsv, jsonl, json o parquet.",
            "InvalidArgument",
        )
    return _FORMATS[key_fmt]


def _import_pyarrow():
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ToolError(
            "pyarrow no está instalado; ejecuta: pip install pyarrow",
            "MissingDependency",
        ) from exc
    return pq


def _infer_column_type(values: list[Any]) -> str:
    cleaned = [value for value in values if value is not None and value != ""]
    if not cleaned:
        return "string"

    def kind(value: Any) -> str:
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int) and not isinstance(value, bool):
            return "int"
        if isinstance(value, float):
            return "float"
        text = str(value).strip()
        if re.fullmatch(r"-?\d+", text):
            return "int"
        if re.fullmatch(r"-?(?:\d+\.\d+|\d+)(?:[eE][+-]?\d+)?", text) and not re.fullmatch(r"-?\d+", text):
            return "float"
        if text.lower() in {"true", "false", "yes", "no"}:
            return "bool"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", text):
            return "date"
        return "string"

    kinds = {kind(value) for value in cleaned}
    if kinds <= {"int", "float"} and "float" in kinds and "int" in kinds:
        return "float"
    if len(kinds) == 1:
        return kinds.pop()
    return "string"


def _coerce(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None if value is None else value
    if kind == "int" and isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    if kind == "float" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if kind == "bool" and isinstance(value, str):
        return value.strip().lower() in {"true", "yes"}
    return value


def _column_meta(rows: list[dict], names: list[str]) -> list[dict]:
    meta = []
    for name in names:
        values = [row.get(name) for row in rows]
        meta.append({"name": name, "type": _infer_column_type(values)})
    return meta


def _apply_types(rows: list[dict], columns: list[dict]) -> list[dict]:
    kinds = {col["name"]: col["type"] for col in columns}
    typed = []
    for row in rows:
        typed.append({key: _coerce(value, kinds.get(key, "string")) for key, value in row.items()})
    return typed


def _sniff_dialect(sample: str, default_delimiter: str) -> tuple[str, str]:
    snippet = sample[:8192]
    delimiter = default_delimiter
    quotechar = '"'
    if not snippet.strip():
        return delimiter, quotechar
    try:
        dialect = csv.Sniffer().sniff(snippet, delimiters=",;\t|")
    except csv.Error:
        return delimiter, quotechar
    return dialect.delimiter or default_delimiter, dialect.quotechar or '"'


def _header_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    return ""


def _parse_header(line: str, delimiter: str, quotechar: str) -> Optional[list[str]]:
    try:
        return next(csv.reader(io.StringIO(line), delimiter=delimiter, quotechar=quotechar))
    except (csv.Error, StopIteration):
        return None


def _still_quoted(value: str) -> bool:
    text = value.strip()
    return len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}


def _unquoted_header_count(fields: list[str]) -> int:
    return sum(1 for field in fields if not _still_quoted(field or ""))


def _quotechar_is_useful(fields: list[str]) -> bool:
    return bool(fields) and _unquoted_header_count(fields) * 2 > len(fields)


def _auto_quotechar(text: str, delimiter: str, sniffed: str) -> str:
    """Si el sniffer no deja la cabecera sin comillas, prueba comilla doble y simple."""
    header = _header_line(text)
    sniffed_quote = sniffed or '"'
    if not header:
        return sniffed_quote
    sniffed_fields = _parse_header(header, delimiter, sniffed_quote)
    if sniffed_fields is not None and _quotechar_is_useful(sniffed_fields):
        return sniffed_quote
    best_quote = '"'
    best_score = -1
    for candidate in ('"', "'"):
        fields = _parse_header(header, delimiter, candidate)
        if not fields:
            continue
        score = _unquoted_header_count(fields)
        if score > best_score:
            best_quote = candidate
            best_score = score
    return best_quote


def _normalize_quotechar(quotechar: Optional[str]) -> Optional[str]:
    if quotechar is None or quotechar == "":
        return None
    if len(quotechar) != 1:
        raise ToolError("quotechar debe ser un solo carácter.", "InvalidArgument")
    return quotechar


def _load_text_bytes(bucket: str, key: str) -> tuple[bytes, int, Optional[str], bool, bool]:
    """Descarga hasta S3_MAX_READ_BYTES y descomprime .gz. Devuelve partial y si hubo gzip."""
    settings = get_settings()
    client = client_for_bucket(bucket)
    head, raw, _used = _fetch_head_and_bytes(client, bucket, key, 0, settings.max_read_bytes, None)
    size = int(head.get("ContentLength") or 0)
    partial = size > len(raw)
    encoding = (head.get("ContentEncoding") or "").lower()
    gzipped = key.lower().endswith(".gz") or "gzip" in encoding
    if gzipped:
        try:
            raw, more = _gzip_window(raw, 0, settings.max_read_bytes)
        except ToolError:
            if partial:
                raise ToolError(
                    "El objeto gzip supera S3_MAX_READ_BYTES y no se pudo descomprimir por completo.",
                    "ObjectTooLarge",
                ) from None
            raise
        partial = partial or more
    return raw, size, head.get("ContentType"), partial, gzipped


def _preview_delimited(
    text: str,
    fmt: str,
    delimiter: Optional[str],
    rows: int,
    columns: Optional[list[str]],
    quotechar: Optional[str] = None,
) -> dict[str, Any]:
    default = "\t" if fmt == "tsv" else ","
    sniffed_delim, sniffed_quote = _sniff_dialect(text, default)
    sep = delimiter if delimiter else sniffed_delim
    chosen_quote = quotechar if quotechar else _auto_quotechar(text, sep, sniffed_quote)
    reader = csv.DictReader(io.StringIO(text), delimiter=sep, quotechar=chosen_quote)
    if not reader.fieldnames:
        raise ToolError("El archivo tabular no tiene cabecera.", "InvalidTable")
    fieldnames = [(name or "").strip() for name in reader.fieldnames if name]
    parsed: list[dict] = []
    extra = 0
    for raw in reader:
        item = {}
        for key, value in raw.items():
            if key is None:
                continue
            item[key.strip()] = None if value is None or value == "" else value
        if len(parsed) < rows:
            parsed.append(item)
        else:
            extra += 1
    return _finish_rows(fmt, fieldnames, parsed, columns, extra > 0, delimiter=sep)


def _preview_json_rows(text: str, fmt: str, rows: int, columns: Optional[list[str]], partial: bool) -> dict[str, Any]:
    records: list[Any] = []
    if fmt == "jsonl":
        lines = text.splitlines()
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if partial and index == len(lines):
                    break
                raise ToolError(f"JSONL inválido en la línea {index}: {exc}", "InvalidJSON") from exc
    else:
        try:
            payload = json.loads(text) if text.strip() else []
        except json.JSONDecodeError as exc:
            raise ToolError(f"JSON inválido: {exc}", "InvalidJSON") from exc
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = [payload]
        else:
            records = [{"value": payload}]
    normalized = []
    for record in records:
        if isinstance(record, dict):
            normalized.append({str(key): clip_cell(value) for key, value in record.items()})
        else:
            normalized.append({"value": clip_cell(record)})
    fieldnames: list[str] = []
    for record in normalized:
        for key in record:
            if key not in fieldnames:
                fieldnames.append(key)
    preview = normalized[:rows]
    return _finish_rows(fmt, fieldnames, preview, columns, len(normalized) > rows, delimiter=None)


def _finish_rows(
    fmt: str,
    fieldnames: list[str],
    parsed: list[dict],
    columns: Optional[list[str]],
    rows_truncated: bool,
    delimiter: Optional[str],
) -> dict[str, Any]:
    missing: list[str] = []
    names = fieldnames
    rows = parsed
    if columns:
        wanted = columns
        missing = [col for col in wanted if col not in fieldnames]
        names = wanted
        rows = [{col: row.get(col) for col in wanted} for row in parsed]
    meta = _column_meta(rows, names)
    typed = _apply_types([{key: clip_cell(value) for key, value in row.items()} for row in rows], meta)
    # Re-infer after coercion so int/float/bool reflejan el valor ya convertido.
    meta = _column_meta(typed, names)
    result: dict[str, Any] = {
        "format": fmt,
        "columns": meta,
        "rows": typed,
        "rows_returned": len(typed),
        "rows_truncated": rows_truncated,
    }
    if delimiter is not None:
        result["delimiter"] = delimiter
    if missing:
        result["missing_columns"] = missing
    return result


class _BudgetExceeded(Exception):
    """La siguiente lectura por rango superaría el tope de bytes."""


def _caused_by_budget(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, _BudgetExceeded):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class _S3RangeRaw(io.RawIOBase):
    """Archivo seekable sobre un objeto S3.

    ``read``/``seek`` piden ``get_object`` con ``Range`` en bloques de ~1 MB y
    se quedan con el último bloque. ``ParquetFile`` lo usa directo: envolverlo
    en ``BufferedReader`` hace que pyarrow lance ``NotImplementedError`` al
    posicionarse en el footer.
    """

    def __init__(
        self,
        client: Any,
        bucket: str,
        key: str,
        size: int,
        *,
        max_bytes: Optional[int] = None,
        block_size: int = _S3_RANGE_BLOCK,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._key = key
        self._size = max(0, int(size))
        self._max = None if max_bytes is None else int(max_bytes)
        self._block = max(1, int(block_size))
        self._pos = 0
        self._cache_start: Optional[int] = None
        self._cache = b""
        self.bytes_fetched = 0

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return True

    def size(self) -> int:
        return self._size

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            new = int(offset)
        elif whence == io.SEEK_CUR:
            new = self._pos + int(offset)
        elif whence == io.SEEK_END:
            new = self._size + int(offset)
        else:
            raise ValueError(f"whence inválido: {whence}")
        if new < 0:
            raise ValueError("seek negativo")
        self._pos = new
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("archivo cerrado")
        if size is None or size < 0:
            size = max(0, self._size - self._pos)
        size = int(size)
        if size <= 0 or self._pos >= self._size:
            return b""
        end = min(self._pos + size, self._size)
        data = self._read(self._pos, end)
        self._pos += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        count = len(data)
        buffer[:count] = data
        return count

    def _read(self, start: int, end: int) -> bytes:
        parts: list[bytes] = []
        pos = start
        while pos < end:
            block_start = (pos // self._block) * self._block
            self._load_block(block_start)
            offset = pos - block_start
            take = min(end - pos, len(self._cache) - offset)
            if take <= 0:
                break
            parts.append(self._cache[offset:offset + take])
            pos += take
        return b"".join(parts)

    def _load_block(self, block_start: int) -> None:
        if self._cache_start == block_start:
            return
        block_end = min(block_start + self._block, self._size)
        need = block_end - block_start
        if need <= 0:
            self._cache_start = block_start
            self._cache = b""
            return
        if self._max is not None and self.bytes_fetched + need > self._max:
            raise _BudgetExceeded(
                f"la lectura llegaría a {self.bytes_fetched + need} bytes (tope {self._max})"
            )
        resp = self._client.get_object(
            Bucket=self._bucket,
            Key=self._key,
            Range=f"bytes={block_start}-{block_end - 1}",
        )
        data = resp["Body"].read()
        self.bytes_fetched += len(data)
        self._cache_start = block_start
        self._cache = data

    def projected_extra(self, spans: list[tuple[int, int]]) -> int:
        """Bytes nuevos si se leen estos rangos en orden, con caché de un bloque."""
        cache = self._cache_start
        extra = 0
        for start, end in spans:
            pos = max(0, int(start))
            stop = min(self._size, int(end))
            while pos < stop:
                block_start = (pos // self._block) * self._block
                block_end = min(block_start + self._block, self._size)
                if block_start != cache:
                    extra += block_end - block_start
                    cache = block_start
                pos = block_end
        return extra


def _describe_parquet(parquet: Any) -> dict[str, Any]:
    schema = [{"name": field.name, "type": str(field.type)} for field in parquet.schema_arrow]
    meta: dict[str, Any] = {}
    raw_meta = parquet.metadata.metadata or {}
    for key, value in raw_meta.items():
        key_text = key.decode("utf-8", "replace") if isinstance(key, (bytes, bytearray)) else str(key)
        if key_text.startswith("ARROW:"):
            continue
        val_text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
        meta[key_text] = clip_cell(val_text)
    created = parquet.metadata.created_by or None
    return {
        "num_rows": int(parquet.metadata.num_rows),
        "num_row_groups": int(parquet.metadata.num_row_groups),
        "schema": schema,
        "metadata": meta,
        "created_by": created,
    }


def _column_selected(path: str, columns: Optional[list[str]]) -> bool:
    if not columns:
        return True
    for name in columns:
        if path == name or path.startswith(name + "."):
            return True
    return False


def _optional_page_offset(column: Any, name: str) -> int:
    """Algunos offsets de pyarrow lanzan NotImplementedError si la página no existe."""
    try:
        value = getattr(column, name)
    except (AttributeError, NotImplementedError):
        return 0
    if not value:
        return 0
    return int(value)


def _chunk_span(column: Any) -> tuple[int, int]:
    start = int(column.data_page_offset)
    dictionary = _optional_page_offset(column, "dictionary_page_offset")
    if dictionary:
        start = min(start, dictionary)
    index_page = _optional_page_offset(column, "index_page_offset")
    if index_page:
        start = min(start, index_page)
    length = int(column.total_compressed_size or 0)
    return start, start + max(0, length)


def _row_group_spans(row_group: Any, columns: Optional[list[str]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for index in range(row_group.num_columns):
        column = row_group.column(index)
        path = str(getattr(column, "path_in_schema", "") or "")
        if not _column_selected(path, columns):
            continue
        spans.append(_chunk_span(column))
    return spans


def _parquet_rows_body(
    described: dict[str, Any],
    records: list[dict],
    columns: Optional[list[str]],
    *,
    skipped: bool,
    aviso: Optional[str],
    bytes_fetched: int,
) -> dict[str, Any]:
    schema = described["schema"]
    schema_names = [field["name"] for field in schema]
    names = list(columns) if columns else schema_names
    clipped = [{key: clip_cell(value) for key, value in record.items()} for record in records]
    if columns:
        clipped = [{col: record.get(col) for col in columns} for record in clipped]
    if skipped or not clipped:
        types = {field["name"]: field["type"] for field in schema}
        column_meta = [{"name": name, "type": types.get(name, "string")} for name in names]
    else:
        column_meta = _column_meta(clipped, names)
    num_rows = int(described["num_rows"])
    body: dict[str, Any] = {
        "format": "parquet",
        "columns": column_meta,
        "rows": clipped,
        "rows_returned": len(clipped),
        "rows_truncated": num_rows > len(clipped),
        "num_rows": num_rows,
        "num_row_groups": described["num_row_groups"],
        "schema": schema,
        "metadata": described["metadata"],
        "created_by": described["created_by"],
        "bytes_fetched": bytes_fetched,
        "aviso": aviso,
    }
    return body


def _budget_error(_exc: Exception) -> ToolError:
    return ToolError(
        "No se pudo leer el parquet: el footer supera S3_MAX_PARQUET_READ_BYTES.",
        "ParquetReadLimit",
    )


def _open_parquet(raw: _S3RangeRaw) -> Any:
    pq = _import_pyarrow()
    try:
        return pq.ParquetFile(raw)
    except _BudgetExceeded as exc:
        raise _budget_error(exc) from exc
    except Exception as exc:
        if _caused_by_budget(exc):
            raise _budget_error(exc) from exc
        raise ToolError(f"No se pudo leer el parquet: {exc}", "InvalidParquet") from exc


def _preview_parquet_ranged(
    client: Any,
    bucket: str,
    key: str,
    size: int,
    rows: int,
    columns: Optional[list[str]],
) -> tuple[dict[str, Any], bool]:
    limit = get_settings().max_parquet_read_bytes
    raw = _S3RangeRaw(client, bucket, key, size, max_bytes=limit)
    parquet = _open_parquet(raw)
    described = _describe_parquet(parquet)
    schema_names = {field["name"] for field in described["schema"]}
    if columns:
        unknown = [col for col in columns if col not in schema_names]
        if unknown:
            raise ToolError(
                f"El parquet no tiene las columnas: {', '.join(unknown)}.",
                "InvalidParquet",
            )
    records: list[dict] = []
    skipped = False
    aviso = None
    if described["num_row_groups"] > 0:
        spans = _row_group_spans(parquet.metadata.row_group(0), columns)
        if raw.bytes_fetched + raw.projected_extra(spans) > limit:
            skipped = True
        else:
            try:
                table = parquet.read_row_group(0, columns=columns)
                records = table.slice(0, rows).to_pylist()
            except _BudgetExceeded:
                skipped = True
                records = []
            except Exception as exc:
                if _caused_by_budget(exc):
                    skipped = True
                    records = []
                else:
                    raise ToolError(
                        f"No se pudo leer las columnas del parquet: {exc}",
                        "InvalidParquet",
                    ) from exc
    if skipped:
        aviso = (
            "No se leyeron filas: el primer row group superaría "
            "S3_MAX_PARQUET_READ_BYTES. Se devuelven solo los metadatos."
        )
    body = _parquet_rows_body(
        described,
        records,
        columns,
        skipped=skipped,
        aviso=aviso,
        bytes_fetched=raw.bytes_fetched,
    )
    return body, skipped


def preview_table(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    rows: int = 20,
    format: Optional[str] = None,
    delimiter: Optional[str] = None,
    quotechar: Optional[str] = None,
    columns: Optional[list[str]] = None,
) -> dict[str, Any]:
    rows = _clamp(rows, 1, 1000, "rows")
    chosen_quote = _normalize_quotechar(quotechar)
    name, object_key = resolve_bucket_key(bucket, key)
    fmt = _normalize_format(format, object_key)
    selected = _as_str_list(columns)
    if fmt == "parquet":
        client = client_for_bucket(name)
        try:
            head = client.head_object(Bucket=name, Key=object_key)
        except ClientError as exc:
            _missing_object(exc, object_key)
            raise
        size = int(head.get("ContentLength") or 0)
        body, partial = _preview_parquet_ranged(client, name, object_key, size, rows, selected)
    else:
        raw, size, content_type, partial, _gzipped = _load_text_bytes(name, object_key)
        if looks_binary(raw):
            raise ToolError(
                "El objeto parece binario; no se puede previsualizar como tabla.",
                "BinaryObject",
            )
        text = raw.decode("utf-8-sig", errors="replace")
        if fmt in {"csv", "tsv"}:
            body = _preview_delimited(text, fmt, delimiter, rows, selected, chosen_quote)
        else:
            body = _preview_json_rows(text, fmt, rows, selected, partial)
        body["content_type"] = content_type
    body.update({
        "bucket": name,
        "key": object_key,
        "object_size": size,
        "object_size_human": human_size(size),
        "partial_read": partial,
    })
    return body


class _FetchState:
    def __init__(self) -> None:
        self.bytes_fetched = 0
        self.partial = False


class _ByteStream(io.RawIOBase):
    """Expone un iterador de bytes como archivo binario para ``TextIOWrapper``."""

    def __init__(self, chunks: Any) -> None:
        self._iter = iter(chunks)
        self._buf = bytearray()
        self._eof = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [bytes(self._buf)]
            self._buf.clear()
            for chunk in self._iter:
                if chunk:
                    parts.append(chunk)
            self._eof = True
            return b"".join(parts)
        while not self._eof and len(self._buf) < size:
            try:
                chunk = next(self._iter)
            except StopIteration:
                self._eof = True
                break
            if chunk:
                self._buf.extend(chunk)
        take = min(int(size), len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data


def _iter_object_plain(body: Any, gzipped: bool, state: _FetchState, max_bytes: int, size: int):
    """Lee el body por chunks. Si es gzip, descomprime con zlib (formato gzip)."""
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzipped else None
    try:
        while True:
            budget = max_bytes - state.bytes_fetched
            if budget <= 0:
                if state.bytes_fetched < size:
                    state.partial = True
                return
            chunk = body.read(min(_S3_RANGE_BLOCK, budget))
            if not chunk:
                if decomp is not None:
                    tail = decomp.flush()
                    if tail:
                        yield tail
                return
            state.bytes_fetched += len(chunk)
            if decomp is not None:
                plain = decomp.decompress(chunk)
                if plain:
                    yield plain
            else:
                yield chunk
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()


def _replay_plain(gen: Any, sample_bytes: int = 8192) -> tuple[bytes, Any]:
    head = bytearray()
    for piece in gen:
        if not piece:
            continue
        head.extend(piece)
        if len(head) >= sample_bytes:
            break

    def replay():
        if head:
            yield bytes(head)
        yield from gen

    return bytes(head), replay()


def _count_entry(
    *,
    key: str,
    size: int,
    last_modified: Any,
    fmt: Optional[str] = None,
    rows: Optional[int] = None,
    method: Optional[str] = None,
    exact: bool = False,
    partial: bool = False,
    bytes_fetched: int = 0,
    error: Optional[str] = None,
) -> dict[str, Any]:
    when = last_modified if isinstance(last_modified, str) else to_iso(as_utc(last_modified))
    return {
        "key": key,
        "size": int(size),
        "size_human": human_size(int(size)),
        "last_modified": when,
        "format": fmt,
        "rows": rows,
        "method": method,
        "exact": bool(exact),
        "partial": bool(partial),
        "bytes_fetched": int(bytes_fetched),
        "error": error,
    }


def _count_parquet(
    client: Any,
    bucket: str,
    key: str,
    size: int,
    last_modified: Any,
) -> dict[str, Any]:
    pq = _import_pyarrow()
    limit = get_settings().max_count_bytes
    raw = _S3RangeRaw(client, bucket, key, size, max_bytes=limit)
    try:
        parquet = pq.ParquetFile(raw)
        nrows = int(parquet.metadata.num_rows)
    except _BudgetExceeded:
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="parquet",
            error="No se pudo leer el footer parquet: supera S3_MAX_COUNT_BYTES.",
            exact=False,
            partial=True,
            bytes_fetched=raw.bytes_fetched,
        )
    except Exception as exc:
        if _caused_by_budget(exc):
            return _count_entry(
                key=key,
                size=size,
                last_modified=last_modified,
                fmt="parquet",
                error="No se pudo leer el footer parquet: supera S3_MAX_COUNT_BYTES.",
                exact=False,
                partial=True,
                bytes_fetched=raw.bytes_fetched,
            )
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="parquet",
            error=f"No se pudo leer el parquet: {exc}",
            bytes_fetched=raw.bytes_fetched,
        )
    return _count_entry(
        key=key,
        size=size,
        last_modified=last_modified,
        fmt="parquet",
        rows=nrows,
        method="parquet_footer",
        exact=True,
        bytes_fetched=raw.bytes_fetched,
    )


def _open_count_stream(client: Any, bucket: str, key: str, size: int, max_bytes: int):
    resp = client.get_object(Bucket=bucket, Key=key)
    state = _FetchState()
    gen = _iter_object_plain(resp["Body"], key.lower().endswith(".gz"), state, max_bytes, size)
    return gen, state


def _count_delimited_stream(
    client: Any,
    bucket: str,
    key: str,
    size: int,
    last_modified: Any,
    fmt: str,
    delimiter: Optional[str],
    quotechar: Optional[str],
    has_header: bool,
) -> dict[str, Any]:
    if size <= 0:
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt=fmt,
            rows=0,
            method="csv_stream",
            exact=True,
        )
    limit = get_settings().max_count_bytes
    gen, state = _open_count_stream(client, bucket, key, size, limit)
    try:
        head, replay = _replay_plain(gen)
        sample = head[:8192].decode("utf-8-sig", errors="replace")
        default = "\t" if fmt == "tsv" else ","
        sniffed_delim, sniffed_quote = _sniff_dialect(sample, default)
        sep = delimiter if delimiter else sniffed_delim
        chosen = quotechar if quotechar else _auto_quotechar(sample, sep, sniffed_quote)
        text = io.TextIOWrapper(_ByteStream(replay), encoding="utf-8-sig", newline="")
        count = 0
        try:
            reader = csv.reader(text, delimiter=sep, quotechar=chosen)
            try:
                for row in reader:
                    if not row:
                        continue
                    count += 1
            except csv.Error:
                state.partial = True
        finally:
            text.detach()
        if has_header and count:
            count -= 1
    finally:
        gen.close()
    return _count_entry(
        key=key,
        size=size,
        last_modified=last_modified,
        fmt=fmt,
        rows=count,
        method="csv_stream",
        exact=not state.partial,
        partial=state.partial,
        bytes_fetched=state.bytes_fetched,
    )


def _count_jsonl_stream(
    client: Any,
    bucket: str,
    key: str,
    size: int,
    last_modified: Any,
) -> dict[str, Any]:
    if size <= 0:
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="jsonl",
            rows=0,
            method="jsonl_stream",
            exact=True,
        )
    limit = get_settings().max_count_bytes
    gen, state = _open_count_stream(client, bucket, key, size, limit)
    count = 0
    try:
        _head, replay = _replay_plain(gen, sample_bytes=0)
        text = io.TextIOWrapper(_ByteStream(replay), encoding="utf-8-sig", newline="")
        try:
            for line in text:
                if line.strip():
                    count += 1
        finally:
            text.detach()
    finally:
        gen.close()
    return _count_entry(
        key=key,
        size=size,
        last_modified=last_modified,
        fmt="jsonl",
        rows=count,
        method="jsonl_stream",
        exact=not state.partial,
        partial=state.partial,
        bytes_fetched=state.bytes_fetched,
    )


def _count_json_array(
    client: Any,
    bucket: str,
    key: str,
    size: int,
    last_modified: Any,
) -> dict[str, Any]:
    settings = get_settings()
    if size > settings.max_read_bytes:
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="json",
            error="El JSON supera S3_MAX_READ_BYTES; no se contó el array.",
            exact=False,
            partial=True,
        )
    limit = min(settings.max_read_bytes, settings.max_count_bytes)
    gen, state = _open_count_stream(client, bucket, key, size, limit)
    try:
        parts = [piece for piece in gen if piece]
    finally:
        gen.close()
    if state.partial:
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="json",
            error="El JSON supera el tope de lectura; no se contó el array.",
            exact=False,
            partial=True,
            bytes_fetched=state.bytes_fetched,
        )
    raw = b"".join(parts)
    try:
        payload = json.loads(raw.decode("utf-8-sig")) if raw.strip() else None
    except json.JSONDecodeError as exc:
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="json",
            error=f"JSON inválido: {exc}",
            bytes_fetched=state.bytes_fetched,
        )
    if not isinstance(payload, list):
        return _count_entry(
            key=key,
            size=size,
            last_modified=last_modified,
            fmt="json",
            error="El JSON no es un array.",
            bytes_fetched=state.bytes_fetched,
        )
    return _count_entry(
        key=key,
        size=size,
        last_modified=last_modified,
        fmt="json",
        rows=len(payload),
        method="json_array",
        exact=True,
        bytes_fetched=state.bytes_fetched,
    )


def _count_object(
    client: Any,
    bucket: str,
    obj: dict,
    forced_format: Optional[str],
    delimiter: Optional[str],
    quotechar: Optional[str],
    has_header: bool,
) -> dict[str, Any]:
    key = str(obj.get("Key") or "")
    size = int(obj.get("Size") or 0)
    modified = obj.get("LastModified")
    try:
        fmt = forced_format or infer_format(key)
    except ToolError as exc:
        return _count_entry(key=key, size=size, last_modified=modified, error=str(exc))
    try:
        if fmt == "parquet":
            return _count_parquet(client, bucket, key, size, modified)
        if fmt in {"csv", "tsv"}:
            return _count_delimited_stream(
                client, bucket, key, size, modified, fmt, delimiter, quotechar, has_header
            )
        if fmt == "jsonl":
            return _count_jsonl_stream(client, bucket, key, size, modified)
        if fmt == "json":
            return _count_json_array(client, bucket, key, size, modified)
    except ClientError as exc:
        return _count_entry(
            key=key,
            size=size,
            last_modified=modified,
            fmt=fmt,
            error=f"{error_code(exc)}: {error_message(exc)}",
        )
    except ToolError as exc:
        return _count_entry(key=key, size=size, last_modified=modified, fmt=fmt, error=str(exc))
    except (zlib.error, csv.Error, UnicodeError) as exc:
        return _count_entry(key=key, size=size, last_modified=modified, fmt=fmt, error=str(exc))
    return _count_entry(
        key=key,
        size=size,
        last_modified=modified,
        fmt=fmt,
        error=f"Formato no soportado para contar filas: {fmt}.",
    )


def _count_accept(
    obj: dict,
    pattern: Optional[str],
    extensions: Optional[list[str]],
    after: Optional[datetime],
    before: Optional[datetime],
) -> bool:
    key = obj["Key"]
    if pattern and not _glob_match(key, pattern):
        return False
    if extensions and not _ext_match(key, extensions):
        return False
    moment = as_utc(obj.get("LastModified"))
    if after is not None and (moment is None or moment < after):
        return False
    if before is not None and (moment is None or moment > before):
        return False
    return True


def _select_count_objects(
    client: Any,
    bucket: str,
    prefix: str,
    pattern: Optional[str],
    extensions: Optional[list[str]],
    after: Optional[datetime],
    before: Optional[datetime],
    max_files: int,
) -> tuple[list[dict], bool]:
    scan = _Scan()
    matches: list[dict] = []
    files_truncated = False
    for obj in iter_objects(client, bucket, prefix, _scan_limit(None), scan):
        if not _count_accept(obj, pattern, extensions, after, before):
            continue
        if len(matches) >= max_files:
            files_truncated = True
            break
        matches.append(obj)
    else:
        if scan.truncated:
            files_truncated = True
    return matches, files_truncated


def _count_summary(
    bucket: str,
    *,
    key: Optional[str],
    prefix: Optional[str],
    files: list[dict],
    files_truncated: bool,
    max_files: int,
    started: float,
) -> dict[str, Any]:
    return {
        "bucket": bucket,
        "key": key,
        "prefix": prefix,
        "files": files,
        "total_rows": sum(item["rows"] or 0 for item in files),
        "files_counted": sum(1 for item in files if not item["error"]),
        "files_with_errors": sum(1 for item in files if item["error"]),
        "all_exact": all(item["exact"] and not item["error"] for item in files),
        "files_truncated": files_truncated,
        "max_files": max_files,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }


def count_rows(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    prefix: Optional[str] = None,
    pattern: Optional[str] = None,
    extensions: Optional[list[str]] = None,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    max_files: int = 50,
    format: Optional[str] = None,
    delimiter: Optional[str] = None,
    quotechar: Optional[str] = None,
    has_header: bool = True,
) -> dict[str, Any]:
    """Cuenta filas. Con ``prefix``, como máximo ``max_files`` objetos, en paralelo."""
    started = time.perf_counter()
    if (key is None) == (prefix is None):
        raise ToolError("Indica exactamente uno de key o prefix.", "InvalidArgument")
    max_files = _clamp(max_files, 1, 1000, "max_files")
    chosen_quote = _normalize_quotechar(quotechar)
    if delimiter == "":
        delimiter = None
    ext_list = _as_str_list(extensions)
    after = _parse_user_dt(modified_after, "modified_after")
    before = _parse_user_dt(modified_before, "modified_before")
    forced = None
    if format is not None and str(format).strip() != "":
        forced = _normalize_format(format, "file.csv")
    header = bool(has_header)

    if key is not None:
        name, object_key = resolve_bucket_key(bucket, key)
        client = client_for_bucket(name)
        try:
            head = client.head_object(Bucket=name, Key=object_key)
        except ClientError as exc:
            if error_code(exc) == "NoSuchBucket":
                raise
            entry = _count_entry(
                key=object_key,
                size=0,
                last_modified=None,
                error=f"{error_code(exc)}: {error_message(exc)}",
            )
            return _count_summary(
                name,
                key=object_key,
                prefix=None,
                files=[entry],
                files_truncated=False,
                max_files=max_files,
                started=started,
            )
        targets = [{
            "Key": object_key,
            "Size": int(head.get("ContentLength") or 0),
            "LastModified": head.get("LastModified"),
        }]
        files_truncated = False
        resolved_prefix = None
        resolved_key: Optional[str] = object_key
    else:
        name, resolved_prefix = resolve_bucket_prefix(bucket, prefix)
        client = client_for_bucket(name)
        targets, files_truncated = _select_count_objects(
            client, name, resolved_prefix, pattern, ext_list, after, before, max_files
        )
        resolved_key = None

    def _run(obj: dict) -> dict:
        try:
            return _count_object(client, name, obj, forced, delimiter, chosen_quote, header)
        except Exception as exc:
            return _count_entry(
                key=str(obj.get("Key") or ""),
                size=int(obj.get("Size") or 0),
                last_modified=obj.get("LastModified"),
                error=f"{type(exc).__name__}: {exc}",
            )

    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=_COUNT_WORKERS) as pool:
            files = list(pool.map(_run, targets))
    else:
        files = [_run(obj) for obj in targets]
    return _count_summary(
        name,
        key=resolved_key,
        prefix=resolved_prefix,
        files=files,
        files_truncated=files_truncated,
        max_files=max_files,
        started=started,
    )


def list_object_versions(
    bucket: Optional[str] = None,
    prefix: str = "",
    max_items: int = 100,
) -> dict[str, Any]:
    name, resolved_prefix = resolve_bucket_prefix(bucket, prefix)
    max_items = _clamp(max_items, 1, 1000, "max_items")
    client = client_for_bucket(name)
    try:
        resp = client.list_object_versions(Bucket=name, Prefix=resolved_prefix, MaxKeys=max_items)
    except ClientError as exc:
        if is_soft_error(error_code(exc)):
            raise ToolError(error_message(exc), error_code(exc)) from exc
        raise
    versions = []
    for item in resp.get("Versions") or []:
        size = int(item.get("Size") or 0)
        versions.append({
            "key": item.get("Key"),
            "version_id": item.get("VersionId"),
            "is_latest": bool(item.get("IsLatest")),
            "is_delete_marker": False,
            "size": size,
            "size_human": human_size(size),
            "last_modified": to_iso(as_utc(item.get("LastModified"))),
            "etag": clean_etag(item.get("ETag")),
        })
    markers = []
    for item in resp.get("DeleteMarkers") or []:
        markers.append({
            "key": item.get("Key"),
            "version_id": item.get("VersionId"),
            "is_latest": bool(item.get("IsLatest")),
            "is_delete_marker": True,
            "last_modified": to_iso(as_utc(item.get("LastModified"))),
        })
    return {
        "bucket": name,
        "prefix": resolved_prefix,
        "versions": versions,
        "delete_markers": markers,
        "is_truncated": bool(resp.get("IsTruncated")),
        "next_key_marker": resp.get("NextKeyMarker"),
        "next_version_id_marker": resp.get("NextVersionIdMarker"),
    }


def _index_prefix(bucket: str, prefix: str, max_objects: int) -> tuple[dict[str, tuple], _Scan]:
    client = client_for_bucket(bucket)
    scan = _Scan()
    index: dict[str, tuple] = {}
    for obj in iter_objects(client, bucket, prefix, max_objects, scan):
        rel = relative_name(obj["Key"], prefix)
        index[rel] = (int(obj.get("Size") or 0), clean_etag(obj.get("ETag")), obj["Key"])
    return index, scan


def compare_prefixes(
    prefix_a: str,
    prefix_b: str,
    bucket: Optional[str] = None,
    bucket_b: Optional[str] = None,
    max_objects: Optional[int] = None,
    limit: int = 100,
) -> dict[str, Any]:
    limit = _clamp(limit, 1, 1000, "limit")
    scan_max = _scan_limit(max_objects)
    bucket_a, resolved_a = resolve_bucket_prefix(bucket, prefix_a)
    if bucket_b is None and not (prefix_b and parse_s3_uri(prefix_b)[0]):
        bucket_b_name = bucket_a
        resolved_b = prefix_b or ""
    else:
        bucket_b_name, resolved_b = resolve_bucket_prefix(bucket_b if bucket_b is not None else bucket, prefix_b)
    left, scan_a = _index_prefix(bucket_a, resolved_a, scan_max)
    right, scan_b = _index_prefix(bucket_b_name, resolved_b, scan_max)
    only_a = []
    only_b = []
    different = []
    same = 0
    for rel, (size, etag, _full) in left.items():
        if rel not in right:
            only_a.append({"key": rel, "size": size, "size_human": human_size(size), "etag": etag})
            continue
        size_b, etag_b, _full_b = right[rel]
        if size != size_b or etag != etag_b:
            different.append({
                "key": rel,
                "size_a": size,
                "size_b": size_b,
                "size_a_human": human_size(size),
                "size_b_human": human_size(size_b),
                "etag_a": etag,
                "etag_b": etag_b,
            })
        else:
            same += 1
    for rel, (size, etag, _full) in right.items():
        if rel not in left:
            only_b.append({"key": rel, "size": size, "size_human": human_size(size), "etag": etag})
    only_a.sort(key=lambda item: item["key"])
    only_b.sort(key=lambda item: item["key"])
    different.sort(key=lambda item: item["key"])
    return {
        "bucket_a": bucket_a,
        "bucket_b": bucket_b_name,
        "prefix_a": resolved_a,
        "prefix_b": resolved_b,
        "only_in_a": only_a[:limit],
        "only_in_b": only_b[:limit],
        "different": different[:limit],
        "only_in_a_total": len(only_a),
        "only_in_b_total": len(only_b),
        "different_total": len(different),
        "same_count": same,
        "scanned_a": scan_a.scanned,
        "scanned_b": scan_b.scanned,
        "scan_truncated": scan_a.truncated or scan_b.truncated,
        "limit": limit,
    }
