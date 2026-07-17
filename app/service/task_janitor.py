"""Task janitor service: startup recovery + runtime compensation for stuck audit tasks.

Two-phase protection:
  1. Startup recovery (sync): resolve ALL PENDING/RUNNING tasks on boot.
     If the material was already audited (audit_status != processing), the
     task is marked DONE — the audit completed, only the task status write
     was lost.  Otherwise the task is FAILED and its unusable material cleaned up.
  2. Runtime compensation (daemon thread): periodically scan for tasks stuck
     in PENDING/RUNNING longer than `stuck_timeout_s`, resolve them same as above.

Depends only on domain ports — zero FastAPI / router coupling.
"""
from __future__ import annotations
import threading
import time
import logging
from typing import Optional

from app.domain.models import JobStatus, AuditStatus, AuditTask
from app.domain.ports import AuditTaskRepo, MaterialRepo, ObjectStorage

logger = logging.getLogger(__name__)

_RECOVERY_ERROR = "服务重启中断,请重新提交"
_TIMEOUT_ERROR  = "审核超时,请重新提交"


class TaskJanitor:
    """Periodically fail stuck audit tasks and clean up orphaned materials."""

    def __init__(
        self,
        task_repo: AuditTaskRepo,
        material_repo: MaterialRepo,
        storage: ObjectStorage,
        scan_interval_s: int = 300,
        stuck_timeout_s: int = 1800,
    ) -> None:
        self._task_repo = task_repo
        self._material_repo = material_repo
        self._storage = storage
        self._scan_interval_s = max(1, scan_interval_s)   # floor: 1s (防 busy-loop)
        self._stuck_timeout_s = stuck_timeout_s
        self._thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None

    # ── public API (called from FastAPI lifespan) ──

    def start(self) -> None:
        """Startup: run recovery synchronously, then launch the daemon scanner."""
        self._recover_on_startup()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="task-janitor",
        )
        self._thread.start()
        logger.info(
            "TaskJanitor started (scan_interval=%ds, stuck_timeout=%ds)",
            self._scan_interval_s, self._stuck_timeout_s,
        )

    def stop(self) -> None:
        """Shutdown: signal the background thread and wait (max 5s)."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("TaskJanitor stopped")

    # ── startup recovery ──

    def _recover_on_startup(self) -> int:
        """Fail every task still in PENDING or RUNNING — the previous process
        that owned them is gone (server restart / crash / deploy)."""
        try:
            stuck = [
                t for t in self._task_repo.list_all()
                if t.status in (JobStatus.PENDING, JobStatus.RUNNING)
            ]
        except Exception:
            logger.exception("TaskJanitor startup recovery: list_all failed")
            return 0

        count = 0
        for task in stuck:
            try:
                # Re-read in case another instance already resolved it
                # (multi-instance deploy — both boot and run recovery concurrently).
                current = self._task_repo.get(task.id)
                if current is None:
                    continue
                if current.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                    continue
                self._resolve_stuck_task(current, _RECOVERY_ERROR)
                count += 1
            except Exception:
                logger.exception("TaskJanitor startup recovery: resolve task %s", task.id)
        if count:
            logger.info("TaskJanitor startup recovery: resolved %d stuck tasks", count)
        return count

    # ── runtime compensation ──

    def _scan_loop(self) -> None:
        """Background daemon: periodically scan for stuck tasks and fail them.
        Exits when `_stop_event` is set (app shutdown)."""
        while not self._stop_event.is_set():
            try:
                self._scan_and_fail()
            except Exception:
                logger.exception("TaskJanitor scan cycle failed (will retry)")
            self._stop_event.wait(self._scan_interval_s)

    def _scan_and_fail(self) -> int:
        """One scan cycle: find tasks stuck longer than `_stuck_timeout_s`,
        fail them, and clean up their materials."""
        cutoff_ms = int(time.time() * 1000) - self._stuck_timeout_s * 1000
        try:
            stuck = [
                t for t in self._task_repo.list_all()
                if t.status in (JobStatus.PENDING, JobStatus.RUNNING)
                and t.created_ms > 0                     # 跳过未初始化时间戳的任务
                and t.created_ms < cutoff_ms
            ]
        except Exception:
            logger.exception("TaskJanitor scan: list_all failed")
            return 0

        count = 0
        for task in stuck:
            try:
                # Re-read before mutating — the audit pool may have finished
                # the task between our list_all and now (race guard).
                current = self._task_repo.get(task.id)
                if current is None:
                    continue
                if current.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                    continue
                self._resolve_stuck_task(current, _TIMEOUT_ERROR)
                count += 1
            except Exception:
                logger.exception("TaskJanitor scan: resolve task %s", task.id)
        if count:
            logger.info("TaskJanitor scan: resolved %d stuck tasks", count)
        return count

    # ── helpers ──

    def _resolve_stuck_task(self, task: AuditTask, error: str) -> None:
        """Resolve a stuck task by checking whether the audit actually completed.

        Two paths:
        1. Material exists AND audit_status != PROCESSING → audit completed
           but task was never marked DONE (e.g. crash between _persist and
           _finish_task).  Mark DONE, sync verdict/report from material, do
           NOT delete the material.
        2. Material missing or still PROCESSING → audit genuinely never
           finished.  Mark FAILED + downgrade material to REVIEW (keep it for retry).
        """
        m = None
        if task.material_id:
            try:
                m = self._material_repo.get(task.material_id)
            except Exception:
                pass

        if m is not None and m.audit_status != AuditStatus.PROCESSING:
            # Audit completed — just the task status write was lost.
            task.status = JobStatus.DONE
            task.verdict = m.audit_status.value
            task.report_id = getattr(m, "audit_report_id", "") or ""
            task.error = ""
        else:
            # Audit genuinely never finished — keep material for retry.
            task.status = JobStatus.FAILED
            task.error = error[:200]
            if task.material_id:
                self._downgrade_material(task.material_id)

        self._task_repo.save(task)

    def _downgrade_material(self, material_id: str) -> None:
        """Mark a material as REVIEW so it can be retried. Best-effort —
        never raises, because the janitor must not crash on cleanup failures."""
        try:
            m = self._material_repo.get(material_id)
        except Exception:
            return
        if m is None:
            return
        if m.audit_status == AuditStatus.PROCESSING:
            try:
                m.audit_status = AuditStatus.REVIEW
                self._material_repo.save(m)
            except Exception:
                pass
