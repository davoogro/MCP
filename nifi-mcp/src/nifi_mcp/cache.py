"""Caché TTL async-safe en memoria."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TTLCache:
    """Caché simple con TTL y bloqueo async."""

    def __init__(self) -> None:
        self._store: dict[str, CacheEntry[Any]] = {}
        self._lock = asyncio.Lock()

    def _key(self, namespace: str, *parts: str) -> str:
        return namespace + "::" + "::".join(parts)

    async def get(self, namespace: str, *parts: str) -> Any | None:
        key = self._key(namespace, *parts)
        async with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            if time.monotonic() >= entry.expires_at:
                del self._store[key]
                return None
            return entry.value

    async def set(self, namespace: str, *parts: str, value: Any, ttl_seconds: float) -> None:
        key = self._key(namespace, *parts)
        async with self._lock:
            self._store[key] = CacheEntry(value=value, expires_at=time.monotonic() + ttl_seconds)

    async def invalidate(self, namespace: str | None = None, *parts: str) -> None:
        async with self._lock:
            if namespace is None:
                self._store.clear()
                return
            prefix = self._key(namespace, *parts)
            to_del = [k for k in self._store if k.startswith(prefix)]
            for k in to_del:
                del self._store[k]
