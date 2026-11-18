"""Descarga de objetos S3 a disco local (lectura remota, escritura solo local)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from s3_mcp.config import get_settings
from s3_mcp.ops import ToolError, client_for_bucket, resolve_bucket_key
from s3_mcp.util import human_size


def download_object(
    key: str,
    bucket: Optional[str] = None,
    local_dir: str = r"C:\DVB\exports\s3",
) -> dict[str, Any]:
    name, object_key = resolve_bucket_key(bucket, key)
    settings = get_settings()
    client = client_for_bucket(name)
    try:
        head = client.head_object(Bucket=name, Key=object_key)
    except Exception as exc:
        from s3_mcp.ops import ClientError, _missing_object

        if isinstance(exc, ClientError):
            _missing_object(exc, object_key)
        raise
    size = int(head.get("ContentLength") or 0)
    if size > settings.max_download_bytes:
        raise ToolError(
            f"El objeto ({human_size(size)}) supera S3_MAX_DOWNLOAD_BYTES ({human_size(settings.max_download_bytes)}).",
            "ObjectTooLarge",
        )
    target_dir = Path(local_dir).expanduser()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"No se pudo crear el directorio local: {local_dir}", "InvalidArgument") from exc
    base_name = object_key.rstrip("/").rsplit("/", 1)[-1] or "object"
    local_path = target_dir / base_name
    if local_path.exists():
        stem = local_path.stem
        suffix = local_path.suffix
        counter = 1
        while local_path.exists():
            local_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    client.download_file(name, object_key, str(local_path))
    written = os.path.getsize(local_path)
    return {
        "bucket": name,
        "key": object_key,
        "local_path": str(local_path.resolve()),
        "bytes": written,
        "bytes_human": human_size(written),
        "object_size": size,
        "object_size_human": human_size(size),
    }
