"""审计日志仓储 —— PostgreSQL 真源实现(实现 domain.ports.AuditLog)。
只追加不删不改:每条事件记录雪花 ID + 时间戳,无 del_flag(纯日志,不软删)。
风格与 pg_rule_repo 一致:每操作短连接、autocommit、裸 SQL。infra→domain。"""
from __future__ import annotations
import re

from app.infrastructure.snowflake import next_id

_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class PgAuditLog:
    def __init__(self, dsn: str, table: str = "audit_log", idgen=None) -> None:
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
                    event       TEXT NOT NULL,
                    create_time TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            c.execute(f"COMMENT ON TABLE {t} IS '审计日志。只追加不删不改，记录权限操作等关键事件。'")
            c.execute(f"COMMENT ON COLUMN {t}.id IS '雪花算法 BIGINT 主键'")
            c.execute(f"COMMENT ON COLUMN {t}.event IS '事件描述文本'")
            c.execute(f"COMMENT ON COLUMN {t}.create_time IS '记录时间'")

    def record(self, event: str) -> None:
        with self._conn() as c:
            c.execute(
                f"INSERT INTO {self._table} (id, event) VALUES (%s, %s)",
                (self._idgen(), event),
            )
