"""审核规则仓储 —— PostgreSQL 真源实现(实现 domain.ports.AuditRuleRepo)。
全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删) /
create_by / create_time / update_by / update_time。domain 的 created_by 映射基础列 create_by。
风格与 pgvector_index 一致:每操作短连接、autocommit、裸 SQL(线程安全、简单)。infra→domain。"""
from __future__ import annotations
import re

from app.domain.models import AuditRule
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# domain 13 字段对应的读取列(id→str、create_by→created_by 在 _to_rule 转换)
_SELECT_COLS = ("id, no, source_type, keywords, condition, action, enabled, "
                "project_id, guidance, match_level, regex, exceptions, create_by")


class PgAuditRuleRepo:
    def __init__(self, dsn: str, table: str = "audit_rule", idgen=None) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"非法表名: {table!r}")
        self._dsn = dsn
        self._table = table
        self._idgen = idgen or next_id   # 软删时给 del_flag 发新雪花
        self._init_schema()

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True, connect_timeout=10,
                               options="-c timezone=Asia/Shanghai")

    def _init_schema(self) -> None:
        t = self._table
        with self._conn() as c:
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {t} (
                    id          BIGINT PRIMARY KEY,
                    no          INT NOT NULL DEFAULT 0,
                    source_type TEXT NOT NULL DEFAULT 'any',
                    keywords    JSONB NOT NULL DEFAULT '[]'::jsonb,
                    condition   TEXT NOT NULL DEFAULT '',
                    action      TEXT NOT NULL DEFAULT 'block',
                    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                    project_id  TEXT NOT NULL DEFAULT '',
                    guidance    TEXT NOT NULL DEFAULT '',
                    match_level TEXT NOT NULL DEFAULT 'metaphor',
                    regex       TEXT NOT NULL DEFAULT '',
                    exceptions  JSONB NOT NULL DEFAULT '[]'::jsonb,
                    del_flag    BIGINT NOT NULL DEFAULT 0,
                    create_by   TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by   TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '审核规则。每条规则按 source_type + project_id 匹配物料，命中后执行 action(block/review)。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键，API 序列化为字符串'")
            c.execute(f"COMMENT ON COLUMN {t}.no IS '规则编号，在用行内唯一、稳定、从 1 递增'")
            c.execute(f"COMMENT ON COLUMN {t}.source_type IS '匹配的文本来源类型(TextSourceType)，any=匹配所有'")
            c.execute(f"COMMENT ON COLUMN {t}.keywords IS '关键词快筛列表，命中任一即触发规则'")
            c.execute(f"COMMENT ON COLUMN {t}.condition IS '自然语言条件描述，提交大模型判定'")
            c.execute(f"COMMENT ON COLUMN {t}.action IS '命中后动作:block=硬拦, review=转人工'")
            c.execute(f"COMMENT ON COLUMN {t}.enabled IS '是否启用，停用规则不参与审核'")
            c.execute(f"COMMENT ON COLUMN {t}.project_id IS '所属项目，空=全局规则，非空=仅该项目作品生效'")
            c.execute(f"COMMENT ON COLUMN {t}.guidance IS '审核尺度说明，含好例/坏例，辅助大模型判定'")
            c.execute(f"COMMENT ON COLUMN {t}.match_level IS '匹配严格度:literal=精确, metaphor=隐喻, regex=正则'")
            c.execute(f"COMMENT ON COLUMN {t}.regex IS 'match_level=regex 时的正则表达式'")
            c.execute(f"COMMENT ON COLUMN {t}.exceptions IS '可放行例外列表，每项含 text/note/by/ms'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID。软删行不占 no 唯一位'")
            c.execute(f"COMMENT ON COLUMN {t}.create_by IS '创建人，映射 domain.created_by'")
            c.execute(f"COMMENT ON COLUMN {t}.create_time IS '创建时间，upsert 不更新此列'")
            c.execute(f"COMMENT ON COLUMN {t}.update_by IS '最后操作人，每次 upsert/delete 更新'")
            c.execute(f"COMMENT ON COLUMN {t}.update_time IS '最后操作时间，每次 upsert/delete 更新'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_no ON {t} (no, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    def add(self, rule: AuditRule, by: str = "") -> None:
        """插入或按 id 覆盖(upsert)。更新只动业务列 + update_by/update_time,
        不动 create_by/create_time/del_flag(创建痕迹与软删状态保持)。"""
        from psycopg.types.json import Jsonb   # psycopg3 不自动适配 list/dict,必须显式包裹
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, no, source_type, keywords, condition,
                         action, enabled, project_id, guidance, match_level, regex, exceptions,
                         create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        no = EXCLUDED.no, source_type = EXCLUDED.source_type,
                        keywords = EXCLUDED.keywords, condition = EXCLUDED.condition,
                        action = EXCLUDED.action, enabled = EXCLUDED.enabled,
                        project_id = EXCLUDED.project_id, guidance = EXCLUDED.guidance,
                        match_level = EXCLUDED.match_level, regex = EXCLUDED.regex,
                        exceptions = EXCLUDED.exceptions,
                        update_by = EXCLUDED.update_by, update_time = now()""",
                (int(rule.id), rule.no, rule.source_type, Jsonb(rule.keywords), rule.condition,
                 rule.action, rule.enabled, rule.project_id, rule.guidance,
                 rule.match_level, rule.regex, Jsonb(rule.exceptions),
                 rule.created_by or by, by or rule.created_by),
            )

    def delete(self, rule_id: str, by: str = "") -> None:
        """软删:del_flag 置新雪花 ID(行保留、释放 (no,0) 唯一位)。只删在用行 → 二次删除无副作用。"""
        try:
            rid = int(rule_id)
        except (TypeError, ValueError):
            return   # 旧 uuid/合成 id:对齐 JSON pop(..., None) 的容忍,静默无操作
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_by = %s, update_time = now() "
                f"WHERE id = %s AND del_flag = 0",
                (self._idgen(), by, rid),
            )

    def list(self) -> list[AuditRule]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE del_flag = 0 ORDER BY no, id"
            ).fetchall()
        return [self._to_rule(r) for r in rows]

    def list_for(self, source_type: str, project_id: str = "") -> list[AuditRule]:
        # 复用 domain 的 applies_to 语义(不把它复制进 SQL,防两处分叉);规则量小,全读可接受
        return [r for r in self.list() if r.applies_to(source_type, project_id)]

    @staticmethod
    def _to_rule(row) -> AuditRule:
        (rid, no, source_type, keywords, condition, action, enabled,
         project_id, guidance, match_level, regex, exceptions, create_by) = row
        return AuditRule(
            id=str(rid),                       # 雪花 int64 → str,防 JS 2^53 精度丢失
            no=no, source_type=source_type, keywords=list(keywords or []),
            condition=condition, action=action, enabled=enabled,
            created_by=create_by, project_id=project_id, guidance=guidance,
            match_level=match_level, regex=regex, exceptions=list(exceptions or []),
        )
