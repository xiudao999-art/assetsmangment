"""Redis-backed distributed lock and short-lived job status storage."""
from __future__ import annotations

import json
from typing import Any


class RedisJobCoordinator:
    def __init__(self, url: str, *, prefix: str = "assets:submission-decode") -> None:
        import redis

        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
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
