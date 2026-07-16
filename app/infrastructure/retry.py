"""AI 调用的统一超时+重试小工具(infra 通用,不依赖具体 SDK 是否支持 timeout 参数)。

用独立 daemon 线程跑调用 + join(timeout) 强制封顶等待时间;失败(含超时)按指数退避重试若干次;
全部失败抛最后一次异常——调用方(audit_pipeline 的 try/except)会把它降级为 REVIEW 或丢该帧,不会卡死整条。
"""
from __future__ import annotations
import time
import threading


def _run_with_timeout(fn, timeout_s: int):
    box: dict = {}

    def _target():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_target, daemon=True)   # daemon:超时后底层调用自然结束,不阻塞退出
    th.start()
    th.join(max(1, int(timeout_s)))
    if th.is_alive():
        raise TimeoutError(f"调用超过 {timeout_s}s")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def call_ai(fn, timeout_s: int = 60, retries: int = 2, base_delay: float = 0.5):
    """跑 fn():每次限时 timeout_s 秒;失败重试 retries 次(指数退避);全败抛最后异常。"""
    last: Exception | None = None
    for attempt in range(max(0, int(retries)) + 1):
        try:
            return _run_with_timeout(fn, timeout_s)
        except Exception as e:  # noqa: BLE001  含 TimeoutError
            last = e
            if attempt < retries:
                time.sleep(base_delay * (2 ** attempt))
    raise last if last is not None else RuntimeError("call_ai: 未执行")
