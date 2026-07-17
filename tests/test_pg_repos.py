"""PG 仓储集成测试 —— 覆盖全部 9 个新 PG repo。
每个测试用一次性表 <name>_test_<hex>,结束后 DROP。DSN 为空/占位自动跳过。"""
import uuid

import pytest

from app.config import settings

_PLACEHOLDER = settings.database_url.startswith("postgresql://user:pass@localhost")

pytestmark = pytest.mark.skipif(
    not settings.database_url or _PLACEHOLDER,
    reason="AM_DATABASE_URL 未配置真实 PG,跳过集成测试",
)


def _conn():
    import psycopg
    return psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10,
                           options="-c timezone=Asia/Shanghai")


def _drop(table: str) -> None:
    with _conn() as c:
        c.execute(f"DROP TABLE IF EXISTS {table}")


# ══════════════════════ WhitelistRepo ══════════════════════

@pytest.fixture()
def whitelist_repo_table():
    from app.infrastructure.pg_whitelist_repo import PgWhitelistRepo
    table = f"content_whitelist_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgWhitelistRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_whitelist_add_list_words(whitelist_repo_table):
    repo, _ = whitelist_repo_table
    repo.add("测试词A")
    repo.add("测试词B")
    repo.add("")  # 空词不写入
    assert "测试词A" in repo.words()
    assert "测试词B" in repo.words()
    assert sorted(repo.list()) == sorted(["测试词A", "测试词B"])


def test_whitelist_remove(whitelist_repo_table):
    repo, _ = whitelist_repo_table
    repo.add("待删词")
    assert "待删词" in repo.words()
    repo.remove("待删词")
    assert "待删词" not in repo.words()


def test_whitelist_idempotent_add(whitelist_repo_table):
    repo, _ = whitelist_repo_table
    repo.add("幂等词")
    repo.add("幂等词")
    assert len(repo.list()) == 1


# ══════════════════════ BlockwordRepo ══════════════════════

@pytest.fixture()
def blockword_repo_table():
    from app.infrastructure.pg_blockword_repo import PgBlockwordRepo
    table = f"blockword_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgBlockwordRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_blockword_add_list_words(blockword_repo_table):
    repo, _ = blockword_repo_table
    repo.add("禁词X")
    repo.add("禁词Y")
    assert "禁词X" in repo.words()
    assert "禁词Y" in repo.words()


def test_blockword_remove(blockword_repo_table):
    repo, _ = blockword_repo_table
    repo.add("要删的禁词")
    repo.remove("要删的禁词")
    assert "要删的禁词" not in repo.words()


# ══════════════════════ AuditLog ══════════════════════

@pytest.fixture()
def audit_log_table():
    from app.infrastructure.pg_audit_log import PgAuditLog
    table = f"audit_log_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgAuditLog(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_audit_log_record(audit_log_table):
    repo, table = audit_log_table
    repo.record("用户 admin 登录")
    repo.record("权限变更: admin grant materials.audit")
    with _conn() as c:
        count = c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    assert count == 2


# ══════════════════════ FavoriteRepo ══════════════════════

@pytest.fixture()
def favorite_repo_table():
    from app.infrastructure.pg_favorite_repo import PgFavoriteRepo
    table = f"user_favorite_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgFavoriteRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_favorite_add_has(favorite_repo_table):
    repo, _ = favorite_repo_table
    repo.add("u1", "m1")
    repo.add("u1", "m2")
    assert repo.has("u1", "m1")
    assert repo.has("u1", "m2")
    assert not repo.has("u1", "m99")
    assert not repo.has("u2", "m1")


def test_favorite_material_ids(favorite_repo_table):
    repo, _ = favorite_repo_table
    repo.add("u1", "m_a")
    repo.add("u1", "m_b")
    repo.add("u2", "m_c")
    assert repo.material_ids("u1") == {"m_a", "m_b"}
    assert repo.material_ids("u2") == {"m_c"}


def test_favorite_remove(favorite_repo_table):
    repo, _ = favorite_repo_table
    repo.add("u1", "m_rm")
    assert repo.has("u1", "m_rm")
    repo.remove("u1", "m_rm")
    assert not repo.has("u1", "m_rm")


def test_favorite_idempotent_add(favorite_repo_table):
    repo, _ = favorite_repo_table
    repo.add("u1", "m_dup")
    repo.add("u1", "m_dup")
    assert len(repo.material_ids("u1")) == 1


# ══════════════════════ UserRepo ══════════════════════

