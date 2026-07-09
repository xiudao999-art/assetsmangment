"""F4 索引 / F7 用户 / F8 权限 单测(闭环③)。"""
import pytest
from app.domain.models import Material, MaterialType, AuditStatus, User
from app.service.indexing import IndexService
from app.service.user import UserService, InvalidCredentials
from app.service.authorization import AuthorizationService, PermissionDenied
from app.infrastructure.fakes import (
    InMemoryVectorIndex, InMemoryUserRepo, FakeHasher, FakeTokenIssuer,
    InMemoryRbac, ListAuditLog,
)


# ── F4 索引 ──
def test_index_material_increments():  # REQ-402
    idx = InMemoryVectorIndex()
    m = Material("m1", MaterialType.IMAGE, "t", 0.0, [0.1] * 8, AuditStatus.PASS, "", "k")
    IndexService(idx).index_material(m)
    assert idx.size() == 1 and "m1" in idx.query([0.1] * 8)


# ── F7 用户 ──
def test_password_hashed_not_plain():  # REQ-602
    repo = InMemoryUserRepo()
    UserService(repo, FakeHasher(), FakeTokenIssuer()).register("bob", "plainpw")
    u = repo.get_by_name("bob")
    assert u.pwd_hash != "plainpw" and "plainpw" not in u.pwd_hash


def test_login_issues_token():  # REQ-601
    svc = UserService(InMemoryUserRepo(), FakeHasher(), FakeTokenIssuer())
    svc.register("alice", "pw123456")
    assert "exp" in svc.login("alice", "pw123456")


def test_login_wrong_password_rejected():
    svc = UserService(InMemoryUserRepo(), FakeHasher(), FakeTokenIssuer())
    svc.register("alice", "pw123456")
    with pytest.raises(InvalidCredentials):
        svc.login("alice", "wrong")


# ── F8 权限 ──
def test_no_permission_denied_and_audited():  # REQ-701
    rbac, audit = InMemoryRbac(), ListAuditLog()
    authz = AuthorizationService(rbac, audit)
    user = User("u1", "u1", "x", role="viewer")
    with pytest.raises(PermissionDenied) as e:
        authz.authorize(user, "materials.delete")
    assert e.value.code == 403 and audit.events


def test_grant_takes_effect_immediately():  # REQ-702
    rbac, audit = InMemoryRbac(), ListAuditLog()
    authz = AuthorizationService(rbac, audit)
    user = User("u2", "u2", "x", role="editor")
    authz.grant("editor", "materials.edit")
    authz.authorize(user, "materials.edit")  # 不抛 = 即时生效
