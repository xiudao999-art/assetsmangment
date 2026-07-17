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
        InMemoryProjectRepo, InMemoryBlockwordRepo, InMemoryWhitelistRepo, FakeArchiver, FakeTavily,
        InMemoryUserRepo, InMemoryFavoriteRepo, InMemoryRbac, ListAuditLog,
    )
    _drain_audit_threads()   # 起点:上一个测试的后台线程先跑完,别沾本测试的 deps
    monkeypatch.setattr(deps, "storage", FakeStorage())
    monkeypatch.setattr(deps, "material_repo", InMemoryMaterialRepo())
    monkeypatch.setattr(deps, "task_repo", InMemoryAuditTaskRepo())  # 待审核任务:强制内存,不碰真 state.json.bak
    # AI 适配器:强制假实现,单测不发真云请求(否则慢且依赖网络/额度)
    monkeypatch.setattr(deps, "_video_parser", FakeVideoParser())
    monkeypatch.setattr(deps, "_embedder", FakeEmbedder())
    monkeypatch.setattr(deps, "_query_embedder", FakeQueryEmbedder())
    monkeypatch.setattr(deps, "_auditor", FakePassAuditor())
    monkeypatch.setattr(deps, "_llm", FakeLlm())
    monkeypatch.setattr(deps, "_vision", FakeVisionDescriber())
    monkeypatch.setattr(deps, "_transcriber", FakeTranscriber())
    monkeypatch.setattr(deps, "_archiver", FakeArchiver())   # 物料档案器:强制假实现,不打真 ARK
    monkeypatch.setattr(deps, "_tavily", FakeTavily())       # 联网搜索:强制假实现,不打真 Tavily
    monkeypatch.setattr(deps, "rule_repo", InMemoryAuditRuleRepo())
    monkeypatch.setattr(deps, "report_repo", InMemoryAuditReportRepo())
    monkeypatch.setattr(deps, "project_repo", InMemoryProjectRepo())
    monkeypatch.setattr(deps, "blockword_repo", InMemoryBlockwordRepo())
    monkeypatch.setattr(deps, "whitelist_repo", InMemoryWhitelistRepo())
    monkeypatch.setattr(deps, "user_repo", InMemoryUserRepo())
    monkeypatch.setattr(deps, "favorites", InMemoryFavoriteRepo())
    monkeypatch.setattr(deps, "rbac", InMemoryRbac())
    monkeypatch.setattr(deps, "audit_log", ListAuditLog())
    # ── 种子数据:与 deps.py 播种一致,确保 API 测试的登录/权限可用 ──
    from app.domain.models import User
    from app.infrastructure.fakes import FakeHasher
    _h = FakeHasher()
    deps.user_repo.save(User(id="admin", name="admin", pwd_hash=_h.hash("admin123"), role="admin"))
    deps.user_repo.save(User(id="user01", name="demo", pwd_hash=_h.hash("pw123456"), role="user"))
    ADMIN_PERMS = {"materials.audit", "materials.publish", "materials.delete_any", "library.all", "admin.grant", "audit.rules"}
    for _p in ADMIN_PERMS:
        deps.rbac.grant("admin", _p)
    from app.infrastructure.snowflake import next_id_str
    from app.domain.models import Project
    deps.project_repo.add(Project(id=next_id_str(), name="汽水音乐", created_by="admin",
                                  created_ms=int(time.time() * 1000)))
    yield
    # 收尾:本测试的后台线程跑完再让 monkeypatch 还原(此 teardown 先于 monkeypatch 还原,
    # 线程仍读到本测试的 fakes)→ 既不泄漏到下一个测试,也不会读到被还原的 deps。
    _drain_audit_threads()
