"""迁移脚本核心 migrate():state.json.bak 的 rules → 规则仓储;以 no 幂等。
用 InMemory repo + tmp_path,无需 PG(PG 行为已由 test_pg_rule_repo 覆盖)。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from migrate_rules_to_pg import migrate            # noqa: E402
from app.infrastructure.fakes import InMemoryAuditRuleRepo   # noqa: E402


def _write_state(tmp_path, payload: dict) -> str:
    p = tmp_path / "state.json.bak"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(p)


_R1 = {"id": "ca2362010fd8443b95fbbb26e1a74e0f", "source_type": "any", "no": 1,
       "keywords": ["苹果"], "condition": "水果隐喻", "action": "review", "enabled": True,
       "created_by": "admin", "project_id": "", "guidance": "g", "match_level": "regex",
       "regex": "苹果", "exceptions": [{"text": "苹果好吃", "note": "", "by": "u1", "ms": 1}]}
# 老数据缺新字段(guidance/match_level/regex/exceptions)→ 应走 dataclass 默认值
_R2 = {"id": "beef", "source_type": "lyrics", "no": 2, "keywords": [], "condition": "c2",
       "action": "block", "enabled": False, "created_by": "u2", "project_id": "p1"}


def test_migrate_two_rules(tmp_path):
    repo = InMemoryAuditRuleRepo()
    inserted, skipped = migrate(_write_state(tmp_path, {"rules": [_R1, _R2]}), repo)
    assert (inserted, skipped) == (2, 0)
    rules = sorted(repo.list(), key=lambda r: r.no)
    assert [r.no for r in rules] == [1, 2]
    assert all(r.id.isdigit() for r in rules)      # 换发雪花 id,旧 uuid hex 弃用
    assert rules[0].keywords == ["苹果"] and rules[0].regex == "苹果"
    assert rules[0].exceptions == [{"text": "苹果好吃", "note": "", "by": "u1", "ms": 1}]
    assert rules[0].created_by == "admin"          # created_by 原样保留
    assert rules[1].enabled is False and rules[1].project_id == "p1"
    assert rules[1].match_level == "metaphor" and rules[1].exceptions == []


def test_migrate_idempotent_by_no(tmp_path):
    repo = InMemoryAuditRuleRepo()
    path = _write_state(tmp_path, {"rules": [_R1, _R2]})
    migrate(path, repo)
    inserted, skipped = migrate(path, repo)        # 重跑:同 no 在用 → 全跳过
    assert (inserted, skipped) == (0, 2)
    assert len(repo.list()) == 2


def test_migrate_no_rules_key(tmp_path):
    repo = InMemoryAuditRuleRepo()
    assert migrate(_write_state(tmp_path, {}), repo) == (0, 0)
    assert repo.list() == []
