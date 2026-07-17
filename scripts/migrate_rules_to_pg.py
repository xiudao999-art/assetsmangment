"""一次性:审核规则从 state.json.bak 迁移到 PostgreSQL(audit_rule 表,雪花ID + 软删基础字段)。

用法(repo 根目录):
    PYTHONUTF8=1 .venv\\Scripts\\python scripts\\migrate_rules_to_pg.py

- 幂等键 = no:PG 里已有同 no 的在用规则 → 跳过(重跑安全)。
- 每条换发新雪花 id(旧 uuid hex 与 BIGINT 主键不兼容,弃用)。
  ⚠ 历史审核报告里记录的旧 uuid rule_id 随之失效:对历史报告「加例外」会 404;
  该物料走重审(recheck 链路)后自动关联新 id,属预期行为。
- **不删 state.json.bak 的 rules 键**(留作冻结备份;切 PG 后代码不再读它)。
"""
from __future__ import annotations
import json
import sys
from dataclasses import fields
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:   # 直接 python scripts/xxx.py 跑时,repo 根不在 sys.path
    sys.path.insert(0, str(_ROOT))

from app.domain.models import AuditRule                 # noqa: E402
from app.infrastructure.snowflake import next_id_str    # noqa: E402

_RULE_FIELDS = {f.name for f in fields(AuditRule)} - {"id"}   # 旧 id 弃用,其余字段原样迁


def migrate(state_path: str, repo, id_fn=next_id_str) -> tuple[int, int]:
    """把 state_path(state.json.bak)里的 rules 迁入 repo → (inserted, skipped)。
    以 no 幂等:repo 里已有同 no 在用规则 → 跳过。老数据缺新字段 → 走 dataclass 默认值。"""
    with open(state_path, encoding="utf-8") as f:
        data = json.load(f)
    existing_nos = {r.no for r in repo.list()}
    inserted = skipped = 0
    for d in data.get("rules", []):
        if d.get("no", 0) in existing_nos:
            skipped += 1
            continue
        rule = AuditRule(id=id_fn(), **{k: v for k, v in d.items() if k in _RULE_FIELDS})
        repo.add(rule, by="migration")
        existing_nos.add(rule.no)
        inserted += 1
    return inserted, skipped


if __name__ == "__main__":
    from app.config import settings

    if not settings.database_url or settings.database_url.startswith("postgresql://user:pass@localhost"):
        print("✗ AM_DATABASE_URL 未配置真实 PG,中止(未改动任何数据)。")
        sys.exit(1)
    if not settings.data_dir:
        print("✗ AM_DATA_DIR 未配置,找不到 state.json.bak,中止。")
        sys.exit(1)
    state_path = f"{settings.data_dir.rstrip('/')}/state.json.bak"

    from app.infrastructure.pg_rule_repo import PgAuditRuleRepo
    repo = PgAuditRuleRepo(settings.database_url)       # 连不上/建表失败 → 直接抛错
    inserted, skipped = migrate(state_path, repo)
    print(f"✓ 迁移完成:inserted={inserted} skipped={skipped};"
          f"PG 在用规则共 {len(repo.list())} 条(源 {state_path},rules 键保留作冻结备份)")
