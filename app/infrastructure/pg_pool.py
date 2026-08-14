"""Process-wide PostgreSQL connection pools shared by every repository."""
from __future__ import annotations

import logging
import threading
from contextlib import AbstractContextManager
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_pools: dict[str, Any] = {}
_lock = threading.RLock()


def get_pool(dsn: str):
    """Return the single process-local pool for *dsn*, creating it lazily."""
    normalized = (dsn or "").strip()
    if not normalized:
        raise ValueError("PostgreSQL DSN must not be empty")
    with _lock:
        pool = _pools.get(normalized)
        if pool is None:
            from psycopg_pool import ConnectionPool

            max_size = max(1, settings.database_pool_max_size)
            min_size = min(max_size, max(0, settings.database_pool_min_size))
            pool = ConnectionPool(
                conninfo=normalized,
                min_size=min_size,
                max_size=max_size,
                timeout=max(1.0, settings.database_pool_timeout_seconds),
                max_lifetime=max(60.0, settings.database_pool_max_lifetime_seconds),
                kwargs={
                    "autocommit": True,
                    "connect_timeout": 10,
                    "options": "-c timezone=Asia/Shanghai",
                },
                check=ConnectionPool.check_connection,
                open=True,
            )
            _pools[normalized] = pool
            logger.info(
                "PostgreSQL connection pool opened (min=%d max=%d)",
                min_size,
                max_size,
            )
        return pool


def connection(dsn: str) -> AbstractContextManager:
    """Borrow a healthy connection and return it to the shared pool on exit."""
    return get_pool(dsn).connection()


def close_all_pools() -> None:
    """Close every process-local pool during application shutdown."""
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            logger.exception("Failed to close PostgreSQL connection pool")
