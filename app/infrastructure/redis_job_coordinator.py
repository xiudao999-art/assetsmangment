"""Redis-backed distributed lock and short-lived job status storage."""
from __future__ import annotations

import json
import threading
from typing import Any


_pool_lock = threading.Lock()
_shared_pools: dict[tuple[str, int], Any] = {}


def get_redis_pool(url: str, *, max_connections: int = 20):
    """Return the process-wide Redis pool for a URL and connection limit."""
    import redis

    key = (url, max(1, int(max_connections)))
    with _pool_lock:
        pool = _shared_pools.get(key)
        if pool is None:
            pool = redis.ConnectionPool.from_url(
                url,
                max_connections=key[1],
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=5,
                health_check_interval=30,
            )
            _shared_pools[key] = pool
        return pool


def close_all_redis_pools() -> None:
    """Close and forget all process-wide Redis pools."""
    with _pool_lock:
        pools = list(_shared_pools.values())
        _shared_pools.clear()
    for pool in pools:
        pool.disconnect()


class RedisJobCoordinator:
    def __init__(
        self,
        url: str,
        *,
        prefix: str = "assets:submission-decode",
        max_connections: int = 20,
    ) -> None:
        import redis

        self._client = redis.Redis(
            connection_pool=get_redis_pool(
                url,
                max_connections=max_connections,
            ),
        )
        self._prefix = prefix.rstrip(":")

    def acquire(self, job_id: str, *, timeout_seconds: int):
        lock = self._client.lock(
            f"{self._prefix}:lock:{job_id}",
            timeout=max(60, int(timeout_seconds)),
            blocking=False,
            thread_local=False,
        )
        return lock if lock.acquire(blocking=False) else None

    def set_status(self, job_id: str, status: dict[str, Any], *, ttl_seconds: int) -> None:
        payload = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
        self._client.setex(
            f"{self._prefix}:status:{job_id}",
            max(60, int(ttl_seconds)),
            payload,
        )

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        payload = self._client.get(f"{self._prefix}:status:{job_id}")
        if not payload:
            return None
        value = json.loads(payload)
        return value if isinstance(value, dict) else None
