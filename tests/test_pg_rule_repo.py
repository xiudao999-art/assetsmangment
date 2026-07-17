"""PgAuditRuleRepo 集成测试 —— 打真实 dev PG;DSN 为空/占位自动跳过。
每个测试用一次性表 audit_rule_test_<hex>,fixture 结束 DROP(共享状态清理,见 CLAUDE.md)。"""
import time
import uuid
from dataclasses import replace

import pytest

from app.config import settings
from app.domain.models import AuditRule
from app.infrastructure.snowflake import next_id_str

_PLACEHOLDER = settings.database_url.startswith("postgresql://user:pass@localhost")

pytestmark = pytest.mark.skipif(
    not settings.database_url or _PLACEHOLDER,
    reason="AM_DATABASE_URL 未配置真实 PG,跳过集成测试",
)


@pytest.fixture()
def repo_table():
    from app.infrastructure.pg_rule_repo import PgAuditRuleRepo
    table = f"audit_rule_test_{uuid.uuid4().hex[:12]}"
    try:
        repo = PgAuditRuleRepo(settings.database_url, table=table)
    except Exception as e:   # dev PG 不可达(离线等)→ 跳过而非红
        pytest.skip(f"PG 不可达: {e}")
    yield repo, table
    import psycopg
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10) as c:
        c.execute(f"DROP TABLE IF EXISTS {table}")


def _fetch(table: str, rule_id: str) -> dict | None:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10,
                         row_factory=dict_row) as c:
        return c.execute(f"SELECT * FROM {table} WHERE id = %s", (int(rule_id),)).fetchone()


def _count(table: str) -> int:
    import psycopg
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10) as c:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _rule(**kw) -> AuditRule:
    base = dict(
        id="", source_type="lyrics", no=1, keywords=["苹果", "apple"],
        condition="出现水果隐喻", action="review", enabled=True,
        created_by="u1", project_id="p1", guidance="到明确指涉才算,好例:xx;坏例:yy",
        match_level="regex", regex=r"苹果|apple",
        exceptions=[{"text": "苹果好吃", "note": "日常语义", "by": "u9", "ms": 123}],
    )
    base.update(kw)
    if not base["id"]:
        base["id"] = next_id_str()
    return AuditRule(**base)


def test_roundtrip_13_fields(repo_table):
    """13 字段完整 roundtrip:含中文、exceptions JSONB list-of-dicts;id 返回 str。"""
    repo, _ = repo_table
    r = _rule()
    repo.add(r, by="u1")
    got = repo.list()
    assert len(got) == 1
    assert isinstance(got[0].id, str) and got[0].id.isdigit()
    assert got[0] == r   # dataclass 全字段相等


def test_upsert_keeps_create_tracks_update(repo_table):
    """二次 add 同 id → 仍单行;create_by/create_time 不变,update_by/update_time 前移。"""
    repo, table = repo_table
    r = _rule(created_by="u1")
    repo.add(r, by="u1")
    row1 = _fetch(table, r.id)
    time.sleep(0.02)
    repo.add(replace(r, condition="改了"), by="u2")
    assert _count(table) == 1
    row2 = _fetch(table, r.id)
    assert row2["create_by"] == row1["create_by"] == "u1"
    assert row2["create_time"] == row1["create_time"]
    assert row2["update_by"] == "u2"
    assert row2["update_time"] > row1["update_time"]
    assert repo.list()[0].condition == "改了"


def test_soft_delete(repo_table):
    """delete → list 不含;行仍在且 del_flag 为雪花值;二次 delete 无副作用;非数字 id 静默容忍。"""
    repo, table = repo_table
    r = _rule()
    repo.add(r, by="u1")
    repo.delete(r.id, by="u2")
    assert repo.list() == []
    row = _fetch(table, r.id)
    assert row is not None and row["del_flag"] != 0
    flag1, by1 = row["del_flag"], row["update_by"]
    assert by1 == "u2"
    repo.delete(r.id, by="u3")            # WHERE del_flag=0 → 不再命中
    row = _fetch(table, r.id)
    assert (row["del_flag"], row["update_by"]) == (flag1, by1)
    repo.delete("not-a-number", by="u3")  # 旧 uuid/合成 id:对齐 JSON pop(...,None) 容忍


def test_list_for_matches_applies_to(repo_table):
    """list_for 与 domain AuditRule.applies_to 语义一致(全局/项目/停用/来源类型)。"""
    repo, _ = repo_table
    rules = [
        _rule(no=1, source_type="any", project_id=""),
        _rule(no=2, source_type="lyrics", project_id=""),
        _rule(no=3, source_type="lyrics", project_id="p1"),
        _rule(no=4, source_type="lyrics", project_id="p2"),
        _rule(no=5, source_type="lyrics", project_id="", enabled=False),
        _rule(no=6, source_type="title", project_id=""),
    ]
    for r in rules:
        repo.add(r, by="u1")
    got = {r.no for r in repo.list_for("lyrics", "p1")}
    assert got == {1, 2, 3}
    assert got == {r.no for r in rules if r.applies_to("lyrics", "p1")}
    assert [r.no for r in repo.list()] == [1, 2, 3, 4, 5, 6]   # list 按 no 排序


def test_no_reusable_after_soft_delete(repo_table):
    """软删后同 no 可复用:唯一索引是 (no, del_flag),软删行 del_flag≠0 不冲突。"""
    repo, table = repo_table
    a = _rule(no=7)
    repo.add(a, by="u1")
    repo.delete(a.id, by="u1")
    b = _rule(no=7)
    repo.add(b, by="u1")                  # 不撞唯一索引
    assert [r.id for r in repo.list()] == [b.id]
    assert _count(table) == 2             # 软删行仍在


def test_table_name_validated():
    from app.infrastructure.pg_rule_repo import PgAuditRuleRepo
    with pytest.raises(ValueError):
        PgAuditRuleRepo("postgresql://x", table="bad-name; DROP TABLE t")
