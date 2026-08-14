from datetime import datetime, timezone

from app.domain.models import MaterialSubmission
from app.infrastructure.fakes import (
    FakeStorage, InMemoryJobCoordinator, InMemoryMaterialSubmissionRepo,
)
from app.infrastructure.snowflake import minimum_id_for_timestamp, next_id_str
from app.task.submission_trash_janitor import SubmissionTrashJanitor, submission_asset_keys


DAY_MS = 24 * 60 * 60 * 1000


def _deleted_submission(repo, now_ms: int, age_days: int = 8,
                        prefix: str = "submissions") -> MaterialSubmission:
    submission = MaterialSubmission(
        id=next_id_str(), oss_key=f"{prefix}/current.mp4",
        decoded_oss_key=f"{prefix}/decoded/current.mp4",
        platform_reject_attachments=[f"{prefix}/current.png"], created_by="user01",
    )
    repo.add(submission, by="user01")
    submission.del_flag = minimum_id_for_timestamp(now_ms - age_days * DAY_MS)
    repo.record_operation(submission.id, "update", "user01", [
        {"field": "oss_key", "before": f"{prefix}/old.mp4", "after": submission.oss_key},
        {"field": "decoded_oss_key", "before": f"{prefix}/decoded/old.mp4",
         "after": submission.decoded_oss_key},
        {"field": "platform_reject_attachments",
         "before": [f"{prefix}/old.png"], "after": submission.platform_reject_attachments},
    ])
    return submission


def test_submission_asset_keys_include_current_and_replaced_files():
    repo = InMemoryMaterialSubmissionRepo()
    now_ms = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    submission = _deleted_submission(repo, now_ms)
    assert submission_asset_keys(submission, repo.list_operations(submission.id)) == {
        "submissions/current.mp4", "submissions/current.png",
        "submissions/decoded/current.mp4", "submissions/decoded/old.mp4",
        "submissions/old.mp4", "submissions/old.png",
    }


def test_janitor_only_cleans_older_than_retention_and_records_operation():
    repo = InMemoryMaterialSubmissionRepo()
    storage = FakeStorage()
    now_ms = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    expired = _deleted_submission(repo, now_ms, age_days=8)
    recent = _deleted_submission(repo, now_ms, age_days=6, prefix="recent")
    for key in ("submissions/current.mp4", "submissions/current.png",
                "submissions/old.mp4", "submissions/old.png"):
        storage.put(key)

    cleaned = SubmissionTrashJanitor(repo, storage, retention_days=7).run_once(now_ms)

    assert cleaned == 1
    assert expired.oss_del_flag != 0
    assert recent.oss_del_flag == 0
    assert not any(storage.exists(key) for key in (
        "submissions/current.mp4", "submissions/current.png",
        "submissions/old.mp4", "submissions/old.png",
    ))
    operations = repo.list_operations(expired.id)
    assert operations[0]["action"] == "oss_delete"
    assert repo.get_deleted(expired.id) is None


def test_janitor_does_not_mark_oss_deleted_when_any_delete_fails():
    class FailingStorage(FakeStorage):
        def delete(self, oss_key: str) -> None:
            if oss_key == "submissions/old.png":
                raise RuntimeError("simulated OSS failure")
            super().delete(oss_key)

    repo = InMemoryMaterialSubmissionRepo()
    storage = FailingStorage()
    now_ms = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    submission = _deleted_submission(repo, now_ms)

    assert SubmissionTrashJanitor(repo, storage, retention_days=7).run_once(now_ms) == 0
    # 抢占后不解锁，防止恢复出部分文件已删除的损坏记录。
    assert submission.oss_del_flag != 0
    assert repo.restore(submission.id, by="user01") is False
    assert all(item["action"] != "oss_delete" for item in repo.list_operations(submission.id))


def test_janitor_claim_blocks_restore_before_first_oss_delete():
    repo = InMemoryMaterialSubmissionRepo()
    now_ms = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    submission = _deleted_submission(repo, now_ms)

    class RestoreDuringDeleteStorage(FakeStorage):
        restore_result = None

        def delete(self, oss_key: str) -> None:
            self.restore_result = repo.restore(submission.id, by="user01")
            super().delete(oss_key)

    storage = RestoreDuringDeleteStorage()
    storage.put(submission.oss_key)

    assert SubmissionTrashJanitor(repo, storage, retention_days=7).run_once(now_ms) == 1
    assert storage.restore_result is False
    assert repo.get(submission.id) is None
    assert submission.oss_del_flag != 0


def test_janitor_skips_when_another_instance_holds_distributed_lock():
    repo = InMemoryMaterialSubmissionRepo()
    storage = FakeStorage()
    coordinator = InMemoryJobCoordinator()
    now_ms = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    submission = _deleted_submission(repo, now_ms)
    held_lock = coordinator.acquire("daily-cleanup", timeout_seconds=3600)
    assert held_lock is not None

    janitor = SubmissionTrashJanitor(
        repo, storage, retention_days=7, lock_coordinator=coordinator,
        require_distributed_lock=True,
    )
    assert janitor.run_once(now_ms) == 0
    assert submission.oss_del_flag == 0

    held_lock.release()
    assert janitor.run_once(now_ms) == 1
    assert submission.oss_del_flag != 0


def test_janitor_fails_closed_when_distributed_lock_is_required_but_missing():
    repo = InMemoryMaterialSubmissionRepo()
    storage = FakeStorage()
    now_ms = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp() * 1000)
    submission = _deleted_submission(repo, now_ms)

    janitor = SubmissionTrashJanitor(
        repo, storage, retention_days=7, require_distributed_lock=True,
    )
    assert janitor.run_once(now_ms) == 0
    assert submission.oss_del_flag == 0
