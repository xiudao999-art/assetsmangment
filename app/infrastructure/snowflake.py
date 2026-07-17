"""雪花 ID 生成器 —— 全项目 PG 业务表主键规范(BIGINT)。只依赖 stdlib。
64 位 = 1 符号(0) | 41 时间戳ms(纪元 2025-01-01Z) | 10 worker_id | 12 序列。
worker_id 取环境变量 AM_WORKER_ID(K8s 多副本时用有序编号注入),默认 0。
时钟回拨:≤50ms 自旋等待追上;>50ms 拒绝发号(RuntimeError),防重复 ID。"""
from __future__ import annotations
import os
import threading
import time
from typing import Callable

EPOCH_MS = 1735689600000          # 2025-01-01T00:00:00Z;41 位毫秒可用约 69 年
_WORKER_BITS = 10
_SEQ_BITS = 12
_MAX_WORKER = (1 << _WORKER_BITS) - 1   # 1023
_SEQ_MASK = (1 << _SEQ_BITS) - 1        # 4095
_MAX_BACKWARD_MS = 50                    # 容忍的最大时钟回拨


def _now_ms() -> int:
    return int(time.time() * 1000)


class Snowflake:
    def __init__(self, worker_id: int | None = None,
                 time_fn: Callable[[], int] = _now_ms) -> None:
        if worker_id is None:
            worker_id = int(os.environ.get("AM_WORKER_ID", "0"))
        if not (0 <= worker_id <= _MAX_WORKER):
            raise ValueError(f"worker_id 必须在 0..{_MAX_WORKER},得到 {worker_id}")
        self._worker_id = worker_id
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._last_ms = -1
        self._seq = 0

    def next_id(self) -> int:
        with self._lock:
            now = self._time_fn()
            if now < self._last_ms:
                back = self._last_ms - now
                if back > _MAX_BACKWARD_MS:
                    raise RuntimeError(f"时钟回拨 {back}ms(>{_MAX_BACKWARD_MS}ms),拒绝发号防重复 ID")
                while now < self._last_ms:   # 小回拨:自旋等时钟追上
                    now = self._time_fn()
            if now == self._last_ms:
                self._seq = (self._seq + 1) & _SEQ_MASK
                if self._seq == 0:           # 同毫秒 4096 个耗尽 → 自旋到下一毫秒
                    while now <= self._last_ms:
                        now = self._time_fn()
            else:
                self._seq = 0
            self._last_ms = now
            return ((now - EPOCH_MS) << (_WORKER_BITS + _SEQ_BITS)) \
                | (self._worker_id << _SEQ_BITS) | self._seq

    def next_id_str(self) -> str:
        """字符串形式:domain/API 的 id 保持 str(int64 直传 JS 会丢精度)。"""
        return str(self.next_id())


_default = Snowflake()


def next_id() -> int:
    return _default.next_id()


def next_id_str() -> str:
    return _default.next_id_str()