@pytest.fixture()
def user_repo_table():
    from app.infrastructure.pg_user_repo import PgUserRepo
    table = f"app_user_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgUserRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_user_save_get(user_repo_table):
    from app.domain.models import User
    repo, _ = user_repo_table
    u = User(id="uid-001", name="testuser", pwd_hash="hash123", role="admin", status="active")
    repo.save(u)
    got = repo.get("uid-001")
    assert got is not None
    assert got.name == "testuser"
    assert got.id == "uid-001"
    assert got.role == "admin"


def test_user_get_by_name(user_repo_table):
    from app.domain.models import User
    repo, _ = user_repo_table
    u = User(id="uuid-alice-123", name="alice", pwd_hash="h", role="user")
    repo.save(u)
    got = repo.get_by_name("alice")
    assert got is not None
    assert got.name == "alice"
    assert got.id == "uuid-alice-123"  # domain id ≠ name


def test_user_list(user_repo_table):
    from app.domain.models import User
    repo, _ = user_repo_table
    repo.save(User(id="id-a", name="a", pwd_hash="h", role="viewer"))
    repo.save(User(id="id-b", name="b", pwd_hash="h", role="user"))
    assert len(repo.list()) == 2


def test_user_delete(user_repo_table):
    from app.domain.models import User
    repo, _ = user_repo_table
    repo.save(User(id="delme-uid", name="delme", pwd_hash="h"))
    assert repo.get("delme-uid") is not None
    repo.delete("delme-uid")
    assert repo.get("delme-uid") is None


def test_user_upsert(user_repo_table):
    from app.domain.models import User
    repo, _ = user_repo_table
    u = User(id="upsert-id", name="upsert-name", pwd_hash="h1", role="viewer")
    repo.save(u)
    u2 = User(id="upsert-id", name="upsert-name-new", pwd_hash="h2", role="admin")
    repo.save(u2)
    got = repo.get("upsert-id")
    assert got.pwd_hash == "h2"
    assert got.role == "admin"


# ══════════════════════ RbacRepo ══════════════════════

@pytest.fixture()
def rbac_repo_tables():
    from app.infrastructure.pg_rbac_repo import PgRbacRepo
    rt = f"role_permission_test_{uuid.uuid4().hex[:12]}"
    ut = f"user_permission_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgRbacRepo(settings.database_url, role_table=rt, user_table=ut)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, rt, ut
    _drop(rt)
    _drop(ut)


def test_rbac_grant_permissions_of(rbac_repo_tables):
    repo, _, _ = rbac_repo_tables
    repo.grant("admin", "materials.audit")
    repo.grant("admin", "materials.publish")
    repo.grant("user", "library.read")
    assert repo.permissions_of("admin") == {"materials.audit", "materials.publish"}
    assert repo.permissions_of("user") == {"library.read"}
    assert repo.permissions_of("guest") == set()


def test_rbac_revoke(rbac_repo_tables):
    repo, _, _ = rbac_repo_tables
    repo.grant("admin", "perm1")
    repo.grant("admin", "perm2")
    repo.revoke("admin", "perm1")
    assert repo.permissions_of("admin") == {"perm2"}


def test_rbac_user_permissions(rbac_repo_tables):
    repo, _, _ = rbac_repo_tables
    repo.set_user_permissions("u1", {"extra.perm1", "extra.perm2"})
    assert repo.user_permissions("u1") == {"extra.perm1", "extra.perm2"}


def test_rbac_set_user_permissions_replace(rbac_repo_tables):
    repo, _, _ = rbac_repo_tables
    repo.set_user_permissions("u1", {"old.perm"})
    repo.set_user_permissions("u1", {"new.perm"})
    assert repo.user_permissions("u1") == {"new.perm"}


# ══════════════════════ AuditTaskRepo ══════════════════════

@pytest.fixture()
def task_repo_table():
    from app.infrastructure.pg_task_repo import PgAuditTaskRepo
    table = f"audit_task_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgAuditTaskRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_task_save_get(task_repo_table):
    from app.domain.models import AuditTask, MaterialType, JobStatus
    from app.infrastructure.snowflake import next_id_str
    repo, _ = task_repo_table
    tid = next_id_str()
    t = AuditTask(id=tid, owner_id="u1", name="测试任务", material_type=MaterialType.IMAGE,
                  status=JobStatus.PENDING, created_ms=123456)
    repo.save(t)
    got = repo.get(tid)
    assert got is not None
    assert got.name == "测试任务"
    assert got.status == JobStatus.PENDING


