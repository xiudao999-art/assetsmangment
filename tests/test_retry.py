"""AI 调用超时+重试 helper(优化 C):失败重试、到点超时、耗尽抛错。"""
import time
import pytest
from app.infrastructure.retry import call_ai


def test_call_ai_retries_then_succeeds():
    n = {"c": 0}

    def fn():
        n["c"] += 1
        if n["c"] < 3:
            raise RuntimeError("偶发失败")
        return "ok"

    assert call_ai(fn, timeout_s=5, retries=2, base_delay=0.01) == "ok"
    assert n["c"] == 3   # 首次 + 2 次重试


def test_call_ai_exhausts_and_raises_last():
    def fn():
        raise ValueError("总失败")
    with pytest.raises(ValueError):
        call_ai(fn, timeout_s=5, retries=2, base_delay=0.01)


def test_call_ai_times_out_and_does_not_hang():
    def fn():
        time.sleep(3)
        return "late"
    t0 = time.time()
    with pytest.raises(TimeoutError):
        call_ai(fn, timeout_s=1, retries=0)
    assert time.time() - t0 < 2.0   # 1s 到点就抛,没干等 3s
