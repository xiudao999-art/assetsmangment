"""用户收藏仓储 —— PostgreSQL 真源实现(实现 domain.ports.FavoriteRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删)。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re

from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgFavoriteRepo:
    def __init__(self, dsn: str, table: str = "user_favorite", idgen=None) -> None:
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
                    user_id     TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    del_flag    BIGINT NOT NULL DEFAULT 0,
                    create_by   TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by   TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '用户收藏关系。把公共物料收进自己的物料库。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
            c.execute(f"COMMENT ON COLUMN {t}.user_id IS '用户 ID'")
            c.execute(f"COMMENT ON COLUMN {t}.material_id IS '物料 ID'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_pair ON {t} (user_id, material_id, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    def add(self, user_id: str, material_id: str) -> None:
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table} (id, user_id, material_id, create_by, update_by)
                    VALUES (%s, %s, %s, %s, '')
                    ON CONFLICT (user_id, material_id, del_flag) WHERE del_flag = 0 DO NOTHING""",
                (self._idgen(), user_id, material_id, user_id),
            )

    def remove(self, user_id: str, material_id: str) -> None:
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_time = now() "
                f"WHERE user_id = %s AND material_id = %s AND del_flag = 0",
                (self._idgen(), user_id, material_id),
            )

    def material_ids(self, user_id: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT material_id FROM {self._table} "
                f"WHERE user_id = %s AND del_flag = 0", (user_id,)
            ).fetchall()
        return {r[0] for r in rows}

    def has(self, user_id: str, material_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                f"SELECT 1 FROM {self._table} "
                f"WHERE user_id = %s AND material_id = %s AND del_flag = 0 LIMIT 1",
                (user_id, material_id),
            ).fetchone()
        return row is not None
