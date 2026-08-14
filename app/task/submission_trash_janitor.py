"""素材提报回收站清理任务。

逻辑删除满 retention_days 后，删除当前及操作历史引用过的全部 OSS 对象。
删除前先原子写 oss_del_flag 抢占并禁止恢复；失败不解锁，避免恢复出文件不完整的数据，
残留 OSS 交由人工处理。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def submission_asset_keys(submission, operations: list[dict]) -> set[str]:
    """收集当前值和历史替换值中的视频、附件 OSS key。"""
    keys = {
        str(getattr(submission, "oss_key", "") or "").strip(),
        str(getattr(submission, "decoded_oss_key", "") or "").strip(),
    }
    keys.update(str(key or "").strip() for key in (getattr(
        submission, "platform_reject_attachments", [],
    ) or []))
    for operation in operations:
        for change in operation.get("changes") or []:
            field = change.get("field")
            if field in ("oss_key", "decoded_oss_key"):
                values = (change.get("before"), change.get("after"))
            elif field == "platform_reject_attachments":
                values = [
                    key
                    for side in (change.get("before"), change.get("after"))
                    for key in (side if isinstance(side, list) else [])
                ]
            else:
                continue
            keys.update(str(value or "").strip() for value in values)
    return {key for key in keys if key}


class SubmissionTrashJanitor:
    def __init__(self, repo, storage, retention_days: int = 7,
                 timezone: str = "Asia/Shanghai", run_hour: int = 1,
                 lock_coordinator=None, lock_ttl_seconds: int = 23 * 60 * 60,
                 require_distributed_lock: bool = False) -> None:
        self._repo = repo
        self._storage = storage
        self._retention_ms = max(1, retention_days) * 24 * 60 * 60 * 1000
        self._timezone = ZoneInfo(timezone)
        self._run_hour = min(23, max(0, run_hour))
        self._lock_coordinator = lock_coordinator
        self._lock_ttl_seconds = max(60, int(lock_ttl_seconds))
        self._require_distributed_lock = bool(require_distributed_lock)
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None

    def start(self) -> None:
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="submission-trash-janitor",
        )
        self._thread.start()
        logger.info("SubmissionTrashJanitor started (daily %02d:00, retention=%d days)",
                    self._run_hour, self._retention_ms // 86400000)

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("SubmissionTrashJanitor stopped")

    def run_once(self, now_ms: int | None = None) -> int:
        """执行一轮清理。公开方法便于在完全隔离的 fake 上测试。"""
        if self._lock_coordinator is None:
            if self._require_distributed_lock:
                logger.error("素材提报 OSS 清理跳过：未配置 Redis 分布式锁")
                return 0
            return self._run_once_unlocked(now_ms)

        try:
            distributed_lock = self._lock_coordinator.acquire(
                "daily-cleanup", timeout_seconds=self._lock_ttl_seconds,
            )
        except Exception:
            logger.exception("素材提报 OSS 清理跳过：获取 Redis 分布式锁失败")
            return 0
        if distributed_lock is None:
            logger.info("素材提报 OSS 清理跳过：其他实例正在执行")
            return 0
        try:
            return self._run_once_unlocked(now_ms)
        finally:
            try:
                distributed_lock.release()
            except Exception:
                logger.exception("素材提报 OSS 清理 Redis 分布式锁释放失败")

    def _run_once_unlocked(self, now_ms: int | None = None) -> int:
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        cutoff_ms = current_ms - self._retention_ms
        submissions = self._repo.list_expired_deleted(cutoff_ms)
        cleaned = 0
        for submission in submissions:
            # 先用 oss_del_flag 原子抢占。恢复接口同样要求 oss_del_flag=0，
            # 因此抢占成功后即使 OSS 只删除了一部分，也不会恢复出损坏记录。
            if not self._repo.mark_oss_deleted(submission.id, by="system"):
                continue
            try:
                keys = submission_asset_keys(
                    submission, self._repo.list_operations(submission.id),
                )
                for key in sorted(keys):
                    self._storage.delete(key)
                self._repo.record_operation(
                    submission.id, "oss_delete", "system",
                    [{"field": "oss_assets", "before": sorted(keys), "after": []}],
                )
                cleaned += 1
            except Exception:
                logger.exception("素材提报 OSS 清理失败，记录保持不可恢复，需人工处理残留 OSS submission_id=%s",
                                 submission.id)
        if cleaned:
            logger.info("素材提报回收站清理完成: %d 条", cleaned)
        return cleaned

    def _next_run_delay(self) -> float:
        now = datetime.now(self._timezone)
        target = now.replace(hour=self._run_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self._next_run_delay()):
            try:
                self.run_once()
            except Exception:
                logger.exception("素材提报回收站定时清理轮次失败")
