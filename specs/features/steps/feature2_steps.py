"""behave step 实现:大量索引(REQ-4xx)/用户管理(REQ-6xx)/权限后台(REQ-7xx)。"""
import time
from behave import given, when, then  # type: ignore
from app.domain.models import Material, MaterialType, AuditStatus, User
from app.service.indexing import IndexService
from app.service.user import UserService
from app.service.authorization import AuthorizationService, PermissionDenied
from app.infrastructure.fakes import (
    InMemoryVectorIndex, InMemoryUserRepo, FakeHasher, FakeTokenIssuer,
    InMemoryRbac, ListAuditLog,
)


# ══ F4 大量索引 ══
@given("有一个新入库的物料")
def g_new_indexed(context):
    context.index = InMemoryVectorIndex()
    context.idx_svc = IndexService(context.index)
    context.mat = Material(
        id="m1", type=MaterialType.IMAGE, thumb="t", source_timecode=0.0,
        embedding=[0.1] * 8, audit_status=AuditStatus.PASS, source_job="", oss_key="k",
    )


@when("系统建立索引")
def w_build_index(context):
    context.idx_svc.index_material(context.mat)


@then("向量索引应包含该物料")
def t_index_contains(context):
    assert context.index.size() >= 1
    assert "m1" in context.index.query([0.1] * 8, k=10)


@given("索引中已有大量物料")
def g_many_indexed(context):
    context.index = InMemoryVectorIndex()
    context.idx_svc = IndexService(context.index)
    for i in range(50):
        context.index.add(f"m{i}", [0.1] * 8)


@when("我做向量近邻查询")
def w_vector_query(context):
    t0 = time.time()
    context.results = context.idx_svc.query([0.1] * 8, k=5)
    context.elapsed = time.time() - t0


@then("应通过索引返回近邻")
def t_index_return(context):
    assert len(context.results) >= 1


@then("查询延迟应在预算内")
def t_latency(context):
    assert context.elapsed < 0.2  # 200ms 预算;真实 P95 由 k6 验证


# ══ F7 用户管理 ══
def _user_svc():
    return UserService(InMemoryUserRepo(), FakeHasher(), FakeTokenIssuer())


@given("存在一个注册用户")
def g_registered_user(context):
    context.repo = InMemoryUserRepo()
    context.user_svc = UserService(context.repo, FakeHasher(), FakeTokenIssuer())
    context.user_svc.register("alice", "pw123456")


@when("该用户用正确凭据登录")
def w_login(context):
    context.token = context.user_svc.login("alice", "pw123456")


@then("应签发一个受时限的 token")
def t_token(context):
    assert context.token and "exp" in context.token


@given("我用密码注册")
def g_register_pw(context):
    context.repo = InMemoryUserRepo()
    context.user_svc = UserService(context.repo, FakeHasher(), FakeTokenIssuer())
    context.plain = "myPlainPw"
    context.user = context.user_svc.register("bob", context.plain)


@when("系统存储密码")
def w_store_pw(context):
    context.stored = context.repo.get_by_name("bob")


@then("密码应加盐哈希存储")
def t_hashed(context):
    assert context.stored.pwd_hash and context.stored.pwd_hash != context.plain


@then("不得明文存储")
def t_not_plain(context):
    assert context.plain not in context.stored.pwd_hash


# ══ F8 权限后台 ══
@given("一个用户没有某功能权限")
def g_no_perm(context):
    context.rbac = InMemoryRbac()
    context.audit = ListAuditLog()
    context.authz = AuthorizationService(context.rbac, context.audit)
    context.user = User(id="u1", name="u1", pwd_hash="x", role="viewer")
    context.permission = "materials.delete"


@when("该用户访问该功能")
def w_access(context):
    try:
        context.authz.authorize(context.user, context.permission)
        context.denied = None
    except PermissionDenied as e:
        context.denied = e


@then("系统应拒绝并返回403")
def t_403(context):
    assert context.denied is not None and context.denied.code == 403


@then("应记录审计")
def t_audit(context):
    assert context.audit.events


@given("管理员在后台给角色新增了一个权限")
def g_admin_grant(context):
    context.rbac = InMemoryRbac()
    context.audit = ListAuditLog()
    context.authz = AuthorizationService(context.rbac, context.audit)
    context.user = User(id="u2", name="u2", pwd_hash="x", role="editor")
    context.permission = "materials.edit"
    context.authz.grant("editor", context.permission)  # 后台新增权限


@when("拥有该角色的用户再次访问")
def w_reaccess(context):
    try:
        context.authz.authorize(context.user, context.permission)
        context.allowed = True
    except PermissionDenied:
        context.allowed = False


@then("系统应即时放行")
def t_allowed(context):
    assert context.allowed is True