def test_task_list_for(task_repo_table):
    from app.domain.models import AuditTask, MaterialType
    from app.infrastructure.snowflake import next_id_str
    repo, _ = task_repo_table
    t1 = AuditTask(id=next_id_str(), owner_id="u1", name="任务1", material_type=MaterialType.IMAGE,
                   created_ms=100)
    t2 = AuditTask(id=next_id_str(), owner_id="u2", name="任务2", material_type=MaterialType.VIDEO,
                   created_ms=200)
    t3 = AuditTask(id=next_id_str(), owner_id="u1", name="任务3", material_type=MaterialType.IMAGE,
                   created_ms=300)
    repo.save(t1); repo.save(t2); repo.save(t3)
    assert len(repo.list_for("u1")) == 2
    assert len(repo.list_for("u2")) == 1


def test_task_list_all(task_repo_table):
    from app.domain.models import AuditTask, MaterialType
    from app.infrastructure.snowflake import next_id_str
    repo, _ = task_repo_table
    t1 = AuditTask(id=next_id_str(), owner_id="u1", name="t1", material_type=MaterialType.IMAGE,
                   created_ms=1)
    t2 = AuditTask(id=next_id_str(), owner_id="u2", name="t2", material_type=MaterialType.IMAGE,
                   created_ms=2)
    repo.save(t1); repo.save(t2)
    assert len(repo.list_all()) == 2


def test_task_delete(task_repo_table):
    from app.domain.models import AuditTask, MaterialType
    from app.infrastructure.snowflake import next_id_str
    repo, _ = task_repo_table
    tid = next_id_str()
    t = AuditTask(id=tid, owner_id="u1", name="删我", material_type=MaterialType.IMAGE)
    repo.save(t)
    repo.delete(tid)
    assert repo.get(tid) is None


def test_task_status_update(task_repo_table):
    from app.domain.models import AuditTask, MaterialType, JobStatus
    from app.infrastructure.snowflake import next_id_str
    repo, _ = task_repo_table
    tid = next_id_str()
    t = AuditTask(id=tid, owner_id="u1", name="状态更新", material_type=MaterialType.IMAGE,
                  status=JobStatus.PENDING)
    repo.save(t)
    t2 = AuditTask(id=tid, owner_id="u1", name="状态更新", material_type=MaterialType.IMAGE,
                   status=JobStatus.DONE, verdict="pass")
    repo.save(t2)
    got = repo.get(tid)
    assert got.status == JobStatus.DONE
    assert got.verdict == "pass"


# ══════════════════════ AuditReportRepo ══════════════════════

@pytest.fixture()
def report_repo_table():
    from app.infrastructure.pg_report_repo import PgAuditReportRepo
    table = f"audit_report_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgAuditReportRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def test_report_save_get(report_repo_table):
    from app.domain.models import AuditReport, AuditStatus, TextSegment, TextSourceType
    repo, _ = report_repo_table
    seg = TextSegment(source_type=TextSourceType.TRANSCRIPT, text="测试文字", begin_ms=0, end_ms=1000)
    r = AuditReport(verdict=AuditStatus.PASS, segments=[seg],
                    triggered=[{"rule_id": "1", "action": "pass"}],
                    summary="无问题")
    repo.save("rpt-001", r)
    got = repo.get("rpt-001")
    assert got is not None
    assert got.verdict == AuditStatus.PASS
    assert len(got.segments) == 1
    assert got.segments[0].text == "测试文字"


def test_report_get_missing(report_repo_table):
    repo, _ = report_repo_table
    assert repo.get("nonexistent") is None


# ══════════════════════ MaterialRepo ══════════════════════

@pytest.fixture()
def material_repo_table():
    from app.infrastructure.pg_material_repo import PgMaterialRepo
    table = f"material_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgMaterialRepo(settings.database_url, table=table)
    except Exception as e:
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    _drop(table)


def _make_mat(**kw) -> "Material":
    from app.domain.models import Material, MaterialType, AuditStatus
    from app.infrastructure.snowflake import next_id_str
    import time
    base = dict(
        id=next_id_str(), type=MaterialType.IMAGE, thumb="t.jpg",
        source_timecode=0.0, embedding=[], audit_status=AuditStatus.PASS,
        source_job="j1", oss_key="oss/t.jpg", description="测试图片",
        owner_id="u1", is_public=True, audit_report_id="",
        content_hash="abc123", project_id="p1",
        tags=["风景", "旅行"], ai_summary="一张风景照", ai_scenarios=["旅行记忆"],
        ai_emotions=["温暖"], ai_atmosphere="宁静",
        reject_events=[],
    )
    base.update(kw)
    return Material(**base)


