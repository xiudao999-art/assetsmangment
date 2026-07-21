"""规则训练集仓储 —— PostgreSQL 真源实现(实现 domain.ports.TrainingSetRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删) /
create_by / create_time / update_by / update_time。domain 的 created_by 映射基础列 create_by。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL(线程安全、简单)。infra→domain。"""
from __future__ import annotations
import re
from typing import Optional

from app.domain.models import TrainingSet
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_SELECT_COLS = ("id, project_id, name, status, rule_snapshot, max_fp_ratio, "
                "max_iterations, training_result, started_at, completed_at, "
                "create_by")


class PgTrainingSetRepo:
    def __init__(self, dsn: str, table: str = "rule_training_set", idgen=None) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"非法表名: {table!r}")
        self._dsn = dsn
        self._table = table
        self._idgen = idgen or next_id
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
                    id               BIGINT PRIMARY KEY,
                    project_id       BIGINT NOT NULL,
                    name             TEXT NOT NULL DEFAULT '',
                    status           TEXT NOT NULL DEFAULT 'collecting',
                    rule_snapshot    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    max_fp_ratio     FLOAT8 NOT NULL DEFAULT 0.20,
                    max_iterations   INT NOT NULL DEFAULT 10,
                    training_result  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    started_at       TEXT NOT NULL DEFAULT '',
                    completed_at     TEXT NOT NULL DEFAULT '',
                    del_flag         BIGINT NOT NULL DEFAULT 0,
                    create_by        TEXT NOT NULL DEFAULT '',
                    create_time      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by        TEXT NOT NULL DEFAULT '',
                    update_time      TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '规则训练集:1:1 关联项目,存放训练配置、规则快照与训练结果。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
            c.execute(f"COMMENT ON COLUMN {t}.project_id IS '关联 project.id,在用行内唯一'")
            c.execute(f"COMMENT ON COLUMN {t}.name IS '训练集名称'")
            c.execute(f"COMMENT ON COLUMN {t}.status IS 'collecting/training/completed/failed'")
            c.execute(f"COMMENT ON COLUMN {t}.rule_snapshot IS '训练开始时项目规则的完整快照'")
            c.execute(f"COMMENT ON COLUMN {t}.max_fp_ratio IS '可接受的最大多判率(多命中/总应命中)'")
            c.execute(f"COMMENT ON COLUMN {t}.max_iterations IS '最大重审迭代次数'")
            c.execute(f"COMMENT ON COLUMN {t}.training_result IS '训练结果摘要:迭代次数/最终指标/规则变更'")
            # 增量迁移:旧表可能缺少这些列(先加列再 COMMENT,防列不存在)
            for col in ("started_at", "completed_at"):
                try:
                    c.execute(f"ALTER TABLE {t} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                except Exception:
                    pass  # 列已存在
            c.execute(f"COMMENT ON COLUMN {t}.started_at IS '最近一次训练开始时间(ISO 8601)'")
            c.execute(f"COMMENT ON COLUMN {t}.completed_at IS '最近一次训练完成时间(ISO 8601)'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_project ON {t} (project_id, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    # ── CRUD ──

    def add(self, ts: TrainingSet, by: str = "") -> None:
        """插入或按 id 覆盖(upsert)。更新只动业务列 + update_by/update_time,
        不动 create_by/create_time/del_flag。"""
        from psycopg.types.json import Jsonb
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, project_id, name, status, rule_snapshot, max_fp_ratio,
                         max_iterations, training_result, started_at, completed_at,
                         create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        project_id = EXCLUDED.project_id,
                        name = EXCLUDED.name,
                        status = EXCLUDED.status,
                        rule_snapshot = EXCLUDED.rule_snapshot,
                        max_fp_ratio = EXCLUDED.max_fp_ratio,
                        max_iterations = EXCLUDED.max_iterations,
                        training_result = EXCLUDED.training_result,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (int(ts.id), int(ts.project_id), ts.name, ts.status,
                 Jsonb(ts.rule_snapshot), ts.max_fp_ratio, ts.max_iterations,
                 Jsonb(ts.training_result),
                 ts.started_at or "", ts.completed_at or "",
                 ts.created_by or by, by or ts.created_by),
            )

    def get(self, ts_id: str) -> Optional[TrainingSet]:
        try:
            rid = int(ts_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE id = %s AND del_flag = 0", (rid,)
            ).fetchone()
        return self._to_ts(row) if row else None

    def get_by_project(self, project_id: str) -> Optional[TrainingSet]:
        try:
            pid = int(project_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE project_id = %s AND del_flag = 0 LIMIT 1", (pid,)
            ).fetchone()
        return self._to_ts(row) if row else None

    def delete(self, ts_id: str, by: str = "") -> None:
        """软删:del_flag 置新雪花 ID,释放 (project_id,0) 唯一位。"""
        try:
            rid = int(ts_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_by = %s, "
                f"update_time = now() WHERE id = %s AND del_flag = 0",
                (self._idgen(), by, rid),
            )

    def list(self) -> list[TrainingSet]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE del_flag = 0 ORDER BY create_time DESC"
            ).fetchall()
        return [self._to_ts(r) for r in rows]

    # ── 映射 ──

    @staticmethod
    def _to_ts(row) -> TrainingSet:
        return TrainingSet(
            id=str(row[0]),
            project_id=str(row[1]),
            name=row[2] or "",
            status=row[3] or "collecting",
            rule_snapshot=row[4] or {},
            max_fp_ratio=float(row[5]) if row[5] is not None else 0.20,
            max_iterations=int(row[6]) if row[6] is not None else 10,
            training_result=row[7] or {},
            started_at=row[8] or "",
            completed_at=row[9] or "",
            created_by=row[10] or "",
        )
