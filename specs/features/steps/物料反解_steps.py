"""behave step 实现(闭环①,REQ-201/202/204)。已从🔴实现为可转🟢。"""
import time
from behave import given, when, then  # type: ignore
from app.service.video_parsing import VideoParsingService
from app.domain.models import AuditStatus
from app.infrastructure.fakes import (
    FakeVideoParser, FakeEmbedder, FakePassAuditor, TimeoutAuditor,
    InMemoryMaterialRepo, FakeStorage,
)


# ── @REQ-201/202:上传视频后自动反解并送审 ──
@given("我上传了一个 50MB 的 mp4 视频")
def step_upload_video(context):
    context.storage = FakeStorage()
    context.storage.put("v.mp4")
    context.svc = VideoParsingService(
        FakeVideoParser(), FakeEmbedder(), FakePassAuditor(),
        InMemoryMaterialRepo(), context.storage,
    )
    t0 = time.time()
    context.job = context.svc.accept_upload("v.mp4", 50 * 1024 * 1024)
    context.accept_elapsed = time.time() - t0


@when("系统调用 Qwen-VL 反解")
def step_parse(context):
    context.materials = context.svc.run_job(context.job)


@then("应生成至少 1 条物料")
def step_at_least_one(context):
    assert len(context.materials) >= 1


@then("每条物料都带有审核结果")
def step_has_audit(context):
    assert all(m.audit_status in AuditStatus for m in context.materials)


@then("受理结果在 10 秒内返回")
def step_within_10s(context):
    assert context.accept_elapsed < 10


# ── @REQ-204:送审失败时不丢弃 ──
@given("一条反解出的物料送审超时")
def step_audit_timeout(context):
    context.storage = FakeStorage()
    context.storage.put("v.mp4")
    context.svc = VideoParsingService(
        FakeVideoParser(), FakeEmbedder(), TimeoutAuditor(),
        InMemoryMaterialRepo(), context.storage,
    )
    context.job = context.svc.accept_upload("v.mp4", 1024)


@when("系统处理该失败")
def step_handle_failure(context):
    context.materials = context.svc.run_job(context.job)


@then("该物料应被标记为「待人工复核」")
def step_mark_review(context):
    assert all(m.audit_status == AuditStatus.REVIEW for m in context.materials)


@then("原视频应被保留")
def step_keep_video(context):
    assert context.storage.exists(context.job.oss_key)
