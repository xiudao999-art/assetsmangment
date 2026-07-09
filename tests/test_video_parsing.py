"""视频反解服务单测(闭环③,REQ-201/202/204)。"""
from app.service.video_parsing import VideoParsingService
from app.domain.models import AuditStatus
from app.infrastructure.fakes import (
    FakeVideoParser, FakeEmbedder, FakePassAuditor, TimeoutAuditor,
    InMemoryMaterialRepo, FakeStorage,
)


def _svc(auditor):
    storage = FakeStorage()
    storage.put("v.mp4")
    return VideoParsingService(
        FakeVideoParser(), FakeEmbedder(), auditor, InMemoryMaterialRepo(), storage
    ), storage


def test_reverse_parse_produces_audited_materials_with_vector():
    """REQ-202:产出≥1 物料,每条带向量与审核结果。"""
    svc, _ = _svc(FakePassAuditor())
    job = svc.accept_upload("v.mp4", 50 * 1024 * 1024)
    mats = svc.run_job(job)
    assert len(mats) >= 1
    assert all(m.embedding for m in mats)
    assert all(m.audit_status == AuditStatus.PASS for m in mats)


def test_audit_timeout_marks_review_and_keeps_video():
    """REQ-204/503:审核超时→review(不放行),原视频保留。"""
    svc, storage = _svc(TimeoutAuditor())
    job = svc.accept_upload("v.mp4", 1024)
    mats = svc.run_job(job)
    assert all(m.audit_status == AuditStatus.REVIEW for m in mats)
    assert storage.exists("v.mp4")  # 原视频保留
