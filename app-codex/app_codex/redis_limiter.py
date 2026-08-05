"""Redis-backed FIFO concurrency limiter for Codex SDK calls."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
import logging
import random
from typing import Any
import uuid


logger = logging.getLogger(__name__)


class RedisCodexLimiter:
    """Queue excess callers and enforce a cross-process concurrency limit."""

    _ENQUEUE_SCRIPT = """
local sequence = redis.call('INCR', KEYS[1])
redis.call('ZADD', KEYS[2], 'NX', sequence, ARGV[1])
redis.call('ZADD', KEYS[3], ARGV[3] + ARGV[2], ARGV[1])
return sequence
"""

    _TRY_ACQUIRE_SCRIPT = """
local now_ms = tonumber(ARGV[4])
redis.call('ZADD', KEYS[2], now_ms + ARGV[3], ARGV[1])

local stale_waiters = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now_ms)
for _, token in ipairs(stale_waiters) do
    redis.call('ZREM', KEYS[1], token)
    redis.call('ZREM', KEYS[2], token)
end

redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now_ms)

local rank = redis.call('ZRANK', KEYS[1], ARGV[1])
if rank == 0 and redis.call('ZCARD', KEYS[3]) < tonumber(ARGV[2]) then
    redis.call('ZREM', KEYS[1], ARGV[1])
    redis.call('ZREM', KEYS[2], ARGV[1])
    redis.call('ZADD', KEYS[3], now_ms + ARGV[3], ARGV[1])
    return 1
end
return 0
"""

    _RENEW_SCRIPT = """
local now_ms = tonumber(ARGV[3])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], now_ms + ARGV[2], ARGV[1])
    return 1
end
return 0
"""

    _REMOVE_WAITER_SCRIPT = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""

    _RELEASE_SCRIPT = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        limit: int = 2,
        *,
        namespace: str = "app-codex",
        poll_interval_seconds: float = 0.2,
        lease_seconds: float = 30.0,
        cooldown_min_seconds: float = 2.0,
        cooldown_max_seconds: float = 5.0,
        client: Any | None = None,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._redis_url = redis_url
        self._limit = max(1, int(limit))
        self._poll_interval_seconds = max(0.01, poll_interval_seconds)
        self._lease_ms = max(5_000, int(lease_seconds * 1000))
        self._cooldown_min_seconds = max(0.0, cooldown_min_seconds)
        self._cooldown_max_seconds = max(
            self._cooldown_min_seconds, cooldown_max_seconds
        )
        self._random_uniform = random_uniform
        self._client = client

        self._sequence_key = f"{namespace}:sequence"
        self._queue_key = f"{namespace}:queue"
        self._waiters_key = f"{namespace}:waiters"
        self._active_key = f"{namespace}:active"

    def _get_client(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._client

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Wait in FIFO order, then hold a slot through the cooldown period."""
        token = uuid.uuid4().hex
        client = self._get_client()
        now_ms = await self._redis_time_ms()
        await client.eval(
            self._ENQUEUE_SCRIPT,
            3,
            self._sequence_key,
            self._queue_key,
            self._waiters_key,
            token,
            self._lease_ms,
            now_ms,
        )

        acquired = False
        renew_task: asyncio.Task[None] | None = None
        try:
            while not acquired:
                now_ms = await self._redis_time_ms()
                acquired = bool(
                    await client.eval(
                        self._TRY_ACQUIRE_SCRIPT,
                        3,
                        self._queue_key,
                        self._waiters_key,
                        self._active_key,
                        token,
                        self._limit,
                        self._lease_ms,
                        now_ms,
                    )
                )
                if not acquired:
                    await asyncio.sleep(self._poll_interval_seconds)

            renew_task = asyncio.create_task(self._renew_lease(token))
            yield
        finally:
            if renew_task is not None:
                renew_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renew_task

            cleanup = asyncio.create_task(
                self._cooldown_and_release(token)
                if acquired
                else self._remove_waiter(token)
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _renew_lease(self, token: str) -> None:
        interval = max(1.0, self._lease_ms / 3000)
        client = self._get_client()
        while True:
            await asyncio.sleep(interval)
            try:
                now_ms = await self._redis_time_ms()
                renewed = await client.eval(
                    self._RENEW_SCRIPT,
                    1,
                    self._active_key,
                    token,
                    self._lease_ms,
                    now_ms,
                )
                if not renewed:
                    logger.error("Codex Redis limiter lost active lease %s", token)
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to renew Codex Redis limiter lease")

    async def _redis_time_ms(self) -> int:
        seconds, microseconds = await self._get_client().time()
        return (int(seconds) * 1000) + (int(microseconds) // 1000)

    async def _cooldown_and_release(self, token: str) -> None:
        try:
            delay = self._random_uniform(
                self._cooldown_min_seconds, self._cooldown_max_seconds
            )
            await asyncio.sleep(delay)
        finally:
            await self._get_client().eval(
                self._RELEASE_SCRIPT, 1, self._active_key, token
            )

    async def _remove_waiter(self, token: str) -> None:
        await self._get_client().eval(
            self._REMOVE_WAITER_SCRIPT,
            2,
            self._queue_key,
            self._waiters_key,
            token,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

