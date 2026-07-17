"""雪花 ID 生成器(app/infrastructure/snowflake.py)—— 全项目 PG 业务表主键规范。
64 位 = 1 符号(0) | 41 时间戳ms(纪元 2025-01-01Z) | 10 worker_id | 12 序列。"""
import threading

import pytest

from app.infrastructure.snowflake import EPOCH_MS, Snowflake, next_id, next_id_str


def make_time_fn(values: list[int]):
    """按序吐出预设毫秒时间戳;耗尽后停在最后一个值(供回拨/自旋场景注入)。"""
    vals = list(values)

    def fn() -> int:
        return vals.pop(0) if len(vals) > 1 else vals[0]

    return fn


def test_epoch_is_2025_01_01_utc():
    assert EPOCH_MS == 1735689600000


def test_uniqueness_10k():
    sf = Snowflake(worker_id=1)
    ids = [sf.next_id() for _ in range(10_000)]
    assert len(set(ids)) == 10_000


def test_monotonic_increasing():
    sf = Snowflake(worker_id=1)
    prev = sf.next_id()
    for _ in range(5_000):
        cur = sf.next_id()
        assert cur > prev
        prev = cur


def test_threaded_uniqueness():
    sf = Snowflake(worker_id=2)
    out: list[int] = []
    lock = threading.Lock()

    def worker():
        local = [sf.next_id() for _ in range(2_000)]
        with lock:
            out.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 8 * 2_000


def test_worker_id_bit_field():
    sf = Snowflake(worker_id=515)
    i = sf.next_id()
    assert i > 0                        # 符号位恒 0
    assert (i >> 12) & 0x3FF == 515     # 12~21 位是 worker_id


def test_worker_id_from_env(monkeypatch):
    monkeypatch.setenv("AM_WORKER_ID", "7")
    sf = Snowflake()                    # 不传 → 读 AM_WORKER_ID
    assert (sf.next_id() >> 12) & 0x3FF == 7


def test_worker_id_default_zero(monkeypatch):
    monkeypatch.delenv("AM_WORKER_ID", raising=False)
    sf = Snowflake()
    assert (sf.next_id() >> 12) & 0x3FF == 0


def test_worker_id_out_of_range():
    with pytest.raises(ValueError):
        Snowflake(worker_id=1024)       # 10 位上限 1023
    with pytest.raises(ValueError):
        Snowflake(worker_id=-1)


def test_timestamp_bits_use_epoch():
    base = EPOCH_MS + 123_456
    sf = Snowflake(worker_id=0, time_fn=make_time_fn([base]))
    assert sf.next_id() >> 22 == 123_456


def test_next_id_str_roundtrip():
    s = next_id_str()
    assert isinstance(s, str) and s.isdigit()
    assert str(int(s)) == s             # 无前导零,int round-trip
    assert isinstance(next_id(), int)


def test_clock_rollback_small_waits():
    """回拨 ≤50ms:自旋等时钟追上,不报错、ID 仍唯一递增。"""
    base = EPOCH_MS + 1_000_000
    sf = Snowflake(worker_id=0, time_fn=make_time_fn([base, base - 30, base - 10, base, base + 1]))
    a = sf.next_id()                    # 在 base
    b = sf.next_id()                    # 见 base-30 → 等到 base 再发
    assert b > a


def test_clock_rollback_large_raises():
    """回拨 >50ms:拒绝发号(防重复),RuntimeError。"""
    base = EPOCH_MS + 1_000_000
    sf = Snowflake(worker_id=0, time_fn=make_time_fn([base, base - 51, base - 51]))
    sf.next_id()
    with pytest.raises(RuntimeError):
        sf.next_id()


def test_sequence_exhaustion_spins_to_next_ms():
    """同毫秒 4096 个序列耗尽 → 自旋到下一毫秒继续,不重复。"""
    base = EPOCH_MS + 2_000_000
    calls = {"n": 0}

    def fn() -> int:
        calls["n"] += 1
        return base if calls["n"] < 4_200 else base + 1

    sf = Snowflake(worker_id=0, time_fn=fn)
    ids = [sf.next_id() for _ in range(4_097)]
    assert len(set(ids)) == 4_097
    assert ids[-1] >> 22 == base + 1 - EPOCH_MS   # 第 4097 个落在下一毫秒
