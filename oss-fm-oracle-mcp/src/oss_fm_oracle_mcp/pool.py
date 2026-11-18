"""Pool de conexiones Oracle thin por RC (min 0, max 2)."""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Any

import oracledb

from oss_fm_oracle_mcp.config import Settings, dsn_for, normalize_rc_name

logger = logging.getLogger("oss-fm-oracle-mcp")

_pools: dict[tuple[str, str, str], oracledb.ConnectionPool] = {}
_pools_lock = threading.Lock()


def _pool_key(rc: str, settings: Settings) -> tuple[str, str, str]:
    name = normalize_rc_name(rc)
    return (name, dsn_for(name, settings), settings.user)


def get_pool(rc: str, settings: Settings) -> oracledb.ConnectionPool:
    """Devuelve (o crea) el pool del RC."""
    key = _pool_key(rc, settings)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            dsn = key[1]
            pool = oracledb.create_pool(
                user=settings.user,
                password=settings.password,
                dsn=dsn,
                min=0,
                max=2,
                increment=1,
            )
            _pools[key] = pool
            logger.debug("Pool creado para %s (%s)", key[0], dsn)
        return pool


def acquire_connection(
    rc: str,
    settings: Settings,
) -> oracledb.Connection:
    """Toma una conexión del pool; hace ping y reintenta una vez si está muerta."""
    pool = get_pool(rc, settings)
    conn = pool.acquire()
    try:
        conn.ping()
    except Exception:
        try:
            pool.release(conn)
        except Exception:
            pass
        conn = pool.acquire()
        conn.ping()
    return conn


def release_connection(rc: str, settings: Settings, conn: oracledb.Connection) -> None:
    """Devuelve la conexión al pool."""
    try:
        get_pool(rc, settings).release(conn)
    except Exception as e:
        logger.debug("release_connection %s: %s", rc, e)
        try:
            conn.close()
        except Exception:
            pass


def close_all_pools() -> None:
    with _pools_lock:
        items = list(_pools.items())
        _pools.clear()
    for key, pool in items:
        try:
            pool.close(force=True)
            logger.debug("Pool cerrado %s", key[0])
        except Exception as e:
            logger.debug("Error cerrando pool %s: %s", key[0], e)


def pool_stats() -> dict[str, Any]:
    """Estadísticas ligeras (tests/diagnóstico)."""
    with _pools_lock:
        return {
            "pool_count": len(_pools),
            "rcs": [k[0] for k in _pools],
        }


atexit.register(close_all_pools)
