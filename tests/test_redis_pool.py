from __future__ import annotations


def test_redis_coordinators_share_pool(monkeypatch):
    import redis
    from app.infrastructure import redis_job_coordinator as module

    created = []

    class FakePool:
        def disconnect(self):
            self.disconnected = True

    def fake_from_url(url, **kwargs):
        pool = FakePool()
        pool.url = url
        pool.kwargs = kwargs
        pool.disconnected = False
        created.append(pool)
        return pool

    class FakeRedis:
        def __init__(self, *, connection_pool):
            self.connection_pool = connection_pool

    module.close_all_redis_pools()
    monkeypatch.setattr(redis.ConnectionPool, "from_url", fake_from_url)
    monkeypatch.setattr(redis, "Redis", FakeRedis)

    first = module.RedisJobCoordinator("redis://example/0", max_connections=12)
    second = module.RedisJobCoordinator(
        "redis://example/0",
        prefix="assets:other",
        max_connections=12,
    )

    assert first._client.connection_pool is second._client.connection_pool
    assert len(created) == 1
    assert created[0].kwargs["max_connections"] == 12

    module.close_all_redis_pools()
    assert created[0].disconnected is True
