"""规则训练样本仓储 —— PostgreSQL 真源实现(实现 domain.ports.TrainingExampleRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删) /
create_by / create_time / update_by / update_time。domain 的 created_by 映射基础列 create_by。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL(线程安全、简单)。infra→domain。"""
from __future__ import annotations
import re
from typing import Optional

from app.domain.models import TrainingExample
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_SELECT_COLS = ("id, training_set_id, material_id, expected_rule_ids, "
                "source_note, create_by")


class PgTrainingExampleRepo:
    def __init__(self, dsn: str, table: str = "rule_training_example", idgen=None) -> None:
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
                    id                BIGINT PRIMARY KEY,
                    training_set_id   BIGINT NOT NULL,
                    material_id       BIGINT NOT NULL,
                    expected_rule_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    source_note       TEXT NOT NULL DEFAULT '',
                    del_flag          BIGINT NOT NULL DEFAULT 0,
                    create_by         TEXT NOT NULL DEFAULT '',
                    create_time       TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by         TEXT NOT NULL DEFAULT '',
                    update_time       TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '规则训练样本:记录某物料应被哪些规则命中(人工标注的地面真相)。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
            c.execute(f"COMMENT ON COLUMN {t}.training_set_id IS '关联 rule_training_set.id'")
            c.execute(f"COMMENT ON COLUMN {t}.material_id IS '关联 material.id,被标注的物料'")
            c.execute(f"COMMENT ON COLUMN {t}.expected_rule_ids IS '该物料应该命中的规则 ID 列表(字符串数组)'")
            c.execute(f"COMMENT ON COLUMN {t}.source_note IS '人工标注备注'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_mat ON {t} (training_set_id, material_id, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_set ON {t} (training_set_id, del_flag)")

    # ── CRUD ──

    def add(self, te: TrainingExample, by: str = "") -> None:
        """插入或按 id 覆盖(upsert)。更新只动业务列 + update_by/update_time,
        不动 create_by/create_time/del_flag。"""
        from psycopg.types.json import Jsonb
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, training_set_id, material_id, expected_rule_ids,
                         source_note, create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        training_set_id = EXCLUDED.training_set_id,
                        material_id = EXCLUDED.material_id,
                        expected_rule_ids = EXCLUDED.expected_rule_ids,
                        source_note = EXCLUDED.source_note,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (int(te.id), int(te.training_set_id), int(te.material_id),
                 Jsonb(te.expected_rule_ids), te.source_note,
                 te.created_by or by, by or te.created_by),
            )

    def get(self, te_id: str) -> Optional[TrainingExample]:
        try:
            rid = int(te_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE id = %s AND del_flag = 0", (rid,)
            ).fetchone()
        return self._to_te(row) if row else None

    def delete(self, te_id: str, by: str = "") -> None:
        """软删:del_flag 置新雪花 ID,释放 (training_set_id,material_id,0) 唯一位。"""
        try:
            rid = int(te_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_by = %s, "
                f"update_time = now() WHERE id = %s AND del_flag = 0",
                (self._idgen(), by, rid),
            )

    def list_for_set(self, training_set_id: str) -> list[TrainingExample]:
        try:
            tsid = int(training_set_id)
        except (TypeError, ValueError):
            return []
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE training_set_id = %s AND del_flag = 0 ORDER BY create_time DESC",
                (tsid,)
            ).fetchall()
        return [self._to_te(r) for r in rows]

    # ── 映射 ──

    @staticmethod
    def _to_te(row) -> TrainingExample:
        return TrainingExample(
            id=str(row[0]),
            training_set_id=str(row[1]),
            material_id=str(row[2]),
            expected_rule_ids=row[3] or [],
            source_note=row[4] or "",
            created_by=row[5] or "",
        )
