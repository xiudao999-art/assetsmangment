"""测试隔离:即使本地 .env 配了真 OSS / DashScope,测试也强制用假实现(绝不触真实云端)。"""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_storage(monkeypatch):
    from app.api import deps
    from app.infrastructure.fakes import (
        FakeStorage, InMemoryMaterialRepo, FakeVideoParser, FakeEmbedder,
        FakeQueryEmbedder, FakePassAuditor,
    )
    monkeypatch.setattr(deps, "storage", FakeStorage())
    monkeypatch.setattr(deps, "material_repo", InMemoryMaterialRepo())
    # AI 适配器:强制假实现,单测不发真云请求(否则慢且依赖网络/额度)
    monkeypatch.setattr(deps, "_video_parser", FakeVideoParser())
    monkeypatch.setattr(deps, "_embedder", FakeEmbedder())
    monkeypatch.setattr(deps, "_query_embedder", FakeQueryEmbedder())
    monkeypatch.setattr(deps, "_auditor", FakePassAuditor())
