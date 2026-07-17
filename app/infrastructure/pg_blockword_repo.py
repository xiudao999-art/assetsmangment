"""绝对禁词仓储 —— PostgreSQL 真源实现(实现 domain.ports.BlockwordRepo)。
遵循全项目 PG 业务表基础字段规范:id(雪花 BIGINT 主键) / del_flag(0=在用,删除时置新雪花 ID,软删)。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re

from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgBlockwordRepo:
    def __init__(self, dsn: str, table: str = "blockword", idgen=None) -> None:
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
                    word        TEXT NOT NULL,
                    del_flag    BIGINT NOT NULL DEFAULT 0,
                    create_by   TEXT NOT NULL DEFAULT '',
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now(),
                    update_by   TEXT NOT NULL DEFAULT '',
                    update_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '绝对禁词。审核第一波——命中任一即直接拦截(block),短路后续。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
            c.execute(f"COMMENT ON COLUMN {t}.word IS '禁词词条'")
            c.execute(f"COMMENT ON COLUMN {t}.del_flag IS '软删标记:0=在用，删除时置为新雪花ID'")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{t}_word ON {t} (word, del_flag)")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_live ON {t} (del_flag) WHERE del_flag = 0")

    def words(self) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT word FROM {self._table} WHERE del_flag = 0"
            ).fetchall()
        return {r[0] for r in rows}

    def list(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT word FROM {self._table} WHERE del_flag = 0 ORDER BY word"
            ).fetchall()
        return [r[0] for r in rows]

    def add(self, word: str) -> None:
        w = (word or "").strip()
        if not w:
            return
        with self._conn() as c:
            c.execute(
                f"""INSERT INTO {self._table} (id, word, create_by, update_by)
                    VALUES (%s, %s, '', '')
                    ON CONFLICT (word, del_flag) WHERE del_flag = 0 DO NOTHING""",
                (self._idgen(), w),
            )

    def remove(self, word: str) -> None:
        w = (word or "").strip()
        if not w:
            return
        with self._conn() as c:
            c.execute(
                f"UPDATE {self._table} SET del_flag = %s, update_time = now() "
                f"WHERE word = %s AND del_flag = 0",
                (self._idgen(), w),
            )
