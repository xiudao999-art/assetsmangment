from __future__ import annotations

import asyncio

import pytest

from app_codex.config import Settings
from app_codex.redis_limiter import RedisCodexLimiter


class _ScriptedRedis:
    def __init__(self, acquire_results: list[int]) -> None:
        self.acquire_results = acquire_results
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.acquire_called = asyncio.Event()
        self.closed = False

    async def time(self) -> tuple[int, int]:
        return (1_700_000_000, 0)

    async def eval(self, script: str, _key_count: int, *args: object) -> int:
        if script == RedisCodexLimiter._ENQUEUE_SCRIPT:
            name, result = "enqueue", 1
        elif script == RedisCodexLimiter._TRY_ACQUIRE_SCRIPT:
            name = "acquire"
            self.acquire_called.set()
            result = self.acquire_results.pop(0) if self.acquire_results else 0
        elif script == RedisCodexLimiter._RENEW_SCRIPT:
            name, result = "renew", 1
        elif script == RedisCodexLimiter._REMOVE_WAITER_SCRIPT:
            name, result = "remove_waiter", 1
        elif script == RedisCodexLimiter._RELEASE_SCRIPT:
            name, result = "release", 1
        else:  # pragma: no cover
            raise AssertionError("unknown Lua script")
        self.calls.append((name, args))
        return result

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_limiter_queues_then_cools_down_before_releasing() -> None:
    redis = _ScriptedRedis([0, 1])
    cooldown_range: list[tuple[float, float]] = []

    def no_wait_random(low: float, high: float) -> float:
        cooldown_range.append((low, high))
        return 0

    limiter = RedisCodexLimiter(
        client=redis,
        poll_interval_seconds=0.01,
        random_uniform=no_wait_random,
    )

    async with limiter.slot():
        assert [name for name, _ in redis.calls].count("acquire") == 2
        acquire_args = next(args for name, args in redis.calls if name == "acquire")
        assert acquire_args[-3] == 2

    assert cooldown_range == [(2.0, 5.0)]
    assert [name for name, _ in redis.calls][-1] == "release"
    assert "remove_waiter" not in [name for name, _ in redis.calls]


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_from_queue() -> None:
    redis = _ScriptedRedis([])
    limiter = RedisCodexLimiter(client=redis, poll_interval_seconds=0.01)

    async def wait_for_slot() -> None:
        async with limiter.slot():
            raise AssertionError("no slot should have been acquired")

    task = asyncio.create_task(wait_for_slot())
    await redis.acquire_called.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert [name for name, _ in redis.calls][-1] == "remove_waiter"


@pytest.mark.asyncio
async def test_limiter_closes_redis_client() -> None:
    redis = _ScriptedRedis([1])
    limiter = RedisCodexLimiter(client=redis)

    await limiter.close()

    assert redis.closed is True


def test_codex_concurrency_can_be_configured_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("CODEX_REDIS_URL", "redis://127.0.0.1:6379/3")

    configured = Settings(_env_file=None)

    assert configured.max_concurrency == 4
    assert configured.redis_url == "redis://127.0.0.1:6379/3"

