"""测试隔离:即使本地 .env 配了真 OSS / DashScope,测试也强制用假实现(绝不触真实云端)。"""
import threading
import time
import pytest


def _drain_audit_threads(timeout: float = 10.0) -> None:
    """排空 /audit/* 端点起的后台审核线程(name='audit-worker')。
    防止上一个测试的后台线程读到本测试的 fakes/monkeypatch(共享 deps 被逐测试替换)导致偶发失败。"""
    deadline = time.time() + timeout
    for t in threading.enumerate():
        if t.name == "audit-worker" and t.is_alive():
            t.join(timeout=max(0.05, deadline - time.time()))


@pytest.fixture(autouse=True)
def _hermetic_storage(monkeypatch):
    from app.api import deps
    from app.infrastructure.fakes import (
        FakeStorage, InMemoryMaterialRepo, FakeVideoParser, FakeEmbedder,
        FakeQueryEmbedder, FakePassAuditor, FakeTranscriber, FakeVisionDescriber,
        FakeLlm, InMemoryAuditRuleRepo, InMemoryAuditReportRepo, InMemoryAuditTaskRepo,
    )
    _drain_audit_threads()   # 起点:上一个测试的后台线程先跑完,别沾本测试的 deps
    monkeypatch.setattr(deps, "storage", FakeStorage())
    monkeypatch.setattr(deps, "material_repo", InMemoryMaterialRepo())
    monkeypatch.setattr(deps, "task_repo", InMemoryAuditTaskRepo())  # 待审核任务:强制内存,不碰真 state.json
    # AI 适配器:强制假实现,单测不发真云请求(否则慢且依赖网络/额度)
    monkeypatch.setattr(deps, "_video_parser", FakeVideoParser())
    monkeypatch.setattr(deps, "_embedder", FakeEmbedder())
    monkeypatch.setattr(deps, "_query_embedder", FakeQueryEmbedder())
    monkeypatch.setattr(deps, "_auditor", FakePassAuditor())
    monkeypatch.setattr(deps, "_llm", FakeLlm())
    monkeypatch.setattr(deps, "_vision", FakeVisionDescriber())
    monkeypatch.setattr(deps, "_transcriber", FakeTranscriber())
    monkeypatch.setattr(deps, "rule_repo", InMemoryAuditRuleRepo())
    monkeypatch.setattr(deps, "report_repo", InMemoryAuditReportRepo())
    yield
    # 收尾:本测试的后台线程跑完再让 monkeypatch 还原(此 teardown 先于 monkeypatch 还原,
    # 线程仍读到本测试的 fakes)→ 既不泄漏到下一个测试,也不会读到被还原的 deps。
    _drain_audit_threads()
