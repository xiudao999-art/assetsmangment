"""MVP 三服务单测(闭环③):物料管理/审核/搜索。"""
import pytest
from app.domain.models import MaterialType, AuditStatus, Material
from app.domain.rules import is_available
from app.service.material import MaterialService, MaterialNotFound
from app.service.audit import AuditService
from app.service.search import SearchService
from app.infrastructure.fakes import (
    FakeStorage, InMemoryMaterialRepo, FakeQueryEmbedder,
    FakePassAuditor, FakeBlockAuditor, TimeoutAuditor,
)


def _mat(status=AuditStatus.PASS, oss_key="k", desc=""):
    import uuid
    return Material(uuid.uuid4().hex, MaterialType.IMAGE, f"{oss_key}#t", 0.0,
                    [0.1] * 8, status, "", oss_key, desc)


# ── F1 物料管理 ──
def test_create_stores_and_persists():  # REQ-101
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    svc = MaterialService(repo, storage)
    m = svc.create(MaterialType.IMAGE, "x.png", b"d", "u1")
    assert storage.exists("x.png") and repo.get(m.id) is not None


def test_get_signed_url_time_limited():  # REQ-102
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    svc = MaterialService(repo, storage)
    m = svc.create(MaterialType.IMAGE, "x.png", b"d", "u1")
    assert "Expires" in svc.get_signed_url(m.id)


def test_delete_makes_inaccessible():  # REQ-103
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    svc = MaterialService(repo, storage)
    m = svc.create(MaterialType.IMAGE, "x.png", b"d", "u1")
    svc.delete(m.id)
    with pytest.raises(MaterialNotFound):
        svc.get_signed_url(m.id)


# ── F6 审核 ──
def test_audit_writes_status():  # REQ-501
    repo = InMemoryMaterialRepo(); m = _mat(AuditStatus.REVIEW); repo.save(m)
    AuditService(FakePassAuditor(), repo).run(m)
    assert m.audit_status == AuditStatus.PASS


def test_block_not_downloadable():  # REQ-502
    repo, storage = InMemoryMaterialRepo(), FakeStorage()
    m = _mat(AuditStatus.BLOCK, "b1"); repo.save(m); storage.put("b1")
    with pytest.raises(PermissionError):
        MaterialService(repo, storage).get_download_url(m.id)


def test_audit_timeout_review_not_available():  # REQ-503
    repo = InMemoryMaterialRepo(); m = _mat(AuditStatus.REVIEW); repo.save(m)
    AuditService(TimeoutAuditor(), repo).run(m)
    assert m.audit_status == AuditStatus.REVIEW and not is_available(m)


# ── F3 搜索 ──
def test_search_only_returns_pass():  # REQ-303
    repo = InMemoryMaterialRepo()
    repo.save(_mat(AuditStatus.PASS, "p", "cat"))
    repo.save(_mat(AuditStatus.BLOCK, "b", "cat"))
    results = SearchService(FakeQueryEmbedder(), repo).search("cat")
    assert all(m.audit_status == AuditStatus.PASS for m in results)
    assert len(results) == 1


def test_search_hybrid_hits_term():  # REQ-302
    repo = InMemoryMaterialRepo()
    repo.save(_mat(AuditStatus.PASS, "t", "量子霍尔效应"))
    repo.save(_mat(AuditStatus.PASS, "o", "普通"))
    results = SearchService(FakeQueryEmbedder(), repo).search("量子霍尔")
    assert any("量子霍尔" in m.description for m in results)
