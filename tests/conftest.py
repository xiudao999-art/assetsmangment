"""测试隔离:即使本地 .env 配了真 OSS,测试也强制用假存储(不触真实云端)。"""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_storage(monkeypatch):
    from app.api import deps
    from app.infrastructure.fakes import FakeStorage, InMemoryMaterialRepo
    monkeypatch.setattr(deps, "storage", FakeStorage())
    monkeypatch.setattr(deps, "material_repo", InMemoryMaterialRepo())