def test_material_save_get(material_repo_table):
    repo, _ = material_repo_table
    m = _make_mat()
    repo.save(m)
    got = repo.get(m.id)
    assert got is not None
    assert got.type.value == "image"
    assert got.thumb == "t.jpg"
    assert got.description == "测试图片"
    assert got.tags == ["风景", "旅行"]
    assert got.ai_emotions == ["温暖"]


def test_material_delete(material_repo_table):
    repo, _ = material_repo_table
    m = _make_mat()
    repo.save(m)
    repo.delete(m.id)
    assert repo.get(m.id) is None


def test_material_list(material_repo_table):
    repo, _ = material_repo_table
    m1 = _make_mat(description="物料1", content_hash="h1")
    m2 = _make_mat(description="物料2", content_hash="h2")
    repo.save(m1); repo.save(m2)
    assert len(repo.list()) == 2


def test_material_by_content_hash(material_repo_table):
    repo, _ = material_repo_table
    m = _make_mat(owner_id="u99", content_hash="unique_hash_xyz")
    repo.save(m)
    got = repo.by_content_hash("u99", "unique_hash_xyz")
    assert got is not None
    assert got.content_hash == "unique_hash_xyz"
    # 不同 owner 不同 hash
    assert repo.by_content_hash("u99", "nonexistent") is None
    assert repo.by_content_hash("other", "unique_hash_xyz") is None


def test_material_search(material_repo_table):
    from app.domain.models import AuditStatus as _AS
    repo, _ = material_repo_table
    m1 = _make_mat(description="包含关键字的物料", ai_summary="这是一张风景图",
                   tags=["风景"], owner_id="u1")
    m2 = _make_mat(description="无关物料", ai_summary="其他内容",
                   tags=["其他"], owner_id="u1")
    m3 = _make_mat(description="也包含风景的图", ai_summary="关键字再次出现",
                   tags=[], owner_id="u1")
    m1.audit_status = _AS.PASS
    m2.audit_status = _AS.PASS
    m3.audit_status = _AS.BLOCK
    repo.save(m1); repo.save(m2); repo.save(m3)
    results = repo.search("关键字", only_pass=True)
    assert len(results) == 1  # m2 description 不含关键字,m3 是 block
    assert results[0].description == "包含关键字的物料"


def test_material_query_pagination(material_repo_table):
    from app.domain.query import MaterialQuery
    repo, _ = material_repo_table
    for i in range(5):
        m = _make_mat(description=f"物料{i}", content_hash=f"h{i}", owner_id="u_pag")
        repo.save(m)
    spec = MaterialQuery(owner_id="u_pag", offset=0, limit=3)
    page, total = repo.query(spec)
    assert total == 5
    assert len(page) == 3


def test_material_query_status_filter(material_repo_table):
    from app.domain.query import MaterialQuery
    from app.domain.models import AuditStatus
    repo, _ = material_repo_table
    m1 = _make_mat(description="pass 物料", owner_id="u1", content_hash="hp")
    m1.audit_status = AuditStatus.PASS
    m2 = _make_mat(description="block 物料", owner_id="u1", content_hash="hb")
    m2.audit_status = AuditStatus.BLOCK
    repo.save(m1); repo.save(m2)
    spec = MaterialQuery(status="pass")
    _, total = repo.query(spec)
    assert total == 1


def test_material_query_type_tag_filter(material_repo_table):
    from app.domain.query import MaterialQuery
    repo, _ = material_repo_table
    m1 = _make_mat(type="image" if hasattr(_make_mat, "__kwdefaults__") else _make_mat().type,
                   description="图片", tags=["风景"], owner_id="u1", content_hash="h_i")
    # 用 MaterialType
    from app.domain.models import MaterialType
    m1.type = MaterialType.IMAGE
    m1.tags = ["风景"]
    m2 = _make_mat(description="视频", tags=["搞笑"], owner_id="u1", content_hash="h_v")
    m2.type = MaterialType.VIDEO
    m2.tags = ["搞笑"]
    repo.save(m1); repo.save(m2)
    spec = MaterialQuery(type="image")
    _, total = repo.query(spec)
    assert total == 1
    spec2 = MaterialQuery(tag="风景")
    _, total2 = repo.query(spec2)
    assert total2 == 1


def test_material_search_without_query(material_repo_table):
    """空查询应返回空列表"""
    repo, _ = material_repo_table
    results = repo.search("", only_pass=False)
    assert results == []
