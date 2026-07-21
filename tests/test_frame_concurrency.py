"""性能优化 A:视频帧 OSS 截图 + Qwen-VL 反解并发跑 → 单条时延从 N 帧串行压到 ~1 帧(闭环③)。"""
import time
from app.service.audit_pipeline import AuditPipelineService
from app.domain.models import MaterialType, TextSourceType
from app.infrastructure.fakes import (
    FakeStorage, InMemoryMaterialRepo, FakeEmbedder, InMemoryVectorIndex,
    InMemoryAuditRuleRepo, InMemoryAuditReportRepo, FakeLlm, FakePassAuditor,
)


class _SlowVision:
    def __init__(self, delay=0.2):
        self.delay = delay
        self.calls = 0

    def describe_image(self, url, hints: str = ""):
        self.calls += 1
        time.sleep(self.delay)
        return "画面描述"


class _EmptyTranscriber:
    def transcribe(self, url):
        return []


class _Storage3min(FakeStorage):
    def video_duration_ms(self, oss_key):
        return 180000   # 3 分钟 → 安全网抽 9 帧


def test_video_frames_run_concurrently():
    vision = _SlowVision(delay=0.2)
    svc = AuditPipelineService(_EmptyTranscriber(), vision, FakeLlm(),
                               InMemoryAuditRuleRepo(), InMemoryAuditReportRepo(),
                               _Storage3min(), InMemoryMaterialRepo(), FakeEmbedder(),
                               InMemoryVectorIndex(), FakePassAuditor())
    job = svc.submit(MaterialType.VIDEO, oss_key="v/x.mp4", owner_id="u1", video_kind="work")
    t0 = time.time()
    report = svc.run(job)
    elapsed = time.time() - t0
    assert vision.calls >= 8                       # 3 分钟安全网 ≈ 9 帧,确实抽了多帧
    # 串行需 ≈ 9×0.2=1.8s;并发(≤5)≈ ceil(9/5)×0.2=0.4s。断言远小于串行时长 = 帧确实并发
    assert elapsed < 1.0, f"帧未并发?elapsed={elapsed:.2f}s(串行约 1.8s)"
    assert any(s.source_type == TextSourceType.VIDEO_FRAME for s in report.segments)
