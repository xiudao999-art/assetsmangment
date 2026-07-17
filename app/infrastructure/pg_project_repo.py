"""作品项目仓储 —— PostgreSQL 真源实现(实现 domain.ports.ProjectRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删) /
create_by / create_time / update_by / update_time。domain 的 created_by 映射基础列 create_by。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL(线程安全、简单)。infra→domain。"""
from __future__ import annotations
import re
from typing import Optional

from app.domain.models import Project
from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_SELECT_COLS = "id, name, created_ms, create_by"


class PgProjectRepo:
    def __init__(self, dsn: str, table: str = "project", idgen=None) -> None:
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
                    id          BIGINT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    created_ms  BIGINT NOT NULL DEFAULT 0,
                    del_flag    BIGINT NOT NULL DEFAULT 0,
                    create_by   TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by   TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '作品项目。每个项目有自己的一组审核规则，作品必须归属项目。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键，API 序列化为字符串'")
            c.execute(f"COMMENT ON COLUMN {t}.name IS '项目名称，唯一(在用行内)'")
            c.execute(f"COMMENT ON COLUMN {t}.created_ms IS '创建时间戳(毫秒)，domain 透传，用于排序'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花 ID'")
            c.execute(f"COMMENT ON COLUMN {t}.create_by IS '创建人，映射 domain.created_by'")
            c.execute(f"COMMENT ON COLUMN {t}.create_time IS '创建时间'")
            c.execute(f"COMMENT ON COLUMN {t}.update_by IS '最后操作人'")
            c.execute(f"COMMENT ON COLUMN {t}.update_time IS '最后操作时间'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_name ON {t} (name, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    def add(self, project: Project) -> None:
        """插入或按 id 覆盖(upsert)。更新只动业务列 + update_by/update_time。"""
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table}
                        (id, name, created_ms, create_by, update_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        created_ms = EXCLUDED.created_ms,
                        update_by = EXCLUDED.update_by,
                        update_time = now()""",
                (int(project.id), project.name, project.created_ms,
                 project.created_by, project.created_by),
            )

    def get(self, project_id: str) -> Optional[Project]:
        try:
            rid = int(project_id)
        except (TypeError, ValueError):
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE id = %s AND del_flag = 0", (rid,)
            ).fetchone()
        return self._to_project(row) if row else None

    def get_by_name(self, name: str) -> Optional[Project]:
        n = (name or "").strip()
        if not n:
            return None
        with self._conn() as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} "
                f"WHERE name = %s AND del_flag = 0 LIMIT 1", (n,)
            ).fetchone()
        return self._to_project(row) if row else None

    def delete(self, project_id: str) -> None:
        """软删:del_flag 置新雪花 ID，释放 (name,0) 唯一位。"""
        try:
            rid = int(project_id)
        except (TypeError, ValueError):
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_by = '', update_time = now() "
                f"WHERE id = %s AND del_flag = 0",
                (self._idgen(), rid),
            )

    def list(self) -> list[Project]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE del_flag = 0 ORDER BY created_ms, id"
            ).fetchall()
        return [self._to_project(r) for r in rows]

    @staticmethod
    def _to_project(row) -> Project:
        rid, name, created_ms, create_by = row
        return Project(
            id=str(rid),             # 雪花 int64 → str,防 JS 2^53 精度丢失
            name=name,
            created_by=create_by,
            created_ms=created_ms,
        )
